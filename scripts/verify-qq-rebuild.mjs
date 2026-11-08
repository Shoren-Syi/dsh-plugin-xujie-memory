// 端到端：改 QQ 配置 → 5 秒内真的重建运行时（真内核 + 真 QQRuntime + 假 OneBot）
import assert from 'node:assert/strict'
import { QQRuntime } from '../lib/qq/runtime.js'
import { QQTransport } from '../lib/qq/transport.js'
import { createQQBridge } from '../lib/qq/bridge.js'

const KERNEL = process.env.KERNEL_URL || 'http://127.0.0.1:8871'
const FAKE = process.env.FAKE_HTTP || 'http://127.0.0.1:8890'
const TOKEN = process.env.FAKE_TOKEN || 'secret-token'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function kernel(path, body) {
  const res = await fetch(`${KERNEL}${path}`, body === undefined
    ? {}
    : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  return res.json()
}

const settingsBody = (marker) => ({
  selfId: '10001', wsUrl: 'ws://127.0.0.1:8890', httpUrl: FAKE,
  accessToken: TOKEN, privateAllow: ['20002'], groupAllow: [],
  requireMention: true, provider: 'p', model: 'm', maxTokens: 256, budgetScope: '10001',
  marker,
})

// 与宿主同构的最小 ctx 桩
const handlers = []
const ctx = {
  llm: {},
  agents: {
    async create() {
      return { agent: { followup() {}, async whenIdle() {}, cancel() {} }, async dispose() {} }
    },
    async resume() { const e = new Error('none'); e.name = 'SessionPersistenceNotFoundError'; throw e },
  },
  on(type, fn) { handlers.push([type, fn]); return () => {} },
}

const events = []
const bridge = createQQBridge({
  fetchJson: async (path) => {
    const res = await fetch(`${KERNEL}${path}`)
    return res.json()
  },
  identities: new Map(),
  intervalMs: 1000,
  log: () => {},
  warn: (msg) => events.push({ warn: msg }),
  createRuntime: async (qq) => {
    events.push({ create: qq.revision })
    return new QQRuntime({
      ctx,
      config: qq,
      ledger: {},
      transport: new QQTransport(qq.httpUrl, qq.accessToken || ''),
      makeMessage: (text) => ({ role: 'user', content: [{ type: 'text', text }] }),
      identities: new Map(),
      onError: (e) => events.push({ error: String(e?.message || e) }),
    })
  },
})

const t0 = Date.now()
bridge.start()

// 1) 初始没有配置：不该建
await sleep(1200)
assert.equal(bridge.runtime, null, '初始无配置不应建立运行时')

// 2) 写入配置 → 等轮询生效
const saved = await kernel('/qq/settings', settingsBody('A'))
assert.equal(saved.ok, true)
const tWriteA = Date.now()
for (let i = 0; i < 60 && !bridge.runtime; i++) await sleep(200)
const tConnectedA = Date.now()

// 3) 改配置（revision 变化）→ 等重建
const saved2 = await kernel('/qq/settings', settingsBody('B'))
const tWriteB = Date.now()
let rebuilt = false
for (let i = 0; i < 60; i++) {
  await sleep(200)
  if (events.filter((e) => e.create).length >= 2) { rebuilt = true; break }
}
const tRebuiltB = Date.now()

// 4) 删配置 → 运行时关闭
await kernel('/qq/settings', undefined) // GET 不行，需要 DELETE
await fetch(`${KERNEL}/qq/settings`, { method: 'DELETE' })
const tDelete = Date.now()
let closed = false
for (let i = 0; i < 60; i++) {
  await sleep(200)
  if (bridge.runtime === null) { closed = true; break }
}
const tClosed = Date.now()

await bridge.dispose()

const report = {
  ok: true,
  initial_no_runtime: true,
  connected_after_save_ms: tConnectedA - tWriteA,
  rebuilt_after_change_ms: rebuilt ? tRebuiltB - tWriteB : -1,
  closed_after_delete_ms: closed ? tClosed - tDelete : -1,
  revisions: [saved.revision, saved2?.revision].filter(Boolean),
  creates: events.filter((e) => e.create).map((e) => e.create),
  warns: events.filter((e) => e.warn).map((e) => e.warn),
  total_ms: Date.now() - t0,
}
assert.ok(report.connected_after_save_ms < 7000, `首次连接应在轮询周期内：${report.connected_after_save_ms}ms`)
assert.ok(rebuilt, '改配置后应重建运行时')
assert.ok(report.rebuilt_after_change_ms < 7000, `重建应在轮询周期内：${report.rebuilt_after_change_ms}ms`)
assert.ok(closed, '删配置后应关闭运行时')
assert.equal(report.warns.length, 0, `不该有警告：${report.warns.join('; ')}`)
console.log(JSON.stringify(report, null, 1))
