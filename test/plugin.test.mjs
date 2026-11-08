import test from 'node:test'
import assert from 'node:assert/strict'
import { __test } from '../lib/index.js'

test('sessionKey：字符串 / 对象 / 空值', () => {
  assert.equal(__test.sessionKey('s1'), 's1')
  assert.equal(__test.sessionKey({ id: 'abc' }), 'abc')
  assert.equal(__test.sessionKey({ sessionId: 'xyz' }), 'xyz')
  assert.equal(__test.sessionKey(null), 'main')
  assert.equal(__test.sessionKey(undefined), 'main')
})

test('extractText：content 数组 / 字符串 / 空', () => {
  assert.equal(
    __test.extractText({ content: [{ type: 'text', text: '你好' }, { type: 'text', text: '世界' }] }),
    '你好世界',
  )
  assert.equal(__test.extractText({ content: 'abc' }), 'abc')
  assert.equal(__test.extractText({ message: { content: [{ type: 'text', text: '助手回复' }, { type: 'thinking', text: '隐私' }, { type: 'tool_call', text: '不应记录' }] } }), '助手回复')
  assert.equal(__test.extractText({ content: [{ type: 'image' }] }), '')
  assert.equal(__test.extractText(null), '')
  assert.equal(__test.extractText({}), '')
})

test('expandUser：~ 与绝对路径', () => {
  assert.ok(__test.expandUser('~/x').endsWith('x'))
  assert.equal(__test.expandUser('C:\\a\\b'), 'C:\\a\\b')
  assert.equal(__test.expandUser(''), '')
})

test('resolvePython：能找到解释器', () => {
  const p = __test.resolvePython({})
  assert.equal(typeof p, 'string')
  assert.ok(p.length > 0)
})

test('resolveApiKey：配置优先', () => {
  assert.equal(__test.resolveApiKey({ apiKey: 'sk-conf' }), 'sk-conf')
})

test('resolveApiKey：环境变量兜底', () => {
  process.env.HIPPO_API_KEY = 'sk-env-probe'
  try {
    assert.equal(__test.resolveApiKey({}), 'sk-env-probe')
  } finally {
    delete process.env.HIPPO_API_KEY
  }
})

test('DSH credentials service resolves key without reading its private file', async () => {
  const previousHippo = process.env.HIPPO_API_KEY
  const previousDeepseek = process.env.DEEPSEEK_API_KEY
  delete process.env.HIPPO_API_KEY
  delete process.env.DEEPSEEK_API_KEY
  try {
    const seen = []
    const ctx = { get(name) { assert.equal(name, 'credentials'); return { async resolve(ref) { seen.push(ref); return { value: 'sk-from-service' } } } } }
    assert.equal(await __test.resolveHostApiKey(ctx), 'sk-from-service')
    assert.deepEqual(seen, ['DEEPSEEK_API_KEY'])
    assert.equal(await __test.resolveHostApiKey(ctx, { apiKey: 'sk-explicit' }), 'sk-explicit')
    assert.equal(seen.length, 1)
  } finally {
    if (previousHippo !== undefined) process.env.HIPPO_API_KEY = previousHippo
    if (previousDeepseek !== undefined) process.env.DEEPSEEK_API_KEY = previousDeepseek
  }
})

test('portFree：空闲端口 true / 已占用 false', async () => {
  const { createServer } = await import('node:net')
  const server = createServer()
  await new Promise((resolve) => server.listen({ host: '127.0.0.1', port: 0 }, resolve))
  const taken = server.address().port
  try {
    assert.equal(await __test.portFree(taken), false, '已监听的端口不该判为空闲')
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('pickPort：跳过被占用的端口，不返回 0', async () => {
  const { createServer } = await import('node:net')
  // 占住一个端口，然后让 pickPort 从这个端口开始找。
  const server = createServer()
  await new Promise((resolve) => server.listen({ host: '127.0.0.1', port: 0 }, resolve))
  const taken = server.address().port
  try {
    const picked = await __test.pickPort(taken, 5)
    assert.ok(picked > 0, '应当找到可用端口')
    assert.notEqual(picked, taken, '不能把被占用的端口当结果')
    assert.equal(await __test.portFree(picked), true)
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('withRetry：传输失败重试一次，超时不重试', async () => {
  let calls = 0
  const flaky = async () => {
    calls += 1
    if (calls === 1) throw new TypeError('fetch failed')
    return { ok: true }
  }
  assert.deepEqual(await __test.withRetry(flaky), { ok: true })
  assert.equal(calls, 2)

  let aborted = 0
  const timeout = async () => {
    aborted += 1
    const error = new Error('aborted')
    error.name = 'AbortError'
    throw error
  }
  await assert.rejects(() => __test.withRetry(timeout), /aborted/)
  assert.equal(aborted, 1, '超时不该重试')
})

test('createTurnQueue：失败重试、成功清空、有界丢弃最旧', async () => {
  const sent = []
  let failing = true
  let scheduled = null
  const q = __test.createTurnQueue({
    send: async (item) => {
      if (failing) throw new Error('down')
      sent.push(item)
    },
    delayMs: 1,
    max: 3,
    timer: (fn) => { scheduled = fn; return { unref() {} } },
  })

  q.push({ text: 'a' })
  await q.flush()
  assert.equal(q.size, 1, '失败后应留在队列里')
  assert.equal(sent.length, 0)

  failing = false
  scheduled?.()
  await new Promise((r) => setTimeout(r, 5))
  assert.equal(q.size, 0, '恢复后应该清空')
  assert.deepEqual(sent, [{ text: 'a' }])

  q.push({ text: '1' })
  q.push({ text: '2' })
  q.push({ text: '3' })
  q.push({ text: '4' })
  assert.equal(q.size, 3, '超过上限丢最旧的')
  await q.flush()
  assert.deepEqual(sent.slice(-3).map((x) => x.text), ['2', '3', '4'])
})
