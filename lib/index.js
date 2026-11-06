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
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-plugin-hippocampus-deepseek'
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
  const port = Number(config.port || DEFAULT_PORT)
  const endpoint = `http://127.0.0.1:${port}`
  const apiKey = resolveApiKey(config)
  const kernelRoot = join(PACKAGE_ROOT, 'hippo')
  const python = resolvePython(config)

  let child = null
  let stopped = false
  let ready = false

  // 每会话：上一轮记忆文本 + 正在组装的 Promise + 写队列
  const lastText = new Map()
  const inflightWrite = new Map()

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
    rotateKernelLog()
    const args = [
      '-m', 'hippo.dsh_kernel',
      '--db', dbPath,
      '--port', String(port),
      '--parent-pid', String(process.pid),
    ]
    if (apiKey) args.push('--api-key', apiKey)
    const env = {
      ...process.env,
      PYTHONPATH: `${PACKAGE_ROOT}${process.platform === 'win32' ? ';' : ':'}${process.env.PYTHONPATH || ''}`,
      PYTHONIOENCODING: 'utf-8',
      HIPPO_LOG: 'off',
    }
    appendKernelLog(`\n[${new Date().toISOString()}] spawn ${python} ${args.join(' ')}\n`)
    try {
      child = spawn(python, args, { cwd: PACKAGE_ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
      child.stdout?.on('data', (b) => appendKernelLog(String(b)))
      child.stderr?.on('data', (b) => appendKernelLog(String(b)))
      child.on('exit', (code) => {
        appendKernelLog(`[${new Date().toISOString()}] kernel exit ${code}\n`)
        ready = false
        if (!stopped) {
          child = null
          setTimeout(() => { if (!stopped) void ensureReady() }, 1500)
        }
      })
      await waitReady()
    } catch (error) {
      warn(`内核拉起失败：${error instanceof Error ? error.message : String(error)}`)
    }
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
        if (res?.ok) {
          ready = true
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
    if (!child) await startKernel()
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
      // 用户消息：先串行写库，写完立刻组装本轮记忆（缓存给下一轮 section）
      const prev = inflightWrite.get(key) || Promise.resolve()
      const job = prev
        .then(() => remember(key, 'user', text))
        .then(() => assemble(key, text))
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

  // ------------------------------------------------------------ 生命周期
  ctx.effect(() => {
    void ensureReady()
    const timer = setInterval(() => {
      if (stopped) return
      if (!child) void ensureReady()
    }, 10_000)
    timer.unref?.()
    return () => {
      stopped = true
      clearInterval(timer)
      stopKernel()
    }
  }, 'hippocampus-deepseek: kernel lifecycle')

  log(`已挂载（库 ${dbPath}，端口 ${port}，key ${apiKey ? '有' : '无（只记录不抽取）'}）`)
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
  if (typeof data.content === 'string') return data.content.trim()
  const parts = Array.isArray(data.content) ? data.content : []
  return parts
    .filter((p) => p && (p.type === 'text' || typeof p.text === 'string'))
    .map((p) => String(p.text ?? ''))
    .join('')
    .trim()
}

/** 优先配置 → 环境变量 → DSH 凭据 → hippo 配置。 */
export function resolveApiKey(config = {}) {
  const direct = String(config.apiKey || '').trim()
  if (direct) return direct
  for (const name of ['HIPPO_API_KEY', 'DEEPSEEK_API_KEY']) {
    const v = String(process.env[name] || '').trim()
    if (v) return v
  }
  try {
    const text = readFileSync(join(homedir(), '.dsh', '.credentials.yaml'), 'utf8')
    const m = text.match(/DEEPSEEK_API_KEY:\s*([^\s#]+)/)
    if (m?.[1]) return m[1]
  } catch {
    /* 没有就继续 */
  }
  try {
    const text = readFileSync(join(homedir(), '.hippo', 'config.yaml'), 'utf8')
    const m = text.match(/api_key:\s*["']?([^\s"'#]+)/)
    if (m?.[1]) return m[1]
  } catch {
    /* 没有就继续 */
  }
  return ''
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

export const __test = {
  PACKAGE_ROOT,
  sleep,
  expandUser,
  sessionKey,
  extractText,
  resolveApiKey,
  resolvePython,
  findDshPython,
}
