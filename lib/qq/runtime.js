import { createHash } from 'node:crypto'
import { QQInbound } from './inbound.js'
import { guardQQStream } from './cost-guard.js'

// 会话 id 前缀改成我们自己的：同一个 QQ 号在别的插件里不会撞会话。
const SESSION_PREFIX = 'hippo-qq:'

function sessionId(self, channel, conversation, actor) {
  const hex = createHash('sha256').update(`${SESSION_PREFIX}${self}:${channel}:${conversation}:${actor}`)
    .digest('hex')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`
}

/** 管理 QQ 发言人对应的 DSH 会话；网络与账本由宿主注入。
 *
 * 移植自 0.8 `lib/qq-runtime.js`。改动只有三处：
 * 1. 会话 id 前缀（避免和原插件撞）；
 * 2. 缺少 llm/agents 服务时给出明确错误而不是 undefined；
 * 3. close() 里先停定时器再断连接。
 */
export class QQRuntime {
  constructor({ ctx, config, ledger, transport, makeMessage, identities, onError }) {
    if (!ctx?.agents || !ctx?.llm || typeof makeMessage !== 'function' || typeof onError !== 'function') {
      throw new TypeError('QQ 桥接缺少 DSH 服务或错误处理')
    }
    if (!config.provider || !config.model || !config.budgetScope
      || !Number.isSafeInteger(config.maxTokens) || config.maxTokens <= 0) {
      throw new TypeError('QQ 桥接需要模型、输出上限和预算范围')
    }
    this.ctx = ctx
    this.config = config
    this.transport = transport
    this.makeMessage = makeMessage
    this.identities = identities
    this.onError = onError
    this.gate = new QQInbound(config)
    this.bindings = new Map()
    this.records = new Map()
    this.attachChain = Promise.resolve()
    this.queues = new Map()
    this.closed = false
    this.abortController = new AbortController()
    this.socket = null
    this.reconnectTimer = null
    this.scheduleTimers = []
    this.offStream = ctx.on('llm/stream', guardQQStream({ llm: ctx.llm, ledger, sessions: this.bindings, onError }))
    this.offEvent = ctx.on('session/event', (session, event) => { void this.forward(session, event).catch(onError) })
  }

  connect() {
    if (this.closed) throw new Error('QQ 桥接已关闭')
    if (this.socket || this.reconnectTimer) return
    const url = new URL(this.config.wsUrl)
    if (!['ws:', 'wss:'].includes(url.protocol) || url.username || url.password || url.hash) {
      throw new TypeError('OneBot WebSocket 地址不合法')
    }
    if (this.config.accessToken) url.searchParams.set('access_token', this.config.accessToken)
    const socket = new WebSocket(url)
    this.socket = socket
    socket.addEventListener('message', event => {
      try {
        const payload = String(event.data)
        if (payload.length > 65536) throw new Error('QQ 消息数据过大，已忽略')
        void this.handle(JSON.parse(payload)).catch(this.onError)
      } catch (error) { this.onError(error) }
    })
    socket.addEventListener('open', () => this.startSchedules())
    socket.addEventListener('error', () => this.onError(new Error('QQ 连接异常')))
    socket.addEventListener('close', () => {
      if (this.socket !== socket) return
      this.socket = null
      if (!this.closed) {
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null
          try { this.connect() } catch (error) { this.onError(error) }
        }, 5000)
      }
    })
  }

  async handle(event) {
    if (this.closed) throw new Error('QQ 桥接已关闭')
    const message = this.gate.accept(event)
    if (!message) return false
    await this.run(message, 'chat', 'user')
    return true
  }

  startSchedules() {
    if (this.scheduleTimers.length || this.closed) return
    for (const activity of ['proactive', 'qzone']) {
      const settings = this.config.schedule?.[activity]
      if (!settings?.enabled) continue
      const timer = setInterval(() => {
        if (this.closed || this.socket?.readyState !== WebSocket.OPEN) return
        const target = activity === 'proactive' ? settings.target : this.gate.selfId
        const message = {
          channel: activity === 'proactive' ? (settings.channel ?? 'private') : 'qzone',
          conversation: target,
          actor: activity === 'proactive' && (settings.channel ?? 'private') === 'private' ? target : this.gate.selfId,
          text: settings.prompt,
        }
        const id = sessionId(this.gate.selfId, message.channel, message.conversation, message.actor)
        if (!this.queues.has(id)) void this.run(message, activity, 'runtime-context').catch(this.onError)
      }, settings.intervalMinutes * 60000)
      timer.unref?.()
      this.scheduleTimers.push(timer)
    }
  }

  async run(message, activity, source) {
    const id = sessionId(this.gate.selfId, message.channel, message.conversation, message.actor)
    const previous = this.queues.get(id) ?? Promise.resolve()
    const task = previous.catch(this.onError).then(async () => {
      if (this.closed) return
      const record = await this.attach(id, message)
      record.inflight = true
      record.activity = activity
      record.responded = false
      this.bindings.get(id).activity = activity
      try {
        record.agent.followup(this.makeMessage(message.text, source))
        await record.agent.whenIdle()
      } finally {
        record.inflight = false
        this.bindings.get(id).activity = 'chat'
      }
    })
    this.queues.set(id, task)
    try { await task } finally { if (this.queues.get(id) === task) this.queues.delete(id) }
  }

  async attach(id, message) {
    const task = this.attachChain.then(() => this.attachRecord(id, message))
    this.attachChain = task.then(() => {}, () => {})
    return task
  }

  async attachRecord(id, message) {
    if (this.closed) throw new Error('QQ 桥接已关闭')
    const cached = this.records.get(id)
    if (cached) {
      this.records.delete(id)
      this.records.set(id, cached)
      return cached
    }
    if (this.records.size >= 128) {
      const idle = [...this.records.keys()].find(key => !this.queues.has(key))
      if (!idle) throw new Error('QQ 会话繁忙，请稍后再试')
      await this.records.get(idle).dispose()
      this.records.delete(idle)
      this.identities.delete(idle)
      this.bindings.delete(idle)
    }
    const cfg = this.config
    const agentOptions = { provider: cfg.provider, model: cfg.model, maxTokens: cfg.maxTokens }
    let handle
    this.identities.set(id, `qq:${this.gate.selfId}:${message.channel}:${message.conversation}:${message.actor}`)
    this.bindings.set(id, {
      scope: cfg.budgetScope, activity: 'chat', provider: cfg.provider,
      model: cfg.model, maxTokens: cfg.maxTokens,
    })
    try {
      try {
        handle = await this.ctx.agents.resume({ resumeSessionId: id, agentOptions, signal: this.abortController.signal })
      } catch (error) {
        if (error?.name !== 'SessionPersistenceNotFoundError') throw error
        handle = await this.ctx.agents.create({ sessionId: id, agentOptions, signal: this.abortController.signal })
      }
      if (!handle?.agent || typeof handle.agent.followup !== 'function'
        || typeof handle.agent.whenIdle !== 'function' || typeof handle.dispose !== 'function') {
        throw new Error('DSH 未返回可用会话')
      }
      if (this.closed) throw new Error('QQ 桥接已关闭')
    } catch (error) {
      this.identities.delete(id)
      this.bindings.delete(id)
      if (typeof handle?.dispose === 'function') await handle.dispose()
      throw error
    }
    const record = { agent: handle.agent, dispose: () => handle.dispose(), message, inflight: false }
    this.records.set(id, record)
    return record
  }

  async forward(session, event) {
    if (this.closed) return
    const id = String(session?.id ?? session?.header?.id ?? '')
    const record = this.records.get(id)
    if (!record?.inflight) return
    if (event?.type === 'agent/error') {
      this.onError(new Error('QQ 会话执行失败，请检查模型、预算和 DSH 日志'))
      return
    }
    if (event?.type !== 'assistant/message') return
    const content = event.data?.message?.content
    if (!Array.isArray(content) || content.some(item => item?.type === 'tool-call')) return
    const text = content.filter(item => item?.type === 'text' && typeof item.text === 'string')
      .map(item => item.text).join('').trim()
    if (!text || (record.activity !== 'chat' && record.responded)) return
    if (record.activity !== 'chat') record.responded = true
    if (record.activity === 'qzone') {
      await this.transport.publish(text, { visibility: this.config.schedule.qzone.visibility })
    } else {
      await this.transport.message(record.message.channel, record.message.conversation, text)
    }
  }

  async close() {
    this.closed = true
    this.abortController.abort()
    for (const timer of this.scheduleTimers) clearInterval(timer)
    this.scheduleTimers = []
    for (const record of this.records.values()) record.agent.cancel?.({ kind: 'disposed' })
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.socket?.close()
    this.socket = null
    this.offStream?.()
    this.offEvent?.()
    await Promise.allSettled([...this.queues.values()])
    const records = [...this.records.values()]
    this.records.clear()
    for (const id of this.bindings.keys()) this.identities.delete(id)
    this.bindings.clear()
    for (const record of records) await record.dispose()
  }
}
