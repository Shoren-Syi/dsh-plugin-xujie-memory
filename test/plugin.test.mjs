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
