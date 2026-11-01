/**
 * 插件冒烟测试（node --test）：
 *   1. 包内内核 + 包内解释器能拉起并就绪
 *   2. user/message 能写记忆，材料能按会话取回
 *   3. 多会话隔离：A 的材料不会出现在 B 的段落里
 *   4. source.kind 不是 user 的注入消息被跳过
 */
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
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
      // 每个 effect 都要留 disposer：插件现在有内核 + 工具两组，
      // 只存最后一个会把内核留成孤儿进程，占着端口和 sqlite 文件。
      const dispose = fn()
      effects.push(dispose)
      return dispose
    },
    inject(_deps, cb) {
      cb({ get: () => ({ section: (spec) => sections.push(spec) }) })
    },
    on(event, cb) {
      handlers.set(event, cb)
    },
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

async function waitFor(fn, timeoutMs = 8000) {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    if (fn()) return true
    await sleep(100)
  }
  return false
}

test('bundled kernel injects per-session memory and ignores injected context', async (t) => {
  const harness = createHarness()
  const port = 8791
  const dataDir = mkdtempSync(join(tmpdir(), 'xujie-plugin-test-'))
  const dbPath = join(dataDir, 'memory.db')
  apply(harness.ctx, { endpoint: `http://127.0.0.1:${port}`, dbPath, scope: 'session', pythonPath: process.env.PYTHON })
  t.after(() => {
    harness.close()
    // Windows 上内核进程刚被 kill 时还占着 sqlite 文件，重试几次再删。
    rmSync(dataDir, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 })
  })

  const ready = await waitFor(() =>
    harness.logs.some(([, m]) => m.includes('内核就绪') || m.includes('内核已在运行')),
  )
  assert.equal(ready, true, `kernel not ready; logs=${JSON.stringify(harness.logs)}`)
  assert.equal(harness.sections.length, 1)
  const section = harness.sections[0]
  assert.equal(section.name, 'xujie-memory')

  const emit = (sessionId, text, sourceKind = 'user') => {
    harness.handlers.get('session/event')?.(
      { id: sessionId },
      { type: 'user/message', data: { role: 'user', content: [{ type: 'text', text }], source: { kind: sourceKind } } },
    )
  }

  emit('s1', '我不吃辣')
  emit('s2', '我欠管家99元')
  const bothReady = await waitFor(() => {
    const a = section.text({ agent: { session: { id: 's1' } } }) || ''
    const b = section.text({ agent: { session: { id: 's2' } } }) || ''
    return a.includes('不吃辣') && b.length > 0 && a !== b
  })
  assert.equal(bothReady, true, `materials not ready; logs=${JSON.stringify(harness.logs)}`)
  // 内核材料是异步更新的；等一拍再快照，避免拿到中间态。
  await sleep(500)

  const s1 = section.text({ agent: { session: { id: 's1' } } })
  const s2 = section.text({ agent: { session: { id: 's2' } } })
  assert.ok(s1.includes('不吃辣'))
  assert.ok(!s1.includes('还不了解'), 'session s1 must not see s2 material')
  assert.ok(!s2.includes('不吃辣'), 'session s2 must not see s1 material')
  assert.notEqual(s1, s2)

  // 注入消息不是用户说的，不该改变任何会话的材料
  emit('s1', 'runtime context text', 'runtime-context')
  await sleep(300)
  assert.equal(section.text({ agent: { session: { id: 's1' } } }), s1)

  // 会话隔离模式下，没有会话上下文时不能返回其他会话的材料。
  const fallback = section.text(undefined)
  assert.equal(fallback, '')
})
