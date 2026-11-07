// 验收 QQ 标签页：点开、填表、保存、读回状态，并截图。
import { spawn } from 'node:child_process'
import fs from 'node:fs'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9336
const PAGE = process.env.QQ_PAGE || 'http://127.0.0.1:8832/board'
const PROFILE = 'C:\\h\\edge-cdp-profile3'
const SHOT = 'C:\\h\\stress\\qq-tab.png'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const child = spawn(EDGE, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' })

const report = { ok: false, steps: [] }
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

  report.steps.push({ before: await evaluate('document.title') })
  await evaluate(`document.querySelector('[data-tab="qq"]').click()`)
  await sleep(1500)
  report.steps.push({ tabTitle: await evaluate(`document.getElementById("drawerTitle").textContent`) })
  report.steps.push({ formFields: await evaluate(`document.querySelectorAll('.qq-input').length`) })

  // 填表并保存（用明显假的地址，只验证链路，不会真连）
  await evaluate(`(() => {
    const set = (id, v) => { const el = document.getElementById(id); el.value = v; };
    set('qqSelf', '10001'); set('qqWs', 'ws://127.0.0.1:3001'); set('qqHttp', 'http://127.0.0.1:3000');
    set('qqPrivate', '20002'); set('qqGroup', '30003');
    set('qqProvider', 'deepseek-official'); set('qqModel', 'deepseek-flash'); set('qqMaxTokens', '800');
    set('qqDailyCalls', '20'); set('qqPerRequest', '0.1'); set('qqDaily', '1'); set('qqMonthly', '10');
    set('qqInPrice', '1'); set('qqOutPrice', '2');
  })()`)
  await evaluate(`document.getElementById('qqSave').click()`)
  await sleep(2500)
  report.steps.push({ afterSave: await evaluate(`document.querySelector('.qq-status')?.innerText?.replace(/\\s+/g,' ')`) })
  report.steps.push({ errors: await evaluate(`document.querySelector('.qq-error')?.textContent || ''`) })

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  if (shot?.data) fs.writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
  report.shot = SHOT
  report.ok = report.steps.some(s => s.formFields >= 8)
  ws.close()
} catch (error) {
  report.error = String(error)
} finally {
  try { child.kill() } catch { /* ignore */ }
}
console.log(JSON.stringify(report, null, 2))
