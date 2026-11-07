/**
 * hippocampus-deepseek · DSH 宿主半（M29）
 *
 * 装上即用：
 *   1. 自动拉起本地 Python 内核（记忆库在 ~/.hippo/mem.db）
 *   2. 订阅 session/event：用户 / 助手每轮写入库
 *   3. systemPrompt.section 注入本会话的记忆文本
 *
 * 设计纪律（和内核一致）：
 *   - 任何失败只降级为「这一轮没有记忆」，绝不影响对话。
 *   - 注入走同步缓存：宿主组装提示词时不等待网络（内核再快也不该卡首包）。
 *   - 内核是子进程，宿主退出时杀掉（--parent-pid 双保险）。
 */

import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statSync } from 'node:fs'
import { createServer } from 'node:net'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { registerBoardRoutes } from './board-routes.js'
import { QQRuntime } from './qq/runtime.js'
import { QQTransport } from './qq/transport.js'
import { QQBudgetLedger } from './qq/budget-ledger.js'

export const name = 'dsh-plugin-hippocampus-deepseek'
// 只把 systemPrompt 当硬依赖：没有它就没法注入记忆，插件本就无事可做。
//
// credentials **不能**放进来。cordis 里模块级 inject 是"必需服务"，
// 缺了就一直 pending、整个插件不激活——headless profile 没有凭据服务时
// 实测就是这样：`hippocampus-deepseek: pending (waiting for service: credentials)`，
// 结果是连对话原文都不记（用户装完看着什么都没发生）。
// 而读 key 的地方本来就是惰性访问 + 兜底（resolveHostApiKey 里
// `ctx.get?.('credentials')` + try/catch + 配置/环境变量兜底），
// 所以这里不需要它，也不该让它挡住插件启动。
export const inject = ['systemPrompt']

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const DEFAULT_PORT = 8766
const HEALTH_WAIT_MS = 12_000
const HEALTH_TIMEOUT_MS = 800
const TURN_TIMEOUT_MS = 3_000
const ASSEMBLE_TIMEOUT_MS = 2_500
const ROTATE_BYTES = 1_000_000

export function apply(ctx, config = {}) {
  const log = (...a) => safeLog('info', a)
  const warn = (...a) => safeLog('warn', a)

  function safeLog(level, parts) {
    const line = `[hippo] ${parts.join(' ')}`
    try {
      if (level === 'warn') ctx.logger?.warn?.(line)
      else ctx.logger?.info?.(line)
    } catch {
      /* 宿主 logger 不在也不影响 */
    }
  }

  const dbPath = expandUser(String(config.dbPath || join(homedir(), '.hippo', 'mem.db')))
  // 端口必须是可变的：固定端口在多实例下必然撞车（实测第二个 DSH 实例的内核
  // 直接 bind 失败退出 10048，然后被 1.5s 无限重拉）。见 pickPort()。
  let port = Number(config.port || DEFAULT_PORT)
  let endpoint = `http://127.0.0.1:${port}`
  let portLocked = Number(config.port) > 0
  let failures = 0
  let retryTimer = null
  let apiKey = ''
  const kernelRoot = join(PACKAGE_ROOT, 'hippo')
  const python = resolvePython(config)

  let child = null
  let starting = null
  let instanceId = ''
  let stopped = false
  let ready = false

  // 每会话：上一轮记忆文本 + 正在组装的 Promise + 写队列
  const lastText = new Map()
  const inflightWrite = new Map()
  const inflightAssembly = new Map()
  // QQ 会话 id → 身份串（qq:self:channel:conversation:actor），供记忆按人隔离。
  const identities = new Map()

  // ------------------------------------------------------------ 内核进程
  function kernelLogPath() {
    return join(dirname(dbPath), 'kernel.log')
  }

  function rotateKernelLog() {
    try {
      const p = kernelLogPath()
      if (!existsSync(p)) return
      if (statSync(p).size > ROTATE_BYTES) renameSync(p, `${p}.1`)
    } catch {
      /* 轮转失败不影响主流程 */
    }
  }

  function appendKernelLog(text) {
    try {
      mkdirSync(dirname(dbPath), { recursive: true })
      appendFileSync(kernelLogPath(), text, 'utf8')
    } catch {
      /* 写不进去也不影响 */
    }
  }

  async function startKernel() {
    if (!python) {
      warn('没有可用的 Python，插件降级为不记忆（用包内 runtime 或装 Python）')
      return
    }
    if (!existsSync(join(kernelRoot, 'dsh_kernel.py'))) {
      warn(`包内内核缺失：${join(kernelRoot, 'dsh_kernel.py')}`)
      return
    }
    apiKey = await resolveHostApiKey(ctx, config)
    if (stopped) return
    rotateKernelLog()
    // 每次拉起前挑一个真正空闲的端口。固定端口在"同时开着第二个 DSH 实例"
    // 这种最常见的场景下就会撞车：内核 bind 失败退出，记忆功能整段失效，
    // 而且用户看不到任何报错。
    if (!portLocked || failures > 0) {
      const picked = await pickPort(port)
      if (picked === 0) {
        // 一个空端口都没有：不要带着旧端口去撞，直接按失败退避。
        warn('找不到空闲端口，记忆内核暂时不启动（会在后台重试）')
        failures += 1
        scheduleRetry()
        return
      }
      if (picked !== port) {
        if (portLocked) warn(`端口 ${port} 被占用，改用 ${picked}`)
        port = picked
        endpoint = `http://127.0.0.1:${port}`
      }
    }
    const args = [
      '-m', 'hippo.dsh_kernel',
      '--db', dbPath,
      '--port', String(port),
      '--parent-pid', String(process.pid),
    ]
    instanceId = randomUUID()
    const env = {
      ...process.env,
      PYTHONPATH: `${PACKAGE_ROOT}${process.platform === 'win32' ? ';' : ':'}${process.env.PYTHONPATH || ''}`,
      PYTHONIOENCODING: 'utf-8',
      HIPPO_LOG: 'off',
      HIPPO_EXTRACT_API_KEY: apiKey,
      HIPPO_KERNEL_INSTANCE: instanceId,
    }
    appendKernelLog(`\n[${new Date().toISOString()}] spawn ${python} ${args.join(' ')}\n`)
    try {
      child = spawn(python, args, { cwd: PACKAGE_ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
      const spawned = child
      child.stdout?.on('data', (b) => appendKernelLog(String(b)))
      child.stderr?.on('data', (b) => appendKernelLog(String(b)))
      child.on('error', () => {
        warn('无法启动记忆内核，请检查 Python 路径和插件安装')
        if (child === spawned) child = null
        ready = false
        if (!stopped) {
          failures += 1
          scheduleRetry()
        }
      })
      child.on('exit', (code) => {
        appendKernelLog(`[${new Date().toISOString()}] kernel exit ${code}\n`)
        ready = false
        if (stopped) return
        child = null
        failures += 1
        scheduleRetry()
      })
      await waitReady()
    } catch (error) {
      warn(`内核拉起失败：${error instanceof Error ? error.message : String(error)}`)
      failures += 1
      scheduleRetry()
    }
  }

  /**
   * 失败后按指数退避重拉（1.5s → 3s → 6s … 封顶 30s）。
   *
   * 原来是无条件 1.5 秒重试：一旦是**永久性失败**（端口被占、Python 缺失、
   * 库被锁），就变成每秒一次的进程狂拉 + 日志刷屏，日志文件几百 KB 地长。
   * 更糟的是它看起来"在努力"，用户完全不知道记忆其实一条没记。
   */
  function scheduleRetry() {
    if (stopped || retryTimer) return
    const delay = Math.min(1500 * 2 ** Math.min(Math.max(failures, 1) - 1, 5), 30_000)
    if (failures === 6) {
      warn(`记忆内核连续 ${failures} 次没起来，改为每 30 秒重试一次（不再反复重拉）：${kernelLogPath()}`)
    }
    retryTimer = setTimeout(() => {
      retryTimer = null
      if (!stopped) void ensureReady()
    }, delay)
    retryTimer.unref?.()
  }

  function stopKernel() {
    if (!child) return
    try {
      child.kill()
    } catch {
      /* 已退出 */
    }
    child = null
  }

  async function waitReady() {
    const deadline = Date.now() + HEALTH_WAIT_MS
    while (!stopped && Date.now() < deadline) {
      if (child === null) return
      try {
        const res = await fetchJson('/health', undefined, HEALTH_TIMEOUT_MS)
        if (res?.ok && res.instance === instanceId) {
          ready = true
          failures = 0
          log(`内核就绪 ${endpoint}，库 ${dbPath}`)
          return
        }
      } catch {
        /* 还没起来 */
      }
      await sleep(250)
    }
    if (child !== null) warn(`内核 ${HEALTH_WAIT_MS}ms 内未就绪：${kernelLogPath()}`)
  }

  async function ensureReady() {
    if (stopped) return false
    if (ready && child) return true
    if (!child) {
      if (!starting) starting = startKernel().finally(() => { starting = null })
      await starting
    }
    else await waitReady()
    return ready
  }

  async function fetchJson(path, body, timeoutMs) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const res = await fetch(`${endpoint}${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return await res.json()
    } finally {
      clearTimeout(timer)
    }
  }

  // ------------------------------------------------------------ 写入 & 组装
  async function remember(session, role, text) {
    if (!text) return
    try {
      if (!(await ensureReady())) return
      await fetchJson('/event', { session, role, text }, TURN_TIMEOUT_MS)
    } catch (error) {
      warn(`写记忆失败（不影响对话）：${error instanceof Error ? error.message : String(error)}`)
    }
  }

  function assemble(session, userText) {
    if (!ready) return Promise.resolve('')
    return fetchJson('/assemble', { session, text: userText }, ASSEMBLE_TIMEOUT_MS)
      .then((res) => {
        const text = res?.ok ? String(res.text || '') : ''
        if (text) lastText.set(session, text)
        else lastText.delete(session)
        return text
      })
      .catch(() => lastText.get(session) || '')
  }

  // DSH 在记录 user/message 之前组装提示词；从收件箱提前启动本轮检索。
  ctx.on('agent/inbox/inserted', ({ agent, message }) => {
    const kind = message?.source?.kind
    if (typeof kind === 'string' && kind !== 'user') return
    const text = extractText(message)
    if (!text) return
    const key = sessionKey(agent?.session || agent?.id)
    const job = ensureReady().then(ok => ok ? assemble(key, text) : '').catch(() => '')
    inflightAssembly.set(key, job)
    job.finally(() => { if (inflightAssembly.get(key) === job) inflightAssembly.delete(key) })
  })

  // section 的 provider 是同步的；waterfall 允许在提示词定稿前等待本轮检索。
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const result = await next()
    const subject = context?.agent?.session || context?.agent?.id
    if (!subject) return result
    const key = sessionKey(subject)
    const job = inflightAssembly.get(key)
    if (job) await Promise.race([job, sleep(3000)])
    const text = lastText.get(key) || ''
    return { ...result, sections: result.sections.map(section => section.name === 'hippocampus-deepseek'
      ? { ...section, text, interpolate: false } : section) }
  })

  // Connection 的 Fetch 路由由 DSH 校验浏览器身份及 owner 权限。
  ctx.inject(['connection'], (boardCtx) => {
    boardCtx.effect(() => registerBoardRoutes(boardCtx.connection, {
      boardPath: join(kernelRoot, 'data', 'board.html'),
      load: async () => {
        if (!(await ensureReady())) throw new Error('kernel unavailable')
        return fetchJson('/board/data', undefined, TURN_TIMEOUT_MS)
      },
    }), 'hippocampus-deepseek: board routes')
  })

  // ------------------------------------------------------------ 宿主接线
  ctx.inject(['systemPrompt'], (prompted) => {
    const service = prompted.get?.('systemPrompt')
    if (!service || typeof service.section !== 'function') {
      warn('宿主没有 systemPrompt.section，本版本不注入（写记忆照常）')
      return
    }
    service.section({
      name: 'hippocampus-deepseek',
      order: 150,
      interpolate: false,
      text: (assembleCtx) => {
        const key = sessionKey(assembleCtx?.agent?.session)
        return lastText.get(key) || ''
      },
    })
  })

  ctx.on('session/event', (session, event) => {
    const type = event?.type
    if (type !== 'user/message' && type !== 'assistant/message') return
    const data = event?.data
    // runtime-context / agent-instructions 也是 user/message，但它们不是用户说的
    const kind = data?.source?.kind
    if (typeof kind === 'string' && kind !== 'user') return

    const text = extractText(data)
    if (!text) return
    const key = sessionKey(session)
    const role = type === 'user/message' ? 'user' : 'assistant'

    if (role === 'user') {
      // 用户消息：串行写库，保持同一会话的先后顺序。
      const prev = inflightWrite.get(key) || Promise.resolve()
      const job = prev
        .then(() => remember(key, 'user', text))
      inflightWrite.set(key, job)
      job.finally(() => {
        if (inflightWrite.get(key) === job) inflightWrite.delete(key)
      })
    } else {
      const prev = inflightWrite.get(key) || Promise.resolve()
      const job = prev.then(() => remember(key, 'assistant', text))
      inflightWrite.set(key, job)
      job.finally(() => {
        if (inflightWrite.get(key) === job) inflightWrite.delete(key)
      })
    }
  })

  // ------------------------------------------------------------ QQ 桥接（自 0.8 实验分支移植）
  //
  // 配置与预算都在内核里，这里只负责：读 revision → 建/拆桥接。
  // 没有配置（或在内核里删掉）时这段完全不做事，也不占端口/内存。
  ctx.inject(['agents', 'llm'], (qqCtx) => {
    qqCtx.effect(() => {
      let runtime = null
      let revision = ''
      let disposed = false
      let refreshing = false

      const refresh = async () => {
        if (refreshing || disposed) return
        refreshing = true
        try {
          if (!(await ensureReady())) return
          const response = await fetchJson('/qq/settings/active', undefined, TURN_TIMEOUT_MS)
          if (response?.ok !== true) throw new Error('读取 QQ 配置失败')
          const qq = response.settings
          if ((qq?.revision ?? '') === revision) return
          if (runtime) { await runtime.close(); runtime = null; revision = '' }
          if (!qq || disposed) return
          let createUserMessage
          try {
            ({ createUserMessage } = await import('@deepseek-ai/dsh-llm'))
          } catch {
            throw new Error('DSH 未提供 llm 消息构造器，QQ 桥接不可用')
          }
          if (disposed) return
          const ledger = new QQBudgetLedger((path, body) => fetchJson(path, body, TOOL_TIMEOUT_MS))
          runtime = new QQRuntime({
            ctx: qqCtx, config: qq, ledger, identities,
            transport: new QQTransport(qq.httpUrl, qq.accessToken || ''),
            makeMessage: (text, source) => createUserMessage({
              content: [{ type: 'text', text }], source: { kind: source },
            }),
            onError: error => warn(`QQ 桥接：${error instanceof Error ? error.message : '操作失败'}`),
          })
          try {
            runtime.connect()
            revision = qq.revision
            log('QQ 桥接已连接')
          } catch (error) {
            await runtime.close()
            runtime = null
            throw error
          }
        } catch (error) {
          if (runtime) { await runtime.close(); runtime = null; revision = '' }
          if (!disposed) warn(`QQ 桥接未就绪：${error instanceof Error ? error.message : '未知错误'}`)
        } finally {
          refreshing = false
        }
      }

      const timer = setInterval(() => { void refresh() }, 5000)
      timer.unref?.()
      void refresh()
      return () => {
        disposed = true
        clearInterval(timer)
        if (runtime) void runtime.close().catch(() => {})
      }
    }, 'hippocampus-deepseek: qq bridge')
  })

  // ------------------------------------------------------------ 生命周期
  ctx.effect(() => {
    void ensureReady()
    const timer = setInterval(() => {
      if (stopped) return
      if (!child) void ensureReady()
    }, 10_000)
    timer.unref?.()
    return async () => {
      clearInterval(timer)
      if (retryTimer) {
        clearTimeout(retryTimer)
        retryTimer = null
      }
      await Promise.allSettled([...inflightWrite.values()])
      if (ready && child) {
        try { await fetchJson('/drain', {}, 32_000) } catch { /* pending turns remain in SQLite */ }
      }
      stopped = true
      stopKernel()
    }
  }, 'hippocampus-deepseek: kernel lifecycle')

  log(`已挂载（库 ${dbPath}，端口 ${port}）`)
}

// ---------------------------------------------------------------- 工具函数
// 提到模块级是为了可单测（node --test 直接 import __test）。

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

export function expandUser(p) {
  if (!p) return p
  if (p === '~') return homedir()
  if (p.startsWith('~/') || p.startsWith('~\\')) return join(homedir(), p.slice(2))
  return p
}

export function sessionKey(session) {
  if (!session) return 'main'
  if (typeof session === 'string') return session
  return String(session.id ?? session.sessionId ?? session.key ?? 'main')
}

/** 从事件数据里取纯文本。宿主正文在 data.content 的 text 块里。 */
export function extractText(data) {
  if (!data) return ''
  const content = data.message?.content ?? data.content
  if (typeof content === 'string') return content.trim()
  const parts = Array.isArray(content) ? content : []
  return parts
    .filter((p) => p && (p.type === 'text' || (p.type == null && typeof p.text === 'string')))
    .map((p) => String(p.text ?? ''))
    .join('')
    .trim()
}

function explicitApiKey(config = {}) {
  const direct = String(config.apiKey || '').trim()
  if (direct) return direct
  for (const name of ['HIPPO_API_KEY', 'DEEPSEEK_API_KEY']) {
    const v = String(process.env[name] || '').trim()
    if (v) return v
  }
  return ''
}

/** 仅检查显式配置、环境变量和海马体配置，不解析 DSH 凭据文件。 */
export function resolveApiKey(config = {}) {
  const explicit = explicitApiKey(config)
  if (explicit) return explicit
  try {
    const text = readFileSync(join(homedir(), '.hippo', 'config.yaml'), 'utf8')
    const m = text.match(/api_key:\s*["']?([^\s"'#]+)/)
    if (m?.[1]) return m[1]
  } catch {
    /* 没有就继续 */
  }
  return ''
}

/** 使用 DSH 官方凭据服务，尊重其当前 DSH_HOME 与凭据存储方式。 */
export async function resolveHostApiKey(ctx, config = {}) {
  const explicit = explicitApiKey(config)
  if (explicit) return explicit
  try {
    const credentials = ctx.get?.('credentials')
    const found = await credentials?.resolve?.('DEEPSEEK_API_KEY')
    if (typeof found?.value === 'string' && found.value.trim()) return found.value.trim()
  } catch {
    // 凭据不可用时继续尝试海马体自己的配置。
  }
  return resolveApiKey(config)
}

/** 优先配置 → 包内 runtime → DSH 自带 runtime → PATH。 */
export function resolvePython(config = {}) {
  const explicit = String(config.pythonPath || '').trim()
  if (explicit && existsSync(explicit)) return explicit
  const platformDir = `${process.platform}-${process.arch}`
  const bundled = process.platform === 'win32'
    ? join(PACKAGE_ROOT, 'runtime', platformDir, 'python.exe')
    : join(PACKAGE_ROOT, 'runtime', platformDir, 'python3')
  if (existsSync(bundled)) return bundled
  const dshRuntime = findDshPython()
  if (dshRuntime) return dshRuntime
  return process.platform === 'win32' ? 'python.exe' : 'python3'
}

/** 找 DSH 自带 python（本机已装 DSH 时直接复用）。 */
export function findDshPython() {
  const base = join(homedir(), '.dsh', 'dsh-runtimes')
  if (!existsSync(base)) return ''
  try {
    for (const dir of readdirSync(base)) {
      const win = join(base, dir, 'dependencies', 'python', 'python.exe')
      if (existsSync(win)) return win
      const posix = join(base, dir, 'dependencies', 'python', 'bin', 'python3')
      if (existsSync(posix)) return posix
    }
  } catch {
    /* ignore */
  }
  return ''
}

/** 端口 p 在 127.0.0.1 上能不能绑定（内核只绑本地回环）。 */
export function portFree(p) {
  return new Promise((resolve) => {
    const probe = createServer()
    probe.unref()
    let done = false
    probe.once('error', () => {
      if (!done) { done = true; resolve(false) }
    })
    probe.listen({ host: '127.0.0.1', port: p, exclusive: true }, () => {
      probe.close(() => { if (!done) { done = true; resolve(true) } })
    })
  })
}

/**
 * 从 preferred 开始找一个空闲端口。
 *
 * 为什么必须动态选：DSH 可以同时开多个实例/多个 profile（桌面 + Web、
 * 或者用户自己再起一个 headless），而插件默认端口写死 8766。实测第二个实例
 * 的内核直接 `[Errno 10048] bind failed` 退出，记忆整段失效且界面上看不出来。
 *
 * 先顺序找 20 个（保持端口好记、和文档一致），实在不行再从高位随机挑 20 个。
 * 返回 0 表示一个都没空——调用方应把它当失败处理，而不是传给内核。
 */
export async function pickPort(preferred, span = 20) {
  const base = Number(preferred) > 0 ? Math.trunc(Number(preferred)) : DEFAULT_PORT
  for (let i = 0; i < span; i++) {
    const candidate = base + i
    if (candidate > 65535) break
    if (await portFree(candidate)) return candidate
  }
  for (let i = 0; i < span; i++) {
    const candidate = 20_000 + Math.floor(Math.random() * 40_000)
    if (await portFree(candidate)) return candidate
  }
  return 0
}

export const __test = {
  PACKAGE_ROOT,
  sleep,
  expandUser,
  sessionKey,
  extractText,
  resolveApiKey,
  resolveHostApiKey,
  resolvePython,
  findDshPython,
  portFree,
  pickPort,
}
