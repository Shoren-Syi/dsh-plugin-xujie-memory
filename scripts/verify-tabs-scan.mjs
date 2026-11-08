// 逐个标签扫控制台报错：console.error / 未捕获异常 / 请求失败
import { spawn } from 'node:child_process'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9341
const PAGE = process.env.TABS_PAGE || 'http://127.0.0.1:8837/board'
const PROFILE = 'C:\\h\\edge-cdp-profile5'
const TABS = ['events', 'facts', 'traits', 'doubts', 'qq', 'status']
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const child = spawn(EDGE, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' })

const report = { tabs: {}, errors: [] }
try {
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/json/version`)).ok) break } catch { /* wait */ }
    await sleep(300)
  }
  const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(PAGE)}`,
    { method: 'PUT' })).json()
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return }
    if (msg.method === 'Runtime.exceptionThrown') {
      report.errors.push({ type: 'exception', text: msg.params?.exceptionDetails?.exception?.description?.slice(0, 200) })
    }
    if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params?.type)) {
      report.errors.push({ type: msg.params.type, text: (msg.params.args || []).map((a) => a.value ?? a.description).join(' ').slice(0, 200) })
    }
    if (msg.method === 'Network.loadingFailed') {
      report.errors.push({ type: 'request_failed', text: (msg.params?.errorText || '') + ' ' + (msg.params?.requestId || '') })
    }
  }
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  const send = (method, params = {}) => new Promise((res) => {
    const myId = ++id
    pending.set(myId, (m) => res(m.result ?? m.error ?? {}))
    ws.send(JSON.stringify({ id: myId, method, params }))
  })
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
    return r?.result?.value
  }
  await send('Runtime.enable')
  await send('Page.enable')
  await send('Network.enable')
  await sleep(3000)
  for (const tab of TABS) {
    const before = report.errors.length
    const clicked = await evaluate(`(() => { const b = document.querySelector('[data-tab="${tab}"]'); if (!b) return false; b.click(); return true })()`)
    await sleep(1800)
    report.tabs[tab] = {
      clicked,
      title: await evaluate('document.getElementById("drawerTitle")?.textContent || ""'),
      rows: await evaluate('document.querySelectorAll(".list .row").length'),
      newErrors: report.errors.slice(before),
    }
  }
  // 点开第一条事件，验证详情面板不炸
  const beforeDetail = report.errors.length
  await evaluate(`(() => { const b = document.querySelector('[data-tab="events"]'); b.click(); return true })()`)
  await sleep(1200)
  await evaluate(`(() => { const r = document.querySelector('.list .row'); if (r) r.click(); return true })()`)
  await sleep(1500)
  report.detail = {
    opened: await evaluate('!!document.getElementById("inspBody")?.innerText'),
    text: (await evaluate('document.getElementById("inspBody")?.innerText'))?.slice(0, 200),
    newErrors: report.errors.slice(beforeDetail),
  }
  ws.close()
} catch (error) {
  report.error = String(error)
} finally {
  try { child.kill() } catch { /* ignore */ }
}
console.log(JSON.stringify(report, null, 2))
