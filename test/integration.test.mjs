import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { __test } from '../lib/index.js'

const PY = __test.resolvePython({})
const PORT = 8791
const BASE = `http://127.0.0.1:${PORT}`

async function waitHealth(ms = 12000) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${BASE}/health`)
      if (r.ok) return await r.json()
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  return null
}

async function postJson(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  return r.json()
}

test('真内核 HTTP 往返：health → event → assemble', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'hippo-integ-'))
  const db = join(dir, 'mem.db')
  const child = spawn(PY, ['-m', 'hippo.dsh_kernel', '--db', db, '--port', String(PORT)], {
    env: { ...process.env, PYTHONPATH: __test.PACKAGE_ROOT, HIPPO_LOG: 'off', PYTHONIOENCODING: 'utf-8', HIPPO_KERNEL_INSTANCE: 'integration-instance', HIPPO_EXTRACT_API_KEY: 'integration-test-key' },
    stdio: 'ignore',
  })
  t.after(() => {
    try {
      child.kill()
    } catch {
      /* 已退出 */
    }
  })

  const health = await waitHealth()
  assert.ok(health, '内核未在超时内就绪')
  assert.equal(health.ok, true)
  assert.equal(health.instance, 'integration-instance')
  assert.equal(health.extract, true)

  const ev = await postJson('/event', { session: 's1', role: 'user', text: '我叫张三，住在杭州，对花生过敏。' })
  assert.equal(ev.ok, true)
  const ev2 = await postJson('/event', { session: 's1', role: 'assistant', text: '记住了。' })
  assert.equal(ev2.ok, true)

  const asm = await postJson('/assemble', { session: 's1', text: '我住哪？' })
  assert.equal(asm.ok, true)
  assert.ok(typeof asm.text === 'string')
  assert.ok(asm.tokens > 0, '组装至少要有人设/时间锚 token')

  const empty = await postJson('/event', { session: 's1', role: 'user', text: '   ' })
  assert.equal(empty.skipped, 'empty')
})
