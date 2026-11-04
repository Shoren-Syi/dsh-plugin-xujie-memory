import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { test } from 'node:test'
import { QQTransport } from '../lib/qq-transport.js'
import { QQInbound } from '../lib/qq-inbound.js'
import { guardQQStream } from '../lib/qq-cost-guard.js'
import { QQBudgetLedger } from '../lib/qq-budget-ledger.js'
import { QQRuntime } from '../lib/qq-runtime.js'

test('QQ 群中不同发言人绑定不同 DSH 会话和记忆身份', async () => {
  const handlers = new Map(), created = [], sent = [], identities = new Map(), errors = []
  const ctx = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name) },
    llm: { resolveModelInfo: async () => ({ context: { contextWindow: 100 } }) },
    agents: {
      async resume() { const error = new Error('new'); error.name = 'SessionPersistenceNotFoundError'; throw error },
      async create({ sessionId, agentOptions }) {
        const turns = []
        created.push({ sessionId, agentOptions, turns })
        return { agent: { followup(message) { turns.push(message) }, async whenIdle() {
          handlers.get('session/event')({ id: sessionId }, { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '你好呀' }] } } })
        } }, dispose() {} }
      },
    },
  }
  const config = { selfId: '99', groupAllow: ['20'], privateAllow: ['10'], provider: 'p', model: 'm', maxTokens: 200, budgetScope: 'owner' }
  const runtime = new QQRuntime({ ctx, config, identities, makeMessage: text => ({ text }),
    ledger: {}, transport: { message: async (...args) => sent.push(args) }, onError: error => errors.push(error) })
  const event = { post_type: 'message', message_type: 'group', group_id: 20, user_id: 10, message_id: 1,
    message: [{ type: 'at', data: { qq: '99' } }, { type: 'text', data: { text: '你好' } }] }
  await runtime.handle(event)
  await runtime.handle({ ...event, user_id: 11, message_id: 2 })
  assert.equal(created.length, 2)
  assert.notEqual(created[0].sessionId, created[1].sessionId)
  assert.notEqual(identities.get(created[0].sessionId), identities.get(created[1].sessionId))
  assert.equal(created[0].turns[0].text, '你好')
  assert.deepEqual(sent, [['group', '20', '你好呀'], ['group', '20', '你好呀']])
  await runtime.forward({ id: created[0].sessionId }, { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '非请求回复' }] } } })
  assert.equal(sent.length, 2)
  assert.equal(errors.length, 0)
  await runtime.close()
  assert.equal(identities.size, 0)
  assert.equal(handlers.size, 0)
})

test('QQ 长期运行时回收最早的空闲会话并保留身份隔离', async () => {
  const handlers = new Map(), disposed = [], identities = new Map()
  const ctx = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name) },
    llm: {},
    agents: {
      async resume() { const error = new Error('new'); error.name = 'SessionPersistenceNotFoundError'; throw error },
      async create({ sessionId }) { return { agent: { followup() {}, async whenIdle() {} }, dispose() { disposed.push(sessionId) } } },
    },
  }
  const runtime = new QQRuntime({ ctx, identities, ledger: {}, transport: {}, makeMessage: text => ({ text }),
    config: { selfId: '99', privateAllow: [], groupAllow: ['20'], provider: 'p', model: 'm', maxTokens: 100, budgetScope: 'owner' },
    onError: error => { throw error } })
  for (let id = 1; id <= 129; id++) await runtime.handle({ post_type: 'message', message_type: 'group', group_id: 20,
    user_id: id + 100, message_id: id, message: [{ type: 'at', data: { qq: '99' } }, { type: 'text', data: { text: '你好' } }] })
  assert.equal(runtime.records.size, 128)
  assert.equal(identities.size, 128)
  assert.equal(disposed.length, 1)
  assert.equal(identities.has(disposed[0]), false)
  await runtime.close()
  assert.equal(disposed.length, 129)
  assert.equal(identities.size, 0)
})

test('关闭 QQ 桥接时取消正在建立的会话', async () => {
  const handlers = new Map(), identities = new Map()
  let ready
  const started = new Promise(resolve => { ready = resolve })
  const ctx = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name) },
    llm: {},
    agents: { async resume({ signal }) {
      ready()
      return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('已取消')), { once: true }))
    } },
  }
  const runtime = new QQRuntime({ ctx, identities, ledger: {}, transport: {}, makeMessage: text => ({ text }),
    config: { selfId: '99', privateAllow: ['10'], provider: 'p', model: 'm', maxTokens: 100, budgetScope: 'owner' },
    onError: () => {} })
  const pending = runtime.handle({ post_type: 'message', message_type: 'private', user_id: 10, message_id: 1,
    message: [{ type: 'text', data: { text: '你好' } }] })
  await started
  const rejected = assert.rejects(pending, /已取消/)
  await runtime.close()
  await rejected
  assert.equal(identities.size, 0)
  assert.equal(handlers.size, 0)
})

test('主动私聊与空间任务使用受控活动预算，且一次任务最多发一条', async () => {
  const handlers = new Map(), sent = [], sources = [], observed = []
  let runtime
  const ctx = {
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name) },
    llm: {},
    agents: { async resume() { const error = new Error('new'); error.name = 'SessionPersistenceNotFoundError'; throw error },
      async create({ sessionId }) {
        return { agent: { followup(input) { sources.push(input.source) }, async whenIdle() {
          observed.push(runtime.bindings.get(sessionId).activity)
          const event = { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '测试内容' }] } } }
          handlers.get('session/event')({ id: sessionId }, event)
          handlers.get('session/event')({ id: sessionId }, event)
        } }, dispose() {} }
      } },
  }
  const config = { selfId: '99', privateAllow: ['10'], provider: 'p', model: 'm', maxTokens: 200,
    budgetScope: 'owner', schedule: { qzone: { visibility: 64 } } }
  runtime = new QQRuntime({ ctx, config, identities: new Map(), ledger: {},
    makeMessage: (text, source) => ({ text, source }), onError: error => { throw error },
    transport: { message: async (...args) => sent.push(['message', ...args]), publish: async (...args) => sent.push(['publish', ...args]) } })
  await runtime.run({ channel: 'private', conversation: '10', actor: '10', text: '主动发言' }, 'proactive', 'runtime-context')
  await runtime.run({ channel: 'qzone', conversation: '99', actor: '99', text: '发布动态' }, 'qzone', 'runtime-context')
  assert.deepEqual(sources, ['runtime-context', 'runtime-context'])
  assert.deepEqual(observed, ['proactive', 'qzone'])
  assert.deepEqual(sent, [['message', 'private', '10', '测试内容'], ['publish', '测试内容', { visibility: 64 }]])
  await runtime.close()
})

test('QQ 预算使用带范围的内核请求，未确认的响应不能放行', async () => {
  const requests = []
  const ledger = new QQBudgetLedger(async (route, body) => {
    requests.push({ route, body })
    return route.endsWith('/reserve') ? { ok: true, call_id: 'a' } : { ok: true, amount: 12 }
  })
  const call = await ledger.reserve('scope-a', 'chat', 1000, 200)
  assert.deepEqual(call, { scope: 'scope-a', id: 'a' })
  assert.equal(await ledger.settle(call, 100, 10, 20), 12)
  assert.equal(requests[1].body.scope_id, 'scope-a')
  assert.equal(requests[1].body.cached_tokens, 20)
  await assert.rejects(() => new QQBudgetLedger(async () => ({ ok: false })).reserve('scope-a', 'chat', 1, 1), /未确认/)
})

test('QQ 模型请求先预留预算，再按用量结算；未绑定的会话不受影响', async () => {
  const events = [], sessions = new Map([['qq-session', { scope: 'owner', activity: 'chat', provider: 'p', model: 'm', maxTokens: 100 }]])
  const llm = { resolveModelInfo: async () => ({ context: { contextWindow: 4096 } }) }
  const ledger = { reserve: async (...args) => { events.push(['reserve', ...args]); return 'call' }, settle: async (...args) => events.push(['settle', ...args]) }
  const guard = guardQQStream({ llm, ledger, sessions, onError: error => { throw error } })
  async function* upstream() { events.push(['dispatch']); yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 3, cacheReadTokens: 2 } } }
  const chunks = []
  for await (const chunk of guard({ sessionId: 'qq-session', provider: 'p', model: 'm', maxTokens: 100 }, upstream)) chunks.push(chunk)
  assert.equal(chunks.length, 1)
  assert.deepEqual(events, [['reserve', 'owner', 'chat', 4096, 100], ['dispatch'], ['settle', 'call', 10, 3, 2]])
  events.length = 0
  for await (const _ of guard({ sessionId: 'other' }, upstream)) { /* 消费结果 */ }
  assert.deepEqual(events, [['dispatch']])
})

test('QQ 模型预算不足时绝不发起请求，用量缺失时保留预留', async () => {
  const errors = [], sessions = new Map([['qq', { scope: 'owner', activity: 'chat', provider: 'p', model: 'm', maxTokens: 100 }]])
  const llm = { resolveModelInfo: async () => ({ context: { contextWindow: 100 } }) }
  let dispatched = false
  async function* upstream() { dispatched = true; yield { type: 'text-delta', text: 'ok' } }
  const denied = guardQQStream({ llm, sessions, ledger: { reserve: async () => { throw new Error('预算不足') } }, onError: e => errors.push(e.message) })
  await assert.rejects(async () => { for await (const _ of denied({ sessionId: 'qq', provider: 'p', model: 'm', maxTokens: 10 }, upstream)) {} }, /预算不足/)
  assert.equal(dispatched, false)
  await assert.rejects(async () => { for await (const _ of denied({ sessionId: 'qq', provider: 'p', model: 'other', maxTokens: 10 }, upstream)) {} }, /模型与预算配置不匹配/)
  await assert.rejects(async () => { for await (const _ of denied({ sessionId: 'qq', provider: 'p', model: 'm' }, upstream)) {} }, /输出上限未知/)
  await assert.rejects(async () => { for await (const _ of denied({ sessionId: 'qq', provider: 'p', model: 'm', maxTokens: 101 }, upstream)) {} }, /超过 QQ 设置上限/)
  const allowed = guardQQStream({ llm, sessions, ledger: { reserve: async () => 'call', settle: async () => assert.fail('未知用量不可结算') }, onError: e => errors.push(e.message) })
  for await (const _ of allowed({ sessionId: 'qq', provider: 'p', model: 'm', maxTokens: 10 }, upstream)) {}
  assert.deepEqual(errors, ['模型用量不完整，预算预留未释放'])
})

test('群消息按群、发言人和 @ 条件过滤，重复消息不再调用', () => {
  const gate = new QQInbound({ selfId: '99', groupAllow: ['20'], privateAllow: ['10'] })
  const message = [{ type: 'at', data: { qq: '99' } }, { type: 'text', data: { text: ' 你好 ' } }]
  const event = { post_type: 'message', message_type: 'group', self_id: 99, group_id: 20, user_id: 10, message_id: 1, message }
  assert.deepEqual(gate.accept(event, 100), { channel: 'group', conversation: '20', actor: '10', text: '你好', messageId: '1' })
  assert.equal(gate.accept(event, 101), null)
  assert.equal(gate.accept({ ...event, message_id: 2, message: [message[1]] }, 102), null)
  assert.equal(gate.accept({ ...event, message_id: 3, group_id: undefined }, 103), null)
  assert.equal(gate.accept({ ...event, message_id: 4, user_id: 99 }, 104), null)
  assert.equal(gate.accept({ ...event, message_id: 5, self_id: 98 }, 105), null)
  assert.equal(gate.accept({ ...event, message_id: 6, group_id: 21 }, 106), null)
  const direct = { ...event, message_type: 'private', message_id: 7, message: [message[1]] }
  assert.equal(gate.accept(direct, 107).actor, '10')
  assert.equal(gate.accept({ ...direct, message_id: 8, user_id: 11 }, 108), null)
})

test('QQ 超长消息和过多片段在调用模型前被拒绝', () => {
  const gate = new QQInbound({ selfId: '99', privateAllow: ['10'] })
  const event = { post_type: 'message', message_type: 'private', self_id: 99, user_id: 10, message_id: 1 }
  assert.throws(() => gate.accept({ ...event, message: [{ type: 'text', data: { text: '字'.repeat(2001) } }] }), /超过 2000 字/)
  assert.throws(() => gate.accept({ ...event, message: Array.from({ length: 129 }, () => ({ type: 'text', data: { text: '字' } })) }), /片段过多/)
  assert.equal(gate.accept({ ...event, user_id: '1'.repeat(21), message: [{ type: 'text', data: { text: '你好' } }] }), null)
})

test('QQ 私聊、群聊和空间动态使用对应接口并传递权限', async () => {
  const requests = []
  const server = createServer(async (request, response) => {
    let body = ''
    for await (const part of request) body += part
    requests.push({ path: request.url, auth: request.headers.authorization, body: JSON.parse(body) })
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ status: 'ok', retcode: 0, data: { id: 'test' } }))
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  try {
    const client = new QQTransport(`http://127.0.0.1:${server.address().port}`, 'test-token')
    await client.message('private', '12345', '你好')
    await client.message('group', '67890', '大家好')
    await client.publish('今天晴天', { visibility: 16, targets: ['12345'] })
    assert.deepEqual(requests.map(r => r.path), ['/send_private_msg', '/send_group_msg', '/send_qzone_msg'])
    assert.ok(requests.every(r => r.auth === 'Bearer test-token'))
    assert.equal(requests[0].body.user_id, '12345')
    assert.equal(requests[1].body.group_id, '67890')
    assert.deepEqual(requests[2].body, { content: '今天晴天', ugc_right: 16, target_uins: ['12345'], images: [] })
    await assert.rejects(() => client.publish('测试', { visibility: 16 }), /可见范围/)
    await assert.rejects(() => client.message('group', 'not-a-number', '测试'), /不合法/)
  } finally {
    server.close()
    await once(server, 'close')
  }
})

test('OneBot 报错时不能把发送当成成功', async () => {
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ status: 'failed', retcode: 100, data: null }))
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  try {
    const client = new QQTransport(`http://127.0.0.1:${server.address().port}`)
    await assert.rejects(() => client.publish('测试'), /未确认操作成功/)
  } finally {
    server.close()
    await once(server, 'close')
  }
})
