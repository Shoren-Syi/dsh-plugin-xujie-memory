import { readFile } from 'node:fs/promises'

/** Register only on DSH Connection's owner-authenticated Fetch channel. */
export function registerBoardRoutes(connection, { boardPath, load }) {
  const routes = [
    {
      path: '/api/hippo.board', methods: ['GET'], requestBody: 'buffered',
      fetch: async () => {
        try {
          return new Response(await readFile(boardPath, 'utf8'), {
            headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' },
          })
        } catch {
          return new Response('记忆看板暂不可用', { status: 503 })
        }
      },
    },
    {
      path: '/api/hippo.board.data', methods: ['GET'], requestBody: 'buffered',
      fetch: async () => {
        try {
          return Response.json(await load(), { headers: { 'cache-control': 'no-store' } })
        } catch {
          return Response.json({ ok: false, error: '记忆内核暂不可用' }, { status: 503 })
        }
      },
    },
  ]
  const dispose = []
  try {
    for (const route of routes) dispose.push(connection.fetch.register(route))
  } catch (error) {
    for (const remove of dispose) void remove()
    throw error
  }
  return async () => { await Promise.all(dispose.map(remove => remove())) }
}
