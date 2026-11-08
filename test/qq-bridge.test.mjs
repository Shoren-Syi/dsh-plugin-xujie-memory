// QQ 桥接：改完配置后 5 秒内是否真的重建（不重建 / 误重建都要挡住）
import assert from 'node:assert/strict'
import test from 'node:test'
import { createQQBridge } from '../lib/qq/bridge.js'

const QQ_A = { revision: 'rev-A', wsUrl: 'ws://a', httpUrl: 'http://a', provider: 'p', model: 'm' }
const QQ_B = { revision: 'rev-B', wsUrl: 'ws://b', httpUrl: 'http://b', provider: 'p', model: 'm' }

function harness(sequence) {
  const state = { calls: 0, created: [], closed: [], connected: [], warned: [], timers: [] }
  const fetchJson = async (path) => {
    assert.equal(path, '/qq/settings/active')
    const settings = sequence[Math.min(state.calls, sequence.length - 1)]
    state.calls += 1
    return { ok: true, settings }
  }
  const bridge = createQQBridge({
    fetchJson,
    identities: new Map(),
    log: () => {},
    warn: (msg) => state.warned.push(msg),
    createRuntime: async (qq) => {
      state.created.push(qq.revision)
      return {
        connect() { state.connected.push(qq.revision) },
        async close() { state.closed.push(qq.revision) },
      }
    },
    timer: (fn, ms) => { state.timers.push(ms); return { unref() {} } },
    clearTimer: () => {},
  })
  return { bridge, state }
}

test('配置出现→建；revision 没变→不重建', async () => {
  const { bridge, state } = harness([null, QQ_A, QQ_A])
  await bridge.refresh()
  assert.deepEqual(state.created, [], '初始没有配置就不该建')

  await bridge.refresh()
  assert.deepEqual(state.created, ['rev-A'])
  assert.deepEqual(state.connected, ['rev-A'])

  await bridge.refresh()
  assert.deepEqual(state.created, ['rev-A'], 'revision 没变不该重建')
  assert.deepEqual(state.closed, [])
})

test('revision 变了→先关旧的再建新的', async () => {
  const { bridge, state } = harness([QQ_A, QQ_B])
  await bridge.refresh()
  await bridge.refresh()
  assert.deepEqual(state.created, ['rev-A', 'rev-B'])
  assert.deepEqual(state.closed, ['rev-A'])
  assert.equal(bridge.revision, 'rev-B')
})

test('配置被删→关掉运行时，不建新的', async () => {
  const { bridge, state } = harness([QQ_A, null])
  await bridge.refresh()
  await bridge.refresh()
  assert.deepEqual(state.closed, ['rev-A'])
  assert.equal(bridge.runtime, null)
  assert.deepEqual(state.created, ['rev-A'])
})

test('创建失败→不留半成品、warn、下一轮能重试', async () => {
  let fail = true
  const state = { created: 0, closed: 0, warned: [] }
  const bridge = createQQBridge({
    fetchJson: async () => ({ ok: true, settings: QQ_A }),
    createRuntime: async () => {
      state.created += 1
      if (fail) throw new Error('连不上')
      return { connect() {}, async close() { state.closed += 1 } }
    },
    warn: (m) => state.warned.push(m),
    timer: () => ({ unref() {} }),
    clearTimer: () => {},
  })
  await bridge.refresh()
  assert.equal(state.warned.length, 1)
  assert.match(state.warned[0], /连不上/)
  assert.equal(bridge.runtime, null)

  fail = false
  await bridge.refresh()
  assert.equal(state.created, 2, '失败后下一轮应该重试')
  assert.ok(bridge.runtime)
})

test('默认轮询间隔是 5 秒，start 会立刻刷一次', async () => {
  const { bridge, state } = harness([QQ_A])
  bridge.start()
  assert.deepEqual(state.timers, [5000])
  await new Promise((r) => setTimeout(r, 10))
  assert.deepEqual(state.created, ['rev-A'])
})

test('dispose 后不再处理配置', async () => {
  const { bridge, state } = harness([QQ_A, QQ_B])
  await bridge.refresh()
  await bridge.dispose()
  const before = state.created.length
  await bridge.refresh()
  assert.equal(state.created.length, before)
})
