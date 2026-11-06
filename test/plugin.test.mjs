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
