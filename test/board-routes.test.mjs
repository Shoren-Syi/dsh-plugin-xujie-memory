import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { registerBoardRoutes } from '../lib/board-routes.js'

test('board routes return real data, fail closed, and dispose', async () => {
  const routes = new Map()
  const connection = { fetch: { register(route) {
    routes.set(route.path, route)
    return async () => routes.delete(route.path)
  } } }
  let available = true
  const dispose = registerBoardRoutes(connection, {
    boardPath: new URL('../hippo/data/board.html', import.meta.url),
    load: async () => { if (!available) throw new Error('internal secret'); return { ok: true, counts: { events: 7 } } },
  })
  const page = await routes.get('/api/hippo.board').fetch()
  assert.equal(page.status, 200)
  assert.match(await page.text(), /记忆看板/)
  assert.equal((await (await routes.get('/api/hippo.board.data').fetch()).json()).counts.events, 7)
  available = false
  const failure = await routes.get('/api/hippo.board.data').fetch()
  assert.equal(failure.status, 503)
  assert.doesNotMatch(await failure.text(), /internal secret/)
  await dispose()
  assert.equal(routes.size, 0)
})

test('client registers DSH panel and launcher', async () => {
  const source = await readFile(new URL('../lib/client.js', import.meta.url), 'utf8')
  let plugin
  const react = { createElement: (type, props) => ({ type, props }) }
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load: ({ factory }) => { plugin = factory(() => react) } } } })
  const registrations = []
  plugin.apply({ slots: { inject: (_slot, callback) => callback(), register: (spec, component) => registrations.push({ spec, component }) } })
  assert.equal(registrations.length, 2)
  assert.equal(registrations[0].component().props.src, '/api/hippo.board')
  assert.equal(registrations[1].spec.label, '记忆')
})
