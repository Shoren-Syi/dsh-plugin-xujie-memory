// 预算守门：拦 QQ 会话的每次模型调用，预留 → 用量完整才结算
import assert from 'node:assert/strict'
import test from 'node:test'
import { guardQQStream } from '../lib/qq/cost-guard.js'

const BINDING = { scope: '10001', activity: 'chat', provider: 'p', model: 'm', maxTokens: 256 }

function setup({ usage = { inputTokens: 100, outputTokens: 50, cacheReadTokens: 0 }, info = { context: { contextWindow: 8000 } }, reserveThrow = null } = {}) {
  const calls = { reserve: [], settle: [], errors: [] }
  const ledger = {
    async reserve(...args) {
      calls.reserve.push(args)
      if (reserveThrow) throw new Error(reserveThrow)
      return { reserved: true, inputLimit: args[2], outputLimit: args[3] }
    },
    async settle(call, input, output, cached) { calls.settle.push({ call, input, output, cached }) },
  }
  const llm = { async resolveModelInfo() { return info } }
  const sessions = new Map([['sess-1', BINDING]])
  const onError = (error) => calls.errors.push(error)
  const guard = guardQQStream({ llm, ledger, sessions, onError })
  return { guard, calls, sessions }
}

async function drain(iter) {
  const out = []
  for await (const chunk of iter) out.push(chunk)
  return out
}

test('预留 → 转发 → 用量完整时结算', async () => {
  const { guard, calls } = setup()
  async function* upstream() {
    yield { type: 'text', text: 'hi' }
    yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 50, cacheReadTokens: 20 } }
  }
  const out = await drain(guard({ sessionId: 'sess-1', provider: 'p', model: 'm', maxTokens: 256 }, upstream))
  assert.equal(out.length, 2)
  assert.equal(calls.reserve.length, 1)
  assert.deepEqual(calls.reserve[0], ['10001', 'chat', 8000, 256])
  assert.deepEqual(calls.settle, [{ call: { reserved: true, inputLimit: 8000, outputLimit: 256 }, input: 100, output: 50, cached: 20 }])
  assert.deepEqual(calls.errors, [])
})

test('用量不完整：不结算，报错但保留预留', async () => {
  const { guard, calls } = setup()
  async function* upstream() {
    yield { type: 'text', text: 'hi' }
  }
  await drain(guard({ sessionId: 'sess-1', provider: 'p', model: 'm', maxTokens: 256 }, upstream))
  assert.equal(calls.settle.length, 0, '没有用量就不该结算')
  assert.equal(calls.errors.length, 1)
  assert.match(calls.errors[0].message, /用量不完整/)
})

test('模型不匹配 / 超过输出上限 / 上下文未知 → 直接拒绝，不发请求', async () => {
  const { guard } = setup()
  let called = false
  async function* upstream() { called = true; yield { type: 'text', text: 'x' } }
  await assert.rejects(() => drain(guard({ sessionId: 'sess-1', provider: 'x', model: 'm', maxTokens: 256 }, upstream)), /模型与预算配置不匹配/)
  await assert.rejects(() => drain(guard({ sessionId: 'sess-1', provider: 'p', model: 'm', maxTokens: 999 }, upstream)), /超过 QQ 设置上限/)
  const noCtx = setup({ info: {} })
  await assert.rejects(() => drain(noCtx.guard({ sessionId: 'sess-1', provider: 'p', model: 'm', maxTokens: 256 }, upstream)), /上下文上限未知/)
  assert.equal(called, false)
})

test('非 QQ 会话直接放行，不碰账本', async () => {
  const { guard, calls } = setup()
  async function* upstream() { yield { type: 'text', text: 'x' } }
  await drain(guard({ sessionId: 'other', provider: 'p', model: 'm', maxTokens: 256 }, upstream))
  assert.equal(calls.reserve.length, 0)
})
