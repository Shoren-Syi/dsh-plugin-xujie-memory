/**
 * 回声测试（node --test）：刚说的话不能当"旧事"喂回来。
 *
 * `/turn` 靠 trace_id 挡；`/context` 只拿得到文本，只能按内容挡。
 * 两条路都过 `_recall`，所以断在两处：同句、同句不同标点。
 * 同时验证"不相关的话"时旧记忆照常出现——消回声不能把记忆一起掐死。
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
      if (deps.includes('tools')) cb({ tools: { register: () => () => {} } })
    },
    on(event, cb) { handlers.set(event, cb) },
  }
  return {
    ctx, logs, sections, handlers,
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

function caller(port, token) {
  return async (path, payload) => {
    const resp = await fetch(`http://127.0.0.1:${port}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Xujie-Token': token },
      body: JSON.stringify(payload),
    })
    return resp.json()
  }
}

test('echo: current sentence is never injected back as memory', async (t) => {
  const harness = createHarness()
  const port = 8793
  const dataDir = mkdtempSync(join(tmpdir(), 'xujie-echo-test-'))
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
  const tokenFile = join(dataDir, 'token')
  assert.equal(await waitFor(() => existsSync(tokenFile)), true)
  await sleep(200)
  const call = caller(port, readFileSync(tokenFile, 'utf8').trim())
  const session = { session: 's1', user: 'user' }

  // 1) 写入这句话：这一轮的材料里不能出现它自己
  const turn = await call('/turn', { ...session, text: '我不吃辣' })
  assert.equal(turn.ok, true, JSON.stringify(turn))
  assert.ok(!turn.context.includes('我不吃辣'), `turn echoed itself:\n${turn.context}`)

  // 2) 只读组装：同一句传进去，也不能被当成「相关的旧事/最近说过的话」
  const same = await call('/context', { ...session, text: '我不吃辣' })
  assert.equal(same.ok, true, JSON.stringify(same))
  assert.ok(!same.context.includes('我不吃辣'), `context echoed the same sentence:\n${same.context}`)

  // 3) 标点/空白不同也算同一句
  const punct = await call('/context', { ...session, text: ' 我不吃辣。 ' })
  assert.ok(!punct.context.includes('我不吃辣'), `punctuation variant echoed:\n${punct.context}`)

  // 4) 反证：问别的事时，旧记忆必须照常出现（消回声不等于消记忆）
  const other = await call('/context', { ...session, text: '今天天气怎么样' })
  assert.ok(other.context.includes('我不吃辣'), `related memory lost:\n${other.context}`)
})
