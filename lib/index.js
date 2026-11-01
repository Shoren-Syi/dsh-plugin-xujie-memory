/**
 * dsh-plugin-xujie-memory — 每轮自动记忆，模型不用问。
 *
 * 设计要点：
 *   1. 内核（Python）和解释器都**跟着插件走**：装完即用，不装 Python、
 *      不 pip install、不填 repoPath。
 *   2. `systemPrompt.section` 同步读缓存，`session/event` 异步写记忆。
 *   3. 任何失败都降级为「这一轮没有记忆」，绝不影响对话。
 *
 * v0.5 变化（稳定性）：
 *   - 内核有**看门狗**：崩了、被别的实例带走了，都会按退避自动拉起。
 *   - /health 核对**版本和库路径**：端口上是旧版/别人的内核就换端口，不再误连。
 *   - 内核带 `--parent-pid`：宿主崩溃也不会留下占端口的孤儿进程。
 *   - 内核日志写进 `~/.xujie-memory/kernel.log`（旧版 stdio:'ignore' 全丢）。
 *   - 请求带 token（`~/.xujie-memory/token`），本机网页无法往记忆里写东西。
 *   - 同一会话的请求按顺序串行，快速连发两句不会乱序。
 *   - 默认 `scope: user`：记忆跨会话有效；`scope: session` 恢复旧行为。
 *
 * 事件名已按真机核对：
 *   user/message      用户发言（真数据在 event.data.content 的 text 块里）
 *   其余类型、以及 source.kind 不是 user 的 user/message → 跳过
 */

import { MemoryController } from './controller.js'
export { MemoryController } from './controller.js'
import { spawn } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, openSync, closeSync, readFileSync, statSync, renameSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-plugin-xujie-memory'
export const inject = ['systemPrompt']

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const BUNDLED_KERNEL = join(PACKAGE_ROOT, 'python')
const DEFAULT_ENDPOINT = 'http://127.0.0.1:8765'
const TURN_TIMEOUT_MS = 4000
const TOOL_TIMEOUT_MS = 5000
const HEALTH_TIMEOUT_MS = 800
const READY_WAIT_MS = 10000
const WATCHDOG_MS = 5000
const PORT_TRIES = 10
const MAX_TEXT_CHARS = 2000
const MAX_SESSIONS = 32
const LOG_ROTATE_BYTES = 2 * 1024 * 1024

export function apply(ctx, config = {}) {
  // Per-plugin-instance caches: two profiles/databases never share material.
  const contexts = new Map()
  const pending = new Map()
  const latestByUser = new Map()
  const cacheState = { contexts, latestByUser, epoch: 0, disposed: false }
  let shapeLogged = false
  const configured = new URL((config.endpoint || DEFAULT_ENDPOINT).replace(/\/+$/, ''))
  const host = configured.hostname || '127.0.0.1'
  const basePort = Number(configured.port || 8765)
  const autoStart = config.autoStart !== false
  const maxAskedTotal = Number.isFinite(Number(config.maxAskedTotal)) ? Number(config.maxAskedTotal) : 6
  const scope = config.scope === 'session' ? 'session' : 'user'
  const kernelRoot = resolveKernelRoot(config.repoPath)
  const python = resolvePython(config.pythonPath)
  const dbPath = resolve(str(config.dbPath) || join(homedir(), '.xujie-memory', 'memory.db'))
  const dataDir = dirname(dbPath)
  const tokenFile = str(config.tokenFile) || join(dataDir, 'token')
  const logFile = str(config.logFile) || join(dataDir, 'kernel.log')
  const statusFile = str(config.statusFile)
  const expectedVersion = kernelVersion(kernelRoot)
  // 宿主若会 await section.text 返回的 Promise，可以打开它：组装时等这一轮的
  // /turn（最多 waitMs）。宿主不支持时会把 Promise 当字符串，**默认关闭**。
  const asyncSection = config.asyncSection === true
  const waitMs = Number.isFinite(Number(config.waitMs)) ? Number(config.waitMs) : 400

  /** 当前在用的内核地址（可能因端口被占而换过）。 */
  let endpoint = `http://${host}:${basePort}`
  let token = readToken(tokenFile)

  // A single queue owns kernel operations and prompt-cache publication. UI edits,
  // model deletions and new turns cannot race to republish stale material.
  let operationTail = Promise.resolve()
  const enqueue = (run) => {
    const job = operationTail.catch(() => {}).then(run)
    operationTail = job.catch(() => {})
    return job
  }
  const clearMemoryCache = () => { cacheState.epoch++; contexts.clear(); latestByUser.clear() }
  ctx.effect(() => () => { cacheState.disposed = true; clearMemoryCache() }, 'xujie-memory: instance caches')
  ctx.inject(['typert'], (remoteCtx) => {
    new MemoryController(remoteCtx, {
      request: (path, body) => enqueue(async () => {
        if (cacheState.disposed) throw new Error('memory_plugin_unloaded')
        if (kernelReady) await kernelReady
        const mutation = path === '/manage/edit' || path === '/manage/forget'
        if (mutation) clearMemoryCache()
        const resp = await fetchJson(`${endpoint}${path}`, body, 15000, token)
        if (!resp || resp.ok !== true) {
          throw new Error(resp?.detail ?? resp?.error ?? 'memory_kernel_unavailable')
        }
        if (mutation) clearMemoryCache()
        return resp
      }),
    })
  })

  const log = (level, message) => {
    const logger = ctx.logger
    if (logger && typeof logger[level] === 'function') logger[level](`xujie-memory: ${message}`)
    if (statusFile && level !== 'debug') {
      try {
        appendFileSync(statusFile, `${new Date().toISOString()} ${level} ${message}\n`, 'utf8')
      } catch { /* 诊断文件写不进去不影响主流程 */ }
    }
  }

  // ------------------------------------------------------------ 内核
  if (autoStart) {
    ctx.effect(() => {
      let child = null
      let stopped = false
      let booting = null
      let failures = 0
      let nextAttemptAt = 0

      /** 端口上这个内核能不能用：版本对、库对、token 对。 */
      const probe = async (url) => {
        const body = await health(url, token)
        if (!body) return 'down'
        if (expectedVersion && body.version !== expectedVersion) return 'foreign'
        if (body.auth && !body.db) return 'foreign'           // token 不对
        if (body.db && resolve(body.db) !== dbPath) return 'foreign'
        if (body.scope_mode && body.scope_mode !== scope) return 'foreign'
        return 'ok'
      }

      const spawnKernel = (port) => {
        mkdirSync(dataDir, { recursive: true })
        rotateLog(logFile)
        let fd = 'ignore'
        try { fd = openSync(logFile, 'a') } catch { fd = 'ignore' }
        const args = [
          '-m', 'xujie_memory.server',
          '--host', host, '--port', String(port),
          '--db', dbPath,
          '--token-file', tokenFile,
          '--scope', scope,
          '--parent-pid', String(process.pid),
        ]
        const proc = spawn(python, args, {
          cwd: kernelRoot,
          stdio: ['ignore', fd, fd],
          windowsHide: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
        })
        if (typeof fd === 'number') { try { closeSync(fd) } catch { /* ignore */ } }
        proc.on?.('error', (err) => log('warn', `内核启动失败：${err.message}`))
        proc.on?.('exit', (code, signal) => {
          if (child === proc) child = null
          if (!stopped) log('warn', `内核退出（code=${code ?? 'null'} signal=${signal ?? 'null'}），看门狗会重拉；详情见 ${logFile}`)
        })
        return proc
      }

      const boot = async () => {
        if (!kernelRoot) {
          log('warn', '找不到记忆内核（python/xujie_memory 不存在），需要在配置里给 repoPath')
          return
        }
        // 1. 找一个能用的端口：已经有对的内核就复用，被别人占了就往后挪。
        for (let i = 0; i < PORT_TRIES && !stopped; i++) {
          const port = basePort + i
          const url = `http://${host}:${port}`
          const state = await probe(url)
          if (state === 'ok') {
            endpoint = url
            failures = 0
            log('info', `复用已在运行的内核：${url}`)
            return
          }
          if (state === 'foreign') {
            log('warn', `端口 ${port} 上的服务不是本版本内核（或库/鉴权不一致），换下一个端口`)
            continue
          }
          // 2. 空端口：拉起自己的内核。
          if (child) { try { child.kill() } catch { /* ignore */ } }
          try {
            child = spawnKernel(port)
          } catch (err) {
            log('warn', `内核没能拉起：${err.message}`)
            return markFailure()
          }
          log('info', `内核启动中（pid ${child.pid ?? '?'}，port ${port}，python=${python}）`)
          const startedAt = Date.now()
          while (!stopped && Date.now() - startedAt < READY_WAIT_MS) {
            await sleep(150)
            token = readToken(tokenFile) || token
            const s = await probe(url)
            if (s === 'ok') {
              endpoint = url
              failures = 0
              log('info', `内核就绪（${Date.now() - startedAt}ms）：${url}`)
              return
            }
            if (!child) break                     // 进程已经退出了
          }
          if (!stopped) log('warn', `${READY_WAIT_MS}ms 内内核未就绪（对话不受影响）；详情见 ${logFile}`)
          return markFailure()
        }
        if (!stopped) log('warn', `端口 ${basePort}-${basePort + PORT_TRIES - 1} 都不可用`)
        return markFailure()
      }

      const markFailure = () => {
        failures += 1
        // 退避：2s, 4s, 8s … 封顶 5 分钟。不会因为环境坏了就疯狂重启。
        const delay = Math.min(300000, 2000 * 2 ** Math.min(failures - 1, 8))
        nextAttemptAt = Date.now() + delay
      }

      const ensure = () => {
        if (stopped || booting || Date.now() < nextAttemptAt) return booting
        booting = boot().catch((err) => {
          log('warn', `内核引导异常：${err?.message ?? err}`)
          markFailure()
        }).finally(() => { booting = null })
        return booting
      }

      kernelReady = async () => { await ensure() }
      void ensure()

      // 看门狗：崩溃、被别的实例带走、被手动关掉，都会被拉回来。
      const timer = setInterval(async () => {
        if (stopped || booting) return
        if ((await probe(endpoint)) !== 'ok') {
          log('warn', `内核不可用（${endpoint}），尝试恢复`)
          void ensure()
        }
      }, WATCHDOG_MS)
      timer.unref?.()

      return () => {
        stopped = true
        clearInterval(timer)
        kernelReady = null
        try { child?.kill() } catch { /* 已经退出 */ }
      }
    }, 'xujie-memory: python service')
  } else {
    log('info', `autoStart=false，等待外部内核：${endpoint}`)
  }

  // ------------------------------------------------------------ 注入材料
  ctx.inject(['systemPrompt'], (prompted) => {
    const service = prompted.get('systemPrompt')
    if (!service || typeof service.section !== 'function') {
      log('warn', 'systemPrompt 没有 section()，这个宿主版本可能不兼容')
      return
    }
    const pick = (assemble) => {
      const raw = assemble?.agent?.session
      if (raw) {
        const got = contexts.get(sessionKey(raw))
        if (got !== undefined) return got
      }
      // 没有这个会话的材料（新会话第一句之前 / 宿主内部组装）：
      // user 模式下记忆本来就跨会话共享，用最近一次的材料；
      // session 模式下绝不串会话，返回空。
      return scope === 'user' && raw ? (latestByUser.get(String(raw.userId ?? raw.account ?? 'user')) ?? '') : ''
    }
    service.section({
      name: 'xujie-memory',
      order: 150,
      text: (assemble) => {
        const key = sessionKey(assemble?.agent?.session)
        const inflight = pending.get(key)
        if (!asyncSection) {
          if (inflight) log('debug', `组装时 ${key} 的本轮记忆还没回来，用的是上一轮材料`)
          return pick(assemble)
        }
        if (!inflight) return pick(assemble)
        return Promise.race([inflight, sleep(waitMs)]).then(() => pick(assemble))
      },
    })
  })

  // ------------------------------------------------------------ 工具
  // 注入全自动，但"主动回忆"和"记错了删掉"必须给模型一只手：
  // 只会被动接收召回结果 = 只许它看，不许它查、不许它改。
  // 三个都是可解释的低危操作：搜（只读）、忘（软删，可审计）、看账（只读）。
  const toolDisposers = []
  ctx.inject(['tools'], (toolCtx) => {
    // 宿主没给 tools 服务（老版本 / 测试替身）时静默跳过：
    // 工具是增强，不能因为它让整块记忆插件挂不上。
    if (typeof toolCtx?.tools?.register !== 'function') {
      log('warn', '宿主没有 tools 服务，检索/删除工具不会注册（注入照常）')
      return
    }
    const sessionOf = (exec) => exec?.agent?.session ?? exec?.session
    const call = async (path, payload, exec) => {
      const resp = await enqueue(async () => {
        if (path === '/memory/forget' || path === '/memory/edit') clearMemoryCache()
        return await fetchJson(
          `${endpoint}${path}`,
          { session: sessionKey(sessionOf(exec)), user: String(sessionOf(exec)?.userId ?? sessionOf(exec)?.account ?? 'user'), ...payload },
          TOOL_TIMEOUT_MS,
          token,
        )
      })
      if (!resp || resp.ok !== true) {
        throw new Error(`xujie-memory: ${resp?.detail ?? resp?.error ?? 'memory kernel did not respond'}`)
      }
      return resp
    }
    const text = (value) => [{ type: 'text', text: value }]

    toolDisposers.push(toolCtx.tools.register({
      name: 'memory_search',
      description: '在用户的长期记忆里检索他说过的话（支持短词与错字模糊匹配）。只读。返回原话、trace_id 与权重点。要删除某条记忆前，先用它拿到 trace_id。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '要检索的内容；两个字也能搜，错一个字也能命中' },
          limit: { type: 'number', description: '最多返回几条，默认 5（1-20）' },
        },
        required: ['query'],
      },
      output: {
        schema: { type: 'object' },
        render: (_args, value) => {
          const hits = value?.hits ?? []
          if (hits.length === 0) return text(`没有搜到与「${value?.query ?? ''}」相关的记忆。`)
          const lines = hits.map((h) => {
            const tags = [h.anchor ? '锚点' : '', h.importance ? `重要度 ${h.importance}` : ''].filter(Boolean)
            return `- ${h.content}（trace_id: ${h.trace_id}${tags.length ? '，' + tags.join('，') : ''}）`
          })
          return text(`搜到 ${hits.length} 条：\n${lines.join('\n')}`)
        },
      },
      execute: async (args, exec) => {
        const query = String(args?.query ?? '').trim().slice(0, MAX_TEXT_CHARS)
        if (!query) throw new Error('memory_search: query 不能为空')
        const limit = Math.max(1, Math.min(20, Math.round(Number(args?.limit) || 5)))
        const resp = await call('/memory/search', { query, limit }, exec)
        return { ok: true, query: resp.query ?? query, hits: resp.hits ?? [] }
      },
    }))

    toolDisposers.push(toolCtx.tools.register({
      name: 'memory_forget',
      description: '删掉一条记忆（软删：内容清空、FTS 移除、由它推出的事实一起失效并重算；库里保留 erased_at 供审计，不可恢复）。trace_id 用 memory_search 取。用户说「忘掉这条」时用。',
      parameters: {
        type: 'object',
        properties: {
          trace_id: { type: 'string', description: '要删的记忆条目 id（memory_search 返回的 trace_id）' },
        },
        required: ['trace_id'],
      },
      output: {
        schema: { type: 'object' },
        render: (_args, value) => {
          if (!value?.erased) return text('删除失败：内核没有确认。')
          const tail = value.already_erased ? '（这条之前已经删过）' : ''
          return text(`已删除 ${value.trace_id}${tail}，连带失效 ${value.state_keys?.length ?? 0} 组状态。`)
        },
      },
      execute: async (args, exec) => {
        const traceId = String(args?.trace_id ?? '').trim()
        if (!traceId) throw new Error('memory_forget: trace_id 不能为空')
        const result = await call('/memory/forget', { trace_id: traceId }, exec)
        contexts.clear()
        latestByUser.clear()
        return result
      },
    }))

    toolDisposers.push(toolCtx.tools.register({
      name: 'memory_edit',
      description: '仅在用户明确要求纠正记忆时，替换一条原话并重算事实与画像。先用 memory_search 取得 trace_id 和 content_hash；保存成功后返回新的 trace_id。不能用来偷偷改写用户历史。',
      parameters: {
        type: 'object',
        properties: {
          trace_id: { type: 'string', description: '要纠正的原话 id' },
          content: { type: 'string', description: '用户明确确认的新内容，最多 2000 字' },
          expected_hash: { type: 'string', description: 'memory_search 返回的 content_hash，防止覆盖其他修改' },
        },
        required: ['trace_id', 'content', 'expected_hash'],
      },
      output: {
        schema: { type: 'object' },
        render: (_args, value) => text(value?.ok
          ? `已纠正记忆，新 trace_id: ${value.trace?.trace_id}。`
          : '纠正未完成，请重新检索后再试。'),
      },
      execute: async (args, exec) => {
        const trace_id = String(args?.trace_id ?? '').trim()
        const content = String(args?.content ?? '').trim()
        const expected_hash = String(args?.expected_hash ?? '').trim()
        if (!trace_id || !content || content.length > MAX_TEXT_CHARS || !expected_hash) {
          throw new Error('memory_edit: 缺少记录、有效内容或版本 hash')
        }
        const result = await call('/memory/edit', { trace_id, content, expected_hash }, exec)
        clearMemoryCache()
        return result
      },
    }))

    toolDisposers.push(toolCtx.tools.register({
      name: 'memory_status',
      description: '看记忆库现状：原话条数、已删条数、锚点数、未确认项，以及内核健康信息。用户问「你记得我什么」「记忆正常吗」时用。只读。',
      parameters: { type: 'object', properties: {} },
      output: {
        schema: { type: 'object' },
        render: (_args, value) => {
          const kernel = value?.kernel ?? {}
          const head = `记忆库：原话 ${value?.traces ?? 0} 条（已删 ${value?.traces_erased ?? 0} 条），`
            + `状态 ${value?.states ?? 0} 条，锚点 ${value?.anchors ?? 0} 个，`
            + `未确认 ${value?.clarifications_open ?? 0} 个。`
          const tail = kernel.ok
            ? `内核：pid ${kernel.pid}，版本 ${kernel.version}，scope=${kernel.scope_mode}，库 ${kernel.db}`
            : '内核：当前连不上（注入会降级为「这一轮没有记忆」）。'
          return text(`${head}\n${tail}`)
        },
      },
      execute: async (_args, exec) => {
        const [stats, kernel] = await Promise.all([
          call('/memory/stats', {}, exec),
          health(endpoint, token),
        ])
        return { ...stats, kernel }
      },
    }))
  })
  ctx.effect(() => () => {
    for (const dispose of toolDisposers.splice(0)) {
      try { dispose?.() } catch { /* 已释放 */ }
    }
  }, 'xujie-memory: tools')

  // ------------------------------------------------------------ 写记忆
  ctx.on('session/event', (session, event) => {
    const type = event?.type
    if (type !== 'user/message') return

    const data = event?.data
    const sourceKind = data?.source?.kind
    // runtime-context / agent-instructions 也是 user/message，但它们不是用户说的。
    if (typeof sourceKind === 'string' && sourceKind !== 'user') {
      log('debug', `event ${type}（source=${sourceKind}，跳过）`)
      return
    }

    let text = extractText(data)
    if (!text) {
      log('warn', `event ${type} 里没找到正文，字段是：${Object.keys(data || {}).join(',')}`)
      return
    }
    if (text.length > MAX_TEXT_CHARS) text = text.slice(0, MAX_TEXT_CHARS)

    if (!shapeLogged) {
      shapeLogged = true
      log('info', `user/message 形状：${shapeOf(data)}`)
    }

    const key = sessionKey(session)
    // 同一会话串行：上一句没写完，下一句排在后面，保证顺序。
    const job = enqueue(() => remember(() => endpoint, () => token, session, text, maxAskedTotal, log, cacheState))
      .finally(() => { if (pending.get(key) === job) pending.delete(key) })
    pending.set(key, job)
  })

  log('info', `已挂载（scope=${scope}，内核 ${expectedVersion || '?'}，库 ${dbPath}）`)
}

/** 测试/诊断用：等内核引导结束。 */
export let kernelReady = null

// ---------------------------------------------------------------- 路径

function str(v) {
  return typeof v === 'string' ? v.trim() : ''
}

/** 优先用配置里的 repoPath（开发），否则用包里自带的 python/。 */
function resolveKernelRoot(repoPath) {
  const configured = str(repoPath)
  if (configured && existsSync(join(configured, 'xujie_memory'))) return configured
  if (existsSync(join(BUNDLED_KERNEL, 'xujie_memory'))) return BUNDLED_KERNEL
  return ''
}

/** 优先用配置，其次包内解释器，最后 PATH 里的 python。 */
function resolvePython(pythonPath) {
  const configured = str(pythonPath)
  if (configured) return configured
  const platformDir = `${process.platform}-${process.arch}`
  const bundled = process.platform === 'win32'
    ? join(PACKAGE_ROOT, 'runtime', platformDir, 'python.exe')
    : join(PACKAGE_ROOT, 'runtime', platformDir, 'bin', 'python3')
  if (existsSync(bundled)) return bundled
  return process.platform === 'win32' ? 'python' : 'python3'
}

/** 包内内核的版本号；/health 对不上就说明端口上是别的内核。 */
function kernelVersion(root) {
  if (!root) return ''
  try {
    const src = readFileSync(join(root, 'xujie_memory', '__init__.py'), 'utf8')
    return src.match(/__version__\s*=\s*["']([^"']+)["']/)?.[1] ?? ''
  } catch {
    return ''
  }
}

function readToken(file) {
  try { return readFileSync(file, 'utf8').trim() } catch { return '' }
}

function rotateLog(file) {
  try {
    if (statSync(file).size > LOG_ROTATE_BYTES) renameSync(file, `${file}.1`)
  } catch { /* 不存在或改不了名都无所谓 */ }
}

// ---------------------------------------------------------------- 工具

function sleep(ms) {
  return new Promise((r) => { const t = setTimeout(r, ms); t.unref?.() })
}

async function health(url, token) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS)
  try {
    const resp = await fetch(`${url}/health`, {
      signal: controller.signal,
      headers: token ? { 'X-Xujie-Token': token } : {},
    })
    if (!resp.ok) return null
    const body = await resp.json().catch(() => null)
    return body?.ok ? body : null
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/** 结构摘要：只出字段名和类型，不出内容。 */
function shapeOf(value, depth = 0) {
  if (value === null) return 'null'
  if (Array.isArray(value)) {
    return depth > 2 ? 'array' : `[${value.length ? shapeOf(value[0], depth + 1) : ''}]`
  }
  if (typeof value !== 'object') return typeof value
  if (depth > 2) return 'object'
  const keys = Object.keys(value).slice(0, 10)
  return `{${keys.map((k) => `${k}: ${shapeOf(value[k], depth + 1)}`).join(', ')}}`.slice(0, 240)
}

/** 从事件 data 里掏正文：真机是 content 数组里的 text 块。 */
export function extractText(data) {
  const candidates = [
    data?.text,
    data?.content,
    data?.message?.content,
    data?.message?.text,
    data?.payload?.text,
    data?.payload?.content,
  ]
  for (const c of candidates) {
    if (typeof c === 'string' && c.trim()) return c.trim()
    if (Array.isArray(c)) {
      const joined = c
        .map((part) => {
          if (typeof part === 'string') return part
          if (!part || typeof part !== 'object') return ''
          return typeof part.text === 'string' ? part.text : ''
        })
        .filter(Boolean)
        .join('\n')
        .trim()
      if (joined) return joined
    }
  }
  return ''
}

function sessionKey(session) {
  return String(session?.id ?? session?.sessionId ?? session?.key ?? 'default')
}

// ---------------------------------------------------------------- 请求

async function remember(getEndpoint, getToken, session, text, maxAskedTotal, log, cache) {
  if (cache.disposed) return
  const { contexts, latestByUser } = cache
  const epoch = cache.epoch
  const key = sessionKey(session)
  const body = {
    session: key,
    user: String(session?.userId ?? session?.account ?? 'user'),
    text,
    // 对话的另一方是助手，不是 'user'——旧版写成 'user'，
    // 「我欠你 20」会记成"自己欠自己"。
    context: { peer_id: 'assistant' },
    budget: { max_asked_total: maxAskedTotal },
  }

  let lastErr = null
  // 失败重试一次（内核刚被看门狗拉起时最常见）。
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const resp = await fetchJson(`${getEndpoint()}/turn`, body, TURN_TIMEOUT_MS, getToken())
      if (resp?.ok && typeof resp.context === 'string') {
        if (cache.disposed || cache.epoch !== epoch) return
        contexts.delete(key)
        contexts.set(key, resp.context)          // 重新插入 = 移到最新
        while (contexts.size > MAX_SESSIONS) {
          const oldest = contexts.keys().next().value
          if (oldest === undefined) break
          contexts.delete(oldest)
        }
        const user = String(session?.userId ?? session?.account ?? 'user')
        latestByUser.delete(user)
        latestByUser.set(user, resp.context)
        while (latestByUser.size > MAX_SESSIONS) latestByUser.delete(latestByUser.keys().next().value)
        log('debug',
          `记下 ${resp.outcomes?.length ?? 0} 条判定，材料 ${resp.context.length} 字，会话 ${key}`)
        return
      }
      lastErr = new Error(resp?.detail ? `${resp.error}: ${resp.detail}` : (resp?.error ?? 'unknown'))
      // 内核已经处理过这句（4xx/5xx 都是）：重试可能写两遍，不重试。
      // 401 例外：token 刚被新内核换掉，重读一次再试。
      if (resp?.status !== 401) break
    } catch (err) {
      lastErr = err
      // 超时 = 内核可能已经写进去了，重试会重复记一笔，不重试。
      // 只有「连不上」（内核正在被看门狗重拉）才重试。
      if (err?.name === 'AbortError' || attempt === 1) break
    }
    await sleep(500)
  }
  log('warn', `这一轮没有记忆：${lastErr?.message ?? 'unknown'}`)
}

async function fetchJson(url, payload, timeoutMs, token) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const headers = { 'Content-Type': 'application/json' }
    if (token) headers['X-Xujie-Token'] = token
    const resp = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    const text = await resp.text()
    try {
      const parsed = JSON.parse(text)
      if (parsed && typeof parsed === 'object') parsed.status = resp.status
      return parsed
    } catch {
      return { ok: false, status: resp.status, error: `bad_json: ${text.slice(0, 120)}` }
    }
  } finally {
    clearTimeout(timer)
  }
}
