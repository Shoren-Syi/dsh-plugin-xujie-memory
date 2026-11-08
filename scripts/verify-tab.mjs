// 通用标签页验收：切到指定 tab，读内容，截图。
import { spawn } from 'node:child_process'
import fs from 'node:fs'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9338
const PAGE = process.env.TAB_PAGE || 'http://127.0.0.1:8834/board'
const TAB = process.env.TAB_NAME || 'status'
const SHOT = process.env.TAB_SHOT || `C:\\h\\stress\\tab-${TAB}.png`
const PROFILE = 'C:\\h\\edge-cdp-profile4'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const child = spawn(EDGE, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' })

const report = { ok: false }
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
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
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
  await sleep(3000)
  await evaluate(`document.querySelector('[data-tab="${TAB}"]').click()`)
  await sleep(2000)
  report.tabTitle = await evaluate(`document.getElementById("drawerTitle").textContent`)
  report.bodyText = (await evaluate(`document.getElementById("inspBody").innerText`))?.slice(0, 600)
  const shot = await send('Page.captureScreenshot', { format: 'png' })
  if (shot?.data) fs.writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
  report.shot = SHOT
  report.ok = Boolean(report.bodyText && report.bodyText.length > 20)
  ws.close()
} catch (error) {
  report.error = String(error)
} finally {
  try { child.kill() } catch { /* ignore */ }
}
console.log(JSON.stringify(report, null, 2))
