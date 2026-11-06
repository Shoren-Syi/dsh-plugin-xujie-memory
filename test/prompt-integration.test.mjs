import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply, __test } from '../lib/index.js'

async function freePort() {
  const server = createServer()
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise(resolve => server.close(resolve))
  return port
}

test('cold start assembles current inbox query before prompt is sent', { timeout: 20000 }, async t => {
  const dir = mkdtempSync(join(tmpdir(), 'hippo-prompt-'))
  const db = join(dir, 'mem.db')
  const python = __test.resolvePython({})
  const setup = spawnSync(python, ['-c', [
    'import sys', 'from hippo.config import Config', 'from hippo.core.store import Store',
    'cfg=Config(); cfg.db_path=sys.argv[1]',
    's=Store.open(cfg.db_path,cfg)',
    "s.add_event(session='session-1', raw='The code is BLUE-PINEAPPLE', summary='The code is BLUE-PINEAPPLE', keywords='code BLUE-PINEAPPLE', pinned=1)",
    's.close()',
  ].join('; '), db], { cwd: __test.PACKAGE_ROOT, encoding: 'utf8', env: { ...process.env, HIPPO_LOG: 'off' } })
  assert.equal(setup.status, 0, setup.stderr)
  const callbacks = new Map(), disposers = []
  let section
  const ctx = {
    on(event, callback) { callbacks.set(event, callback) },
    get(service) { return service === 'systemPrompt' ? { section: x => { section = x } } : undefined },
    inject(services, callback) { if (services.includes('systemPrompt')) callback(this) },
    effect(callback) { disposers.push(callback()) },
  }
  apply(ctx, { dbPath: db, port: await freePort(), pythonPath: python, apiKey: 'test-key' })
  t.after(async () => {
    for (const dispose of disposers.reverse()) if (typeof dispose === 'function') await dispose()
    rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  })
  const agent = { id: 'session-1', session: { id: 'session-1' } }
  callbacks.get('agent/inbox/inserted')({ agent, message: { content: 'What is the code?' } })
  const result = await callbacks.get('system-prompt/assemble')({}, { agent }, async () => ({
    sections: [{ name: section.name, text: section.text({ agent }) }], contexts: [], tools: [], variables: {},
  }))
  assert.match(result.sections[0].text, /BLUE-PINEAPPLE/)
  assert.equal(result.sections[0].interpolate, false)
})
