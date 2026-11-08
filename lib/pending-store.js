/** 待补写回合的落盘存储。
 *
 * 为什么需要：队列在内存里，DSH 重启就没了。重启时如果刚好有一批
 * `/event` 失败正在重试，那些回合会永久丢失（用户看到"少了几轮"）。
 *
 * 格式：每行一个 JSON `{session, role, text, at}`。追加写；成功补写后
 * 整份重写（原子：写临时文件再 rename）。条数上限由队列控制（100），
 * 文件不会无限涨。
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

export function loadPending(path) {
  if (!existsSync(path)) return []
  let raw = ''
  try {
    raw = readFileSync(path, 'utf8')
  } catch {
    return []
  }
  const out = []
  for (const line of raw.split('\n')) {
    const text = line.trim()
    if (!text) continue
    try {
      const item = JSON.parse(text)
      if (item && typeof item.session === 'string' && typeof item.text === 'string'
        && (item.role === 'user' || item.role === 'assistant')) {
        out.push({ session: item.session, role: item.role, text: item.text, at: item.at ?? 0 })
      }
    } catch {
      // 坏行跳过：宁可少补一条，也不要因为一行坏了整个队列起不来
    }
  }
  return out.slice(-100)
}

export function appendPending(path, item) {
  mkdirSync(dirname(path), { recursive: true })
  try {
    appendFileSync(path, `${JSON.stringify(item)}\n`, 'utf8')
  } catch {
    // 落盘失败不该影响对话：内存队列还在，最坏情况是重启后少补这一条
  }
}

export function rewritePending(path, items) {
  mkdirSync(dirname(path), { recursive: true })
  const body = items.map((item) => JSON.stringify(item)).join('\n')
  const tmp = `${path}.tmp`
  try {
    writeFileSync(tmp, body ? `${body}\n` : '', 'utf8')
    renameSync(tmp, path)
  } catch {
    try { rewritePendingBestEffort(tmp, path) } catch { /* ignore */ }
  }
}

function rewritePendingBestEffort(tmp, path) {
  try { renameSync(tmp, path) } catch { /* ignore */ }
}
