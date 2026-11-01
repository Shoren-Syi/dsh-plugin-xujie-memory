/**
 * 工具层测试（node --test）：memory_search / memory_forget / memory_status
 *
 * 为什么要真起内核：这三个工具的价值全在"和库里的真实数据对上"。
 * 用假 HTTP 只能测到参数拼装，测不到软删真的把 FTS 和状态一起收拾干净。
 */
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { apply } from '../lib/index.js'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function createHarness() {
  const logs = []
  const sections = []
  const tools = []
  const handlers = new Map()
  const effects = []
  const ctx = {
    logger: {
      debug: (m) => logs.push(['debug', m]),
      info: (m) => logs.push(['info', m]),
      warn: (m) => logs.push(['warn', m]),
      error: (m) => logs.push(['error', m]),
    },
    effect(fn) {
      const dispose = fn()
      effects.push(dispose)
      return dispose
    },
    inject(deps, cb) {
      if (deps.includes('systemPrompt')) {
        cb({ get: () => ({ section: (spec) => sections.push(spec) }) })
      }
      if (deps.includes('tools')) {
        cb({ tools: { register: (tool) => { tools.push(tool); return () => {} } } })
      }
    },
    on(event, cb) {
      handlers.set(event, cb)
    },
  }
  return {
    ctx, logs, sections, tools, handlers,
    close: () => {
      for (const dispose of effects.reverse()) {
        try { dispose?.() } catch { /* 已释放 */ }
      }
    },
  }
}

async function waitFor(fn, timeoutMs = 10000) {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    if (fn()) return true
    await sleep(100)
  }
  return false
}

test('tools: search / forget / status work against a real kernel', async (t) => {
  const harness = createHarness()
  const port = 8792
  const dataDir = mkdtempSync(join(tmpdir(), 'xujie-tools-test-'))
  const dbPath = join(dataDir, 'memory.db')
  apply(harness.ctx, {
    endpoint: `http://127.0.0.1:${port}`,
    dbPath,
    scope: 'session',
    pythonPath: process.env.PYTHON,
  })
  t.after(() => {
    harness.close()
    rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 })
  })

  const ready = await waitFor(() =>
    harness.logs.some(([, m]) => m.includes('内核就绪') || m.includes('已在运行')),
  )
  assert.equal(ready, true, `kernel not ready; logs=${JSON.stringify(harness.logs)}`)

  // 1) 三个工具都注册了，名字对得上
  assert.deepEqual(
    harness.tools.map((tool) => tool.name).sort(),
    ['memory_forget', 'memory_search', 'memory_status'],
  )
  const byName = Object.fromEntries(harness.tools.map((tool) => [tool.name, tool]))

  // 2) 先让内核里有一条真记忆
  harness.handlers.get('session/event')?.(
    { id: 's1' },
    { type: 'user/message', data: { role: 'user', content: [{ type: 'text', text: '我欠管家99元' }], source: { kind: 'user' } } },
  )
  const tokenFile = join(dataDir, 'token')
  const stored = await waitFor(() => existsSync(dbPath) && existsSync(tokenFile))
  assert.equal(stored, true, 'kernel did not create db/token')
  const token = readFileSync(tokenFile, 'utf8').trim()
  assert.ok(token.length > 0)
  await sleep(600) // 等 /turn 落库

  const exec = { agent: { id: 'agent-different-from-session', session: { id: 's1' } } }

  const status1 = await byName.memory_status.execute({}, exec)
  assert.equal(status1.ok, true)
  assert.ok(status1.traces >= 1, `expected >=1 trace, got ${status1.traces}`)
  assert.equal(status1.kernel.ok, true, 'health should be ok')

  const found = await byName.memory_search.execute({ query: '管家' }, exec)
  assert.equal(found.ok, true)
  assert.ok(found.hits.length >= 1)
  assert.ok(found.hits.some((hit) => hit.content.includes('管家')))
  const traceId = found.hits[0].trace_id
  assert.match(traceId, /^tr_/)

  // 3) 删除这条：软删 + 依赖状态失效
  assert.ok(harness.sections[0].text({ agent: { session: { id: 's1' } } }).includes('管家'))
  const erased = await byName.memory_forget.execute({ trace_id: traceId }, exec)
  assert.equal(harness.sections[0].text({ agent: { session: { id: 's1' } } }), '')
  assert.equal(erased.erased, true)
  assert.equal(erased.already_erased, false)
  assert.ok(Array.isArray(erased.state_keys))

  const after = await byName.memory_search.execute({ query: '管家' }, exec)
  assert.equal(after.hits.length, 0, 'erased memory must not be searchable')

  const status2 = await byName.memory_status.execute({}, exec)
  assert.ok(status2.traces_erased >= 1, `expected erased>=1, got ${status2.traces_erased}`)
  assert.ok(status2.traces < status1.traces || status1.traces === 1)

  // 4) 删不存在的 trace_id：报错，不静默成功
  await assert.rejects(
    () => byName.memory_forget.execute({ trace_id: 'tr_does_not_exist' }, exec),
    /trace_not_found/,
  )

  // 5) 参数校验：空 query 直接拒绝
  await assert.rejects(() => byName.memory_search.execute({ query: '  ' }, exec), /query 不能为空/)
})
