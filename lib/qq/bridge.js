/** QQ 桥接的"配置轮询 → 建/拆运行时"逻辑（从 index.js 抽出来，便于测试）。
 *
 * 行为（和原来的内联实现一致）：
 * - 每 `intervalMs`（默认 5 秒）读一次 `/qq/settings/active`；
 * - revision 没变 → 什么都不做（不重建，不重连）；
 * - revision 变了 → 关掉旧运行时，再按新配置建一个新的；
 * - 配置被删（settings=null）→ 关掉旧运行时，不建新的；
 * - 建失败 → 关掉半成品、清空 revision，等下一轮再试。
 *
 * 依赖全部注入：`fetchJson` / `ensureReady` / `createRuntime` / 日志，
 * 这样测试可以直接给假实现，不需要真 DSH 服务。
 */
export function createQQBridge({
  fetchJson,
  ensureReady = async () => true,
  createRuntime,
  log = () => {},
  warn = () => {},
  identities,
  intervalMs = 5000,
  timer = setInterval,
  clearTimer = clearInterval,
  timeoutMs = 3000,
}) {
  if (typeof fetchJson !== 'function' || typeof createRuntime !== 'function') {
    throw new TypeError('QQ 桥接缺少 fetchJson 或 createRuntime')
  }
  let runtime = null
  let revision = ''
  let disposed = false
  let refreshing = false

  async function refresh() {
    if (refreshing || disposed) return
    refreshing = true
    try {
      if (!(await ensureReady())) return
      const response = await fetchJson('/qq/settings/active', undefined, timeoutMs)
      if (response?.ok !== true) throw new Error('读取 QQ 配置失败')
      const qq = response.settings
      if ((qq?.revision ?? '') === revision) return
      if (runtime) {
        await runtime.close()
        runtime = null
        revision = ''
      }
      if (!qq || disposed) return
      const next = await createRuntime(qq, { identities })
      if (disposed) {
        await next?.close?.()
        return
      }
      try {
        next.connect()
      } catch (error) {
        await next.close()
        throw error
      }
      runtime = next
      revision = qq.revision
      log('QQ 桥接已连接')
    } catch (error) {
      if (runtime) {
        await runtime.close().catch(() => {})
        runtime = null
        revision = ''
      }
      if (!disposed) warn(`QQ 桥接未就绪：${error instanceof Error ? error.message : '未知错误'}`)
    } finally {
      refreshing = false
    }
  }

  function start() {
    const handle = timer(() => { void refresh() }, intervalMs)
    handle?.unref?.()
    void refresh()
    return handle
  }

  async function dispose() {
    disposed = true
    if (runtime) {
      await runtime.close().catch(() => {})
      runtime = null
    }
  }

  return {
    refresh,
    start,
    dispose,
    get runtime() { return runtime },
    get revision() { return revision },
  }
}
