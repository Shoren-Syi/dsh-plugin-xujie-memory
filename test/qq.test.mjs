import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'

import { QQInbound } from '../lib/qq/inbound.js'
import { QQTransport } from '../lib/qq/transport.js'
import { QQBudgetLedger } from '../lib/qq/budget-ledger.js'

const gate = () => new QQInbound({ selfId: '10001', privateAllow: ['20002'], groupAllow: ['30003'], requireMention: true })
const priv = (id = '20002', text = '你好') => ({
  post_type: 'message', message_type: 'private', self_id: 10001,
  user_id: Number(id), message_id: 'm1', message: [{ type: 'text', data: { text } }],
})

test('inbound：白名单私聊可入，陌生人丢弃', () => {
  const g = gate()
  const msg = g.accept(priv())
  assert.equal(msg.channel, 'private')
  assert.equal(msg.actor, '20002')
  assert.equal(g.accept({ ...priv('29999'), message_id: 'm2' }), null)
})

test('inbound：群聊必须 @ 机器人', () => {
  const g = gate()
  const group = {
    post_type: 'message', message_type: 'group', self_id: 10001, group_id: 30003,
    user_id: 20002, message_id: 'g1', message: [{ type: 'text', data: { text: '在吗' } }],
  }
  assert.equal(g.accept(group), null, '没 @ 不该进')
  const mentioned = { ...group, message_id: 'g2', message: [
    { type: 'at', data: { qq: '10001' } }, { type: 'text', data: { text: '在吗' } }] }
  assert.equal(g.accept(mentioned).conversation, '30003')
})

test('inbound：同一条消息去重', () => {
  const g = gate()
  assert.ok(g.accept(priv()))
  assert.equal(g.accept(priv()), null)
})

test('inbound：自己的消息、超长消息、非法结构都挡', () => {
  const g = gate()
  assert.equal(g.accept({ ...priv(), user_id: 10001, message_id: 'x' }), null)
  assert.throws(() => g.accept(priv('20002', 'x'.repeat(2001))))
  assert.equal(g.accept({ ...priv(), message_id: 'y', message: 'not-array' }), null)
})

test('transport：私聊发送的参数形状', async () => {
  const seen = []
  const server = http.createServer((req, res) => {
    let body = ''
    req.on('data', c => { body += c })
    req.on('end', () => {
      seen.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(body) })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 1 } }))
    })
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  const port = server.address().port
  try {
    const t = new QQTransport(`http://127.0.0.1:${port}`, 'tok')
    await t.message('private', '20002', '收到')
    assert.equal(seen[0].url, '/send_private_msg')
    assert.equal(seen[0].auth, 'Bearer tok')
    assert.deepEqual(seen[0].body, { user_id: '20002', message: [{ type: 'text', data: { text: '收到' } }] })
    await t.message('group', '30003', '在')
    assert.equal(seen[1].url, '/send_group_msg')
    assert.deepEqual(seen[1].body.group_id, '30003')
  } finally {
    server.close()
  }
})

test('transport：拒绝不安全地址与非法入参', async () => {
  assert.throws(() => new QQTransport('ftp://x'))
  assert.throws(() => new QQTransport('http://u:p@x'))
  const t = new QQTransport('http://127.0.0.1:3000')
  await assert.rejects(() => t.message('private', 'abc', 'x'))
  await assert.rejects(() => t.message('room', '123', 'x'))
  await assert.rejects(() => t.message('private', '123', '   '))
  await assert.rejects(() => t.publish('x', { visibility: 16, targets: [] }))
})

test('ledger：把内核拒绝翻译成人能读的错误', async () => {
  const ledger = new QQBudgetLedger(async () => ({ ok: false, error: '已达到每日预算上限' }))
  await assert.rejects(() => ledger.reserve('1', 'chat', 100, 100), /每日预算上限/)
  const bad = new QQBudgetLedger(async () => ({ ok: true }))
  await assert.rejects(() => bad.reserve('1', 'chat', 100, 100), /预算预留未确认/)
})

test('ledger：结算返回金额', async () => {
  const ledger = new QQBudgetLedger(async (path) => path.endsWith('settle')
    ? { ok: true, amount: 42 } : { ok: true, call_id: 'c1' })
  const call = await ledger.reserve('1', 'chat', 100, 100)
  assert.deepEqual(call, { scope: '1', id: 'c1' })
  assert.equal(await ledger.settle(call, 10, 10, 0), 42)
})
