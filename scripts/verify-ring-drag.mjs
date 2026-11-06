// 验收：星环平面拖拽。用 CDP 真按鼠标，读引擎内部量，证明"只转环、不倾场景"。
import { spawn } from 'node:child_process'

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9334
const PAGE = 'http://127.0.0.1:8794/board'
const PROFILE = 'C:\\h\\edge-cdp-profile2'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitDebugger(timeoutMs = 20000) {
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

const child = spawn(EDGE, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' })

const report = { ok: false, steps: [] }
try {
  await waitDebugger()
  const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(PAGE)}`, { method: 'PUT' })).json()
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
  await sleep(3500)

  // 关掉场景自转，让 yaw/pitch 的变化只可能来自拖拽
  await evaluate(`window.__hippoBoard.autoRotate = false`)
  await sleep(200)
  const geom = await evaluate(`(() => {
    const e = window.__hippoBoard;
    if (!e) return null;
    const r = e.ringRadius();
    return { cx: e.somaScreen.x, cy: e.somaScreen.y, R: r, angle: e.ringAngle, yaw: e.yaw, pitch: e.pitch, doubts: e.doubts.length };
  })()`)
  report.steps.push({ geom })
  if (!geom) throw new Error('window.__hippoBoard 不存在')

  // 在环上取三个点做一次真实的按下 → 拖动 → 抬起
  const startX = Math.round(geom.cx + geom.R), startY = Math.round(geom.cy)
  const midA = -0.7, endA = -1.5
  const pt = (a) => [Math.round(geom.cx + Math.cos(a) * geom.R), Math.round(geom.cy + Math.sin(a) * geom.R)]
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: startX, y: startY, button: 'left', clickCount: 1, buttons: 1 })
  for (const a of [-0.25, midA, endA]) {
    const [x, y] = pt(a)
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'left', buttons: 1 })
    await sleep(60)
  }
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: pt(endA)[0], y: pt(endA)[1], button: 'left', clickCount: 1, buttons: 0 })
  await sleep(300)

  const after = await evaluate(`(() => {
    const e = window.__hippoBoard;
    return { angle: e.ringAngle, yaw: e.yaw, pitch: e.pitch };
  })()`)
  report.steps.push({ after })

  const dAngle = Math.abs(after.angle - geom.angle)
  const dYaw = Math.abs(after.yaw - geom.yaw)
  const dPitch = Math.abs(after.pitch - geom.pitch)
  report.delta = { angle: Number(dAngle.toFixed(4)), yaw: Number(dYaw.toFixed(4)), pitch: Number(dPitch.toFixed(4)) }
  report.ok = dAngle > 0.15 && dYaw < 0.01 && dPitch < 0.01
  ws.close()
} catch (error) {
  report.error = String(error)
} finally {
  try { child.kill() } catch { /* ignore */ }
}

console.log(JSON.stringify(report, null, 2))
