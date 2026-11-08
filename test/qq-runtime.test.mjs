// QQRuntime 端到端（桩）：收消息 → 驱动假会话 → 经假 OneBot 发出回复
import assert from 'node:assert/strict'
import test from 'node:test'
import { QQRuntime } from '../lib/qq/runtime.js'

const CONFIG = {
  selfId: '10001', privateAllow: ['20002'], groupAllow: ['30003'],
  requireMention: true, provider: 'p', model: 'm', maxTokens: 256, budgetScope: '10001',
}

function makeHarness({ reply = '收到啦', failCreate = false } = {}) {
  const handlers = { 'llm/stream': [], 'session/event': [] }
  const calls = { followups: [], messages: [], publishes: [], created: [], disposed: [] }
  let pending = null

  const ctx = {
    llm: {},
    agents: {
      async create({ sessionId }) {
        if (failCreate) throw new Error('创建会话失败')
        calls.created.push(sessionId)
        return {
          agent: {
            followup(text) {
              calls.followups.push(text)
              pending = () => {
                for (const fn of handlers['session/event']) {
                  fn({ id: sessionId }, {
                    type: 'assistant/message',
                    data: { message: { content: [{ type: 'text', text: reply }] } },
                  })
                }
              }
            },
            async whenIdle() { pending?.(); pending = null },
            cancel() {},
          },
          async dispose() { calls.disposed.push(sessionId) },
        }
      },
      async resume() {
        const error = new Error('missing')
        error.name = 'SessionPersistenceNotFoundError'
        throw error
      },
    },
    on(type, fn) { handlers[type].push(fn); return () => {} },
  }

  const transport = {
    async message(kind, id, text) { calls.messages.push({ kind, id, text }) },
    async publish(text, opts) { calls.publishes.push({ text, opts }) },
  }

  const errors = []
  const runtime = new QQRuntime({
    ctx, config: CONFIG, ledger: {}, transport,
    makeMessage: (text) => ({ role: 'user', content: [{ type: 'text', text }] }),
    identities: new Map(),
    onError: (error) => errors.push(error),
  })
  return { runtime, calls, errors }
}

const privateEvent = {
  post_type: 'message', message_type: 'private', self_id: 10001,
  user_id: 20002, message: [{ type: 'text', data: { text: '在吗' } }], message_id: 1,
}

test('private 白名单消息 → 驱动会话 → 回复经 transport 发出', async () => {
  const { runtime, calls, errors } = makeHarness({ reply: '在的' })
  const accepted = await runtime.handle(privateEvent)
  assert.equal(accepted, true)
  assert.equal(calls.followups.length, 1, '应该把消息投给会话')
  assert.deepEqual(calls.messages, [{ kind: 'private', id: '20002', text: '在的' }])
  assert.deepEqual(errors, [])
  await runtime.close()
  assert.equal(calls.disposed.length, 1, '关闭时应 dispose 会话')
})

test('陌生人不进会话；群聊不 @ 也不进', async () => {
  const { runtime, calls } = makeHarness()
  const stranger = await runtime.handle({ ...privateEvent, user_id: 99999 })
  assert.equal(stranger, false)
  const group = await runtime.handle({
    post_type: 'message', message_type: 'group', self_id: 10001,
    group_id: 30003, user_id: 20002, message: [{ type: 'text', data: { text: '在吗' } }], message_id: 2,
  })
  assert.equal(group, false)
  assert.equal(calls.followups.length, 0)
  await runtime.close()
})

test('空间动态走 publish，且只发一条', async () => {
  const { runtime, calls } = makeHarness({ reply: '今天的感想' })
  runtime.config.schedule = { qzone: { enabled: true, intervalMinutes: 60, visibility: 64 } }
  const message = { channel: 'qzone', conversation: '10001', actor: '10001', text: '写一句感想' }
  await runtime.run(message, 'qzone', 'runtime-context')
  assert.equal(calls.publishes.length, 1)
  assert.equal(calls.publishes[0].opts.visibility, 64)
  assert.equal(calls.messages.length, 0, '空间动态不该走私聊通道')
  await runtime.close()
})

test('会话创建失败 → onError 收到错误，不崩溃', async () => {
  const { runtime, errors } = makeHarness({ failCreate: true })
  await assert.rejects(() => runtime.handle(privateEvent), /创建会话失败/)
  await runtime.close()
})
