// 用 Edge headless + CDP 验证记忆看板的真实交互（无 puppeteer 依赖）
// 用法: node verify_board.mjs
import { spawn } from 'node:child_process'
import fs from 'node:fs'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9333
const PAGE = 'http://127.0.0.1:8794/board'
const PROFILE = 'C:\\h\\edge-cdp-profile'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitDebugger(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (r.ok) return await r.json()
    } catch { /* not up */ }
    await sleep(300)
  }
  throw new Error('debugger not ready')
}

async function newTarget() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })
  if (!r.ok) throw new Error(`new target failed ${r.status}`)
  return await r.json()
}

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, 'about:blank',
], { stdio: 'ignore', windowsHide: true })

const errors = []
try {
  await waitDebugger()
  const target = await newTarget()
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

  let seq = 0
  const pending = new Map()
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails
      errors.push('exception: ' + (d.exception?.description || d.text || 'unknown'))
    }
    if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
      errors.push('log: ' + msg.params.entry.text)
    }
  }
  const send = (method, params = {}) => new Promise((res) => {
    const id = ++seq
    pending.set(id, res)
    ws.send(JSON.stringify({ id, method, params }))
  })
  const evaluate = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (r.result?.exceptionDetails) return '__ERR__ ' + (r.result.exceptionDetails.text || '')
    return r.result?.result?.value
  }

  await send('Page.enable')
  await send('Runtime.enable')
  await send('Log.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
  await send('Page.navigate', { url: PAGE })

  // 等数据加载（nav-count 有数字）
  let counts = ''
  for (let i = 0; i < 60; i++) {
    counts = await evaluate(`[...document.querySelectorAll('.nav-count')].map(x=>x.textContent.trim()).join('/')`)
    if (counts && /\\d/.test(counts)) break
    await sleep(400)
  }

  const readRows = () => evaluate(`document.querySelectorAll('#inspBody .row').length`)
  const title = () => evaluate(`document.getElementById('drawerTitle')?.textContent||''`)

  // 1) 事件列表
  await evaluate(`document.querySelector('.nav-item[data-tab="events"]')?.click()`)
  await sleep(700)
  const eventRows = await readRows()
  const eventTitle = await title()

  // 2) 事实列表
  await evaluate(`document.querySelector('.nav-item[data-tab="facts"]')?.click()`)
  await sleep(500)
  const factRows = await readRows()

  // 3) 人物列表
  await evaluate(`document.querySelector('.nav-item[data-tab="entities"]')?.click()`)
  await sleep(500)
  const entityRows = await readRows()

  // 4) 搜索过滤（先切回事件页）
  await evaluate(`document.querySelector('.nav-item[data-tab="events"]')?.click()`)
  await sleep(400)
  await evaluate(`(() => {
    const q = document.getElementById('q');
    q.value = 'ITX';
    q.dispatchEvent(new Event('input', { bubbles: true }));
  })()`)
  await sleep(600)
  const searchRows = await readRows()

  // 5) 清空并打开详情
  await evaluate(`document.getElementById('btnClearQ')?.click()`)
  await sleep(400)
  await evaluate(`document.querySelectorAll('.nav-item[data-tab="events"]')[0]?.click()`)
  await sleep(400)
  const firstRowClick = await evaluate(`(() => {
    const row = document.querySelector('#inspBody .row');
    if (!row) return 'no-row';
    row.click(); return row.dataset.id || 'clicked';
  })()`)
  await sleep(600)
  const detailTitle = await title()
  const detailVisible = await evaluate(`!!document.querySelector('#inspBody .detail-head, #inspBody .content')`)

  // 6) 截图（列表 + 详情）
  const shot1 = await send('Page.captureScreenshot', { format: 'png' })
  if (shot1.result?.data) fs.writeFileSync('C:\\h\\board_detail.png', Buffer.from(shot1.result.data, 'base64'))
  await evaluate(`document.getElementById('btnBack')?.click() || document.querySelector('[data-jump]')?.click()`)
  await sleep(500)
  const shot2 = await send('Page.captureScreenshot', { format: 'png' })
  if (shot2.result?.data) fs.writeFileSync('C:\\h\\board_list.png', Buffer.from(shot2.result.data, 'base64'))

  const result = {
    counts,
    eventRows, eventTitle,
    factRows, entityRows,
    searchRows,
    firstRowClick, detailTitle, detailVisible,
    errors,
    shots: ['C:\\h\\board_detail.png', 'C:\\h\\board_list.png'],
  }
  console.log(JSON.stringify(result, null, 2))
  ws.close()
} catch (e) {
  console.error('VERIFY_FAILED', e.message)
  console.log(JSON.stringify({ errors }, null, 2))
} finally {
  child.kill()
  await sleep(300)
}
