// 队列落盘：重启不丢待补写回合
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { appendPending, loadPending, rewritePending } from '../lib/pending-store.js'
import { createTurnQueue } from '../lib/index.js'

function tempFile() {
  const dir = mkdtempSync(join(tmpdir(), 'hippo-pending-'))
  return join(dir, 'pending-turns.jsonl')
}

test('落盘：追加 → 读取 → 重写', () => {
  const path = tempFile()
  appendPending(path, { session: 's1', role: 'user', text: '第一句', at: 1 })
  appendPending(path, { session: 's1', role: 'assistant', text: '第二句', at: 2 })
  assert.deepEqual(loadPending(path).map((x) => x.text), ['第一句', '第二句'])

  rewritePending(path, [{ session: 's1', role: 'user', text: '留一条', at: 3 }])
  assert.deepEqual(loadPending(path).map((x) => x.text), ['留一条'])
})

test('坏行不影响其它行；非法结构被丢弃', () => {
  const path = tempFile()
  writeFileSync(path, [
    '{"session":"s1","role":"user","text":"好的","at":1}',
    '{坏行',
    '{"session":"s1","role":"system","text":"不合法角色"}',
    '{"session":"s1","role":"assistant","text":"也行","at":2}',
  ].join('\n'), 'utf8')
  assert.deepEqual(loadPending(path).map((x) => x.text), ['好的', '也行'])
})

test('队列：初始项（重启后加载）能补写并清空文件', async () => {
  const path = tempFile()
  const sent = []
  const items = loadPending(path)
  const q = createTurnQueue({
    send: async (item) => { sent.push(item) },
    initial: [{ session: 's', role: 'user', text: '重启前没写进去', at: 1 }],
    onAdd: (item) => appendPending(path, item),
    onRemove: () => rewritePending(path, q.items()),
    timer: (fn) => { void fn(); return { unref() {} } },
  })
  assert.equal(items.length, 0)
  assert.equal(q.size, 1, '初始项应在队列里')
  await q.flush()
  assert.deepEqual(sent.map((x) => x.text), ['重启前没写进去'])
  assert.equal(loadPending(path).length, 0, '补写成功后文件应清空')
})

test('队列：新项立刻落盘，失败保留', async () => {
  const path = tempFile()
  const q = createTurnQueue({
    send: async () => { throw new Error('kernel down') },
    onAdd: (item) => appendPending(path, item),
    onRemove: () => rewritePending(path, q.items()),
    timer: () => ({ unref() {} }),
  })
  q.push({ session: 's', role: 'user', text: '要活下来的话', at: 1 })
  await q.flush()
  assert.equal(loadPending(path).length, 1, '失败后文件里应保留')
  assert.equal(q.size, 1)
})

test('读文件：不存在或空文件都返回空数组', () => {
  assert.deepEqual(loadPending(tempFile()), [])
})
