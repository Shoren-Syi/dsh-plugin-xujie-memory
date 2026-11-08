// QQ 网络层验收：连假 OneBot，验鉴权、动作调用、事件下行
import assert from 'node:assert/strict'
import { QQTransport } from '../lib/qq/transport.js'
import { QQRuntime } from '../lib/qq/runtime.js'

const HTTP = process.env.FAKE_HTTP || 'http://127.0.0.1:8890'
const WS = process.env.FAKE_WS || 'ws://127.0.0.1:8890'
const TOKEN = process.env.FAKE_TOKEN || 'secret-token'
const report = { steps: [] }
process.on('uncaughtException', (e) => { console.log('UNCAUGHT ' + (e?.stack || e)); process.exitCode = 3 })
process.on('unhandledRejection', (e) => { console.log('UNHANDLED ' + (e?.stack || e)); process.exitCode = 4 })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function dump() {
  const res = await fetch(`${HTTP}/dump`)
  return res.json()
}

// 1) 带 token 的私聊发送
const good = new QQTransport(HTTP, TOKEN)
const sent = await good.message('private', '20002', '你好呀')
assert.equal(sent?.message_id, 42)

// 2) 错误 token → 401
const bad = new QQTransport(HTTP, 'wrong')
await assert.rejects(() => bad.message('private', '20002', 'x'), /HTTP 401|未确认/)

// 3) 空间动态
await good.publish('今天的感想', { visibility: 64 })

// 4) 事件下行：假端推一条私聊消息，运行时应该接住并驱动会话
const handlers = { 'llm/stream': [], 'session/event': [] }
const calls = { followups: [], messages: [] }
let reply = '在的'
const ctx = {
  llm: {},
  agents: {
    async create({ sessionId }) {
      return {
        agent: {
          followup(text) { calls.followups.push(text) },
          async whenIdle() {
            for (const fn of handlers['session/event']) {
              fn({ id: sessionId }, { type: 'assistant/message',
                data: { message: { content: [{ type: 'text', text: reply }] } } })
            }
          },
          cancel() {},
        },
        async dispose() {},
      }
    },
    async resume() {
      const e = new Error('missing'); e.name = 'SessionPersistenceNotFoundError'; throw e
    },
  },
  on(type, fn) { handlers[type].push(fn); return () => {} },
}
const errors = []
const runtime = new QQRuntime({
  ctx,
  config: { selfId: '10001', privateAllow: ['20002'], groupAllow: [], requireMention: true,
    provider: 'p', model: 'm', maxTokens: 256, budgetScope: '10001',
    wsUrl: `${WS}?access_token=${TOKEN}`, accessToken: TOKEN, httpUrl: HTTP },
  ledger: {},
  transport: good,
  makeMessage: (text) => ({ role: 'user', content: [{ type: 'text', text }] }),
  identities: new Map(),
  onError: (e) => errors.push(e),
})
runtime.connect()
await sleep(2500)
const after = await dump()
await runtime.close()

const reportOut = {
  ok: true,
  actions: after.actions.map((a) => a.action),
  clients_seen: after.clients,
  followups: calls.followups,
  errors: errors.map((e) => String(e?.message || e)),
}
reportOut.action_names = reportOut.actions

// 断言：私聊、空间、事件驱动都发生过
assert.ok(after.actions.some((a) => a.action === 'send_private_msg'), '要有 send_private_msg')
assert.ok(after.actions.some((a) => a.action === 'send_qzone_msg'), '要有 send_qzone_msg')
assert.ok(calls.followups.length >= 1, '假 OneBot 推的事件要能驱动会话')

console.log(JSON.stringify(reportOut, null, 1))
