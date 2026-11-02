import { buildMorphology, SOMA_RADIUS, STRATA, type LayerIndex, type Morphology } from './morphology.ts'

/** One real memory record placed in the field. */
export interface FieldNode {
  id: string
  layer: LayerIndex
  /** Read-only derived records (state/summary/clarification) render as diamonds. */
  derived: boolean
  /** 0..1 visual weight from importance/priority. */
  weight: number
  label: string
  kindLabel: string
}

/** One real relation returned by the snapshot. */
export interface FieldEdge { source: string; target: string }

export interface EngineOptions {
  reducedMotion: boolean
  onSelect: (id: string | null) => void
  /** Accessible layer captions drawn inside the field. */
  layerCaptions: readonly [string, string, string]
}

type RGB = readonly [number, number, number]
/** Semantic: cold blue. Episodic: pale amber. Working: cyan-teal. Trunk: silver. */
export const LAYER_RGB: readonly RGB[] = [[132, 168, 255], [238, 196, 128], [96, 220, 200], [206, 216, 236]]

const BANDS = 6
const LIGHT_CLASSES = 3
const WIDTH_CLASSES = 4
const GROUPS = 4
const EDGE_SAMPLES = 20
const TAU = Math.PI * 2

interface Pulse { path: Int32Array; cum: Float32Array; total: number; t: number; speed: number; group: number; node: number }
interface EdgePulse { edge: number; t: number; speed: number }

function sprite(rgb: RGB, core: number): HTMLCanvasElement {
  const c = document.createElement('canvas')
  c.width = c.height = 64
  const g = c.getContext('2d')!
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32)
  grd.addColorStop(0, `rgba(255,255,255,${core})`)
  grd.addColorStop(0.12, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.9)`)
  grd.addColorStop(0.35, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.28)`)
  grd.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`)
  g.fillStyle = grd
  g.fillRect(0, 0, 64, 64)
  return c
}

/**
 * Canvas renderer with genuine 3D geometry: every vertex is rotated by the
 * camera (yaw/pitch), perspective-projected and painted back-to-front in
 * depth bands so the soma occludes fibres and records behind it.
 */
export class NeuralEngine {
  private readonly ctx: CanvasRenderingContext2D
  private readonly m: Morphology
  private readonly sx: Float32Array
  private readonly sy: Float32Array
  private readonly sz: Float32Array
  private readonly sp: Float32Array
  private readonly light: Float32Array
  private readonly segWidth: Uint8Array
  private readonly buckets: Int32Array[]
  private readonly bucketLen: Int32Array
  private readonly sprites: HTMLCanvasElement[]
  private readonly whiteSprite: HTMLCanvasElement

  private nodes: FieldNode[] = []
  private nodeIndex = new Map<string, number>()
  private nodeVertex = new Int32Array(0)
  private nx = new Float32Array(0)
  private ny = new Float32Array(0)
  private nz = new Float32Array(0)
  private nsx = new Float32Array(0)
  private nsy = new Float32Array(0)
  private nsz = new Float32Array(0)
  private nsp = new Float32Array(0)
  private nOcc = new Uint8Array(0)
  private flash = new Float32Array(0)
  private order: number[] = []
  private edges: Array<{ s: number; t: number; layer: number }> = []
  private edgePts = new Float32Array(0)
  private edgeScr = new Float32Array(0)
  private neighbours = new Set<number>()
  private pulses: Pulse[] = []
  private edgePulses: EdgePulse[] = []
  private pathCache = new Map<number, { path: Int32Array; cum: Float32Array; total: number }>()

  private width = 1
  private height = 1
  private dpr = 1
  private yaw = 0.55
  private pitch = 0.34
  private yawVel = 0
  private pitchVel = 0
  private zoom = 1
  private panX = 0
  private panY = 0
  private yawTarget: number | null = null
  private autoRotate = true
  private time = 0
  private last = 0
  private raf = 0
  private running = false
  private dirty = true
  private visible = true
  private selected = -1
  private hover = -1
  private fading = -1
  private fadeUntil = 0
  private highlight: Set<number> | null = null
  private dimmed = false
  private readonly pointers = new Map<number, { x: number; y: number }>()
  private press: { x: number; y: number; t: number; moved: boolean; button: number; shift: boolean } | null = null
  private pinch = 0
  private readonly disposers: Array<() => void> = []
  private somaScreen = { x: 0, y: 0, r: 0, z: 0 }
  private fBase = 1
  private captionBoxes: Array<{ x: number; y: number; width: number; height: number }> = []

  constructor(private readonly canvas: HTMLCanvasElement, private readonly tooltip: HTMLElement, private opts: EngineOptions) {
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) throw new Error('canvas_2d_unavailable')
    this.ctx = ctx
    this.m = buildMorphology()
    const n = this.m.count
    this.sx = new Float32Array(n); this.sy = new Float32Array(n); this.sz = new Float32Array(n); this.sp = new Float32Array(n)
    this.light = new Float32Array(n)
    this.segWidth = new Uint8Array(this.m.segCount)
    for (let i = 0; i < this.m.segCount; i++) {
      const b = this.m.segB[i]!, rad = this.m.r[b]!
      this.segWidth[i] = this.m.kind[b] === 1 ? 3 : rad >= 2.6 ? 0 : rad >= 1.3 ? 1 : rad >= 0.6 ? 2 : 3
    }
    const bucketCount = GROUPS * BANDS * WIDTH_CLASSES * LIGHT_CLASSES
    this.buckets = Array.from({ length: bucketCount }, () => new Int32Array(this.m.segCount))
    this.bucketLen = new Int32Array(bucketCount)
    this.sprites = LAYER_RGB.map(rgb => sprite(rgb, 0.95))
    this.whiteSprite = sprite([235, 242, 255], 1)
    this.autoRotate = !opts.reducedMotion
    this.bindInput()
    this.seedPulses()
  }

  // ---------- public API ----------

  setOptions(opts: EngineOptions): void {
    const motionChanged = opts.reducedMotion !== this.opts.reducedMotion
    this.opts = opts
    if (motionChanged) { this.autoRotate = !opts.reducedMotion; this.pulses = []; this.edgePulses = []; if (!opts.reducedMotion) this.seedPulses() }
    this.invalidate()
  }

  setData(nodes: FieldNode[], edges: FieldEdge[]): void {
    const selectedId = this.selected >= 0 ? this.nodes[this.selected]?.id : undefined
    const hl = this.highlight ? [...this.highlight].map(i => this.nodes[i]?.id).filter(Boolean) as string[] : null
    this.nodes = nodes
    this.hover = -1; this.fading = -1; this.fadeUntil = 0
    this.nodeIndex = new Map(nodes.map((nd, i) => [nd.id, i]))
    const count = nodes.length
    this.nodeVertex = new Int32Array(count)
    this.nx = new Float32Array(count); this.ny = new Float32Array(count); this.nz = new Float32Array(count)
    this.nsx = new Float32Array(count); this.nsy = new Float32Array(count); this.nsz = new Float32Array(count); this.nsp = new Float32Array(count)
    this.nOcc = new Uint8Array(count)
    this.flash = new Float32Array(count)
    this.placeNodes()
    this.edges = []
    for (const e of edges) {
      const s = this.nodeIndex.get(e.source), t = this.nodeIndex.get(e.target)
      if (s === undefined || t === undefined || s === t) continue
      this.edges.push({ s, t, layer: nodes[t]!.layer })
    }
    this.buildEdgeCurves()
    this.selected = selectedId !== undefined ? (this.nodeIndex.get(selectedId) ?? -1) : -1
    this.highlight = hl ? new Set(hl.map(id => this.nodeIndex.get(id)).filter((v): v is number => v !== undefined)) : null
    this.refreshNeighbours()
    this.pathCache.clear()
    this.pulses = []; this.edgePulses = []
    if (!this.opts.reducedMotion) this.seedPulses()
    this.invalidate()
  }

  setSelected(id: string | null, focus = true): void {
    const idx = id === null ? -1 : (this.nodeIndex.get(id) ?? -1)
    this.selected = idx
    this.hideTooltip()
    this.refreshNeighbours()
    if (idx >= 0 && focus) {
      const target = Math.atan2(-this.nx[idx]!, -this.nz[idx]!)
      if (this.opts.reducedMotion) { this.yaw = target; this.yawTarget = null } else this.yawTarget = target
      this.flash[idx] = 1
    }
    this.invalidate()
  }

  /** Emphasise search hits; null clears. Returns how many hits are inside the field. */
  setHighlight(ids: readonly string[] | null): number {
    if (!ids) { this.highlight = null; this.invalidate(); return 0 }
    const set = new Set<number>()
    for (const id of ids) {
      const i = this.nodeIndex.get(id)
      if (i !== undefined) set.add(i)
      // A raw trace may also be projected as anchor/recent nodes.
      for (const prefix of ['anchor:', 'working:']) { const j = this.nodeIndex.get(prefix + id); if (j !== undefined) set.add(j) }
    }
    this.highlight = set
    this.invalidate()
    return set.size
  }

  setDimmed(dimmed: boolean): void { this.dimmed = dimmed; this.invalidate() }
  async fadeNode(id: string): Promise<void> {
    const index = this.nodeIndex.get(id)
    if (index === undefined || this.opts.reducedMotion) return
    this.fading = index
    this.fadeUntil = performance.now() + 300
    this.invalidate()
    await new Promise<void>(resolve => setTimeout(resolve, 300))
    this.fading = -1
    this.invalidate()
  }
  setAutoRotate(on: boolean): void { this.autoRotate = on && !this.opts.reducedMotion; this.invalidate() }
  get isAutoRotating(): boolean { return this.autoRotate }
  zoomBy(k: number): void { this.zoomAt(this.width / 2, this.height / 2, k) }
  resetView(): void { this.yaw = 0.55; this.pitch = 0.34; this.zoom = 1; this.panX = 0; this.panY = 0; this.yawTarget = null; this.yawVel = this.pitchVel = 0; this.invalidate() }

  resize(width: number, height: number): void {
    this.dpr = Math.min(2, window.devicePixelRatio || 1)
    this.width = Math.max(1, width); this.height = Math.max(1, height)
    this.canvas.width = Math.round(this.width * this.dpr)
    this.canvas.height = Math.round(this.height * this.dpr)
    this.fBase = Math.min(this.width / 1060, this.height / 900)
    this.invalidate()
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.last = performance.now()
    const loop = (now: number) => {
      if (!this.running) return
      this.raf = requestAnimationFrame(loop)
      const dt = Math.min(0.05, (now - this.last) / 1000)
      this.last = now
      if (!this.visible || document.hidden) return
      const animating = !this.opts.reducedMotion
      if (animating || this.dirty || this.press || Math.abs(this.yawVel) > 1e-4 || Math.abs(this.pitchVel) > 1e-4 || this.yawTarget !== null) {
        this.step(dt)
        this.render()
        this.dirty = false
      }
    }
    this.raf = requestAnimationFrame(loop)
  }

  setVisible(v: boolean): void { this.visible = v; if (v) this.invalidate() }

  dispose(): void {
    this.running = false
    cancelAnimationFrame(this.raf)
    for (const d of this.disposers) d()
    this.disposers.length = 0
  }

  /** Test/QA hook: projected screen position of a node, or null. */
  screenOf(id: string): { x: number; y: number } | null {
    const i = this.nodeIndex.get(id)
    return i === undefined ? null : { x: this.nsx[i]!, y: this.nsy[i]! }
  }

  /** Test/QA hook: camera state proves the motion is a 3D rotation, not a 2D canvas spin. */
  cameraState(): { yaw: number; pitch: number; zoom: number } { return { yaw: this.yaw, pitch: this.pitch, zoom: this.zoom } }

  /** Screen-space caption bounds for layout regression checks. */
  captionBounds() { return this.captionBoxes.map(b => ({ ...b })) }

  // ---------- data placement ----------

  private placeNodes(): void {
    const used = new Set<number>()
    const placed: Array<[number, number, number]> = []
    const hash = (s: string) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) } return h >>> 0 }
    const minGap = 13
    this.nodes.forEach((node, i) => {
      const sites = this.m.sites[node.layer]
      let h = hash(node.id), chosen = -1
      for (let attempt = 0; attempt < sites.length && attempt < 900; attempt++) {
        const v = sites[(h + attempt * 2654435761) % sites.length >>> 0]!
        if (used.has(v)) continue
        const px = this.m.x[v]!, py = this.m.y[v]!, pz = this.m.z[v]!
        const gap = attempt < 400 ? minGap : minGap * 0.4
        if (placed.some(p => (p[0] - px) ** 2 + (p[1] - py) ** 2 + (p[2] - pz) ** 2 < gap * gap)) continue
        chosen = v
        break
      }
      if (chosen < 0) chosen = sites[h % Math.max(1, sites.length)] ?? 0
      used.add(chosen)
      // A bouton sits just beside its dendrite, offset along a stable direction.
      const a = ((h >>> 8) % 628) / 100, b = ((h >>> 3) % 314) / 100 - 1.57
      const off = 3.5 + this.m.r[chosen]!
      this.nodeVertex[i] = chosen
      this.nx[i] = this.m.x[chosen]! + Math.cos(a) * Math.cos(b) * off
      this.ny[i] = this.m.y[chosen]! + Math.sin(b) * off
      this.nz[i] = this.m.z[chosen]! + Math.sin(a) * Math.cos(b) * off
      placed.push([this.nx[i]!, this.ny[i]!, this.nz[i]!])
    })
  }

  private buildEdgeCurves(): void {
    const e = this.edges.length
    this.edgePts = new Float32Array(e * EDGE_SAMPLES * 3)
    this.edgeScr = new Float32Array(e * EDGE_SAMPLES * 3)
    this.edges.forEach((edge, k) => {
      const ax = this.nx[edge.s]!, ay = this.ny[edge.s]!, az = this.nz[edge.s]!
      const bx = this.nx[edge.t]!, by = this.ny[edge.t]!, bz = this.nz[edge.t]!
      // Arcs bow toward the neuron axis so inter-stratum links read as passing through the core.
      const cx = (ax + bx) * 0.28, cy = (ay + by) * 0.5, cz = (az + bz) * 0.28
      for (let j = 0; j < EDGE_SAMPLES; j++) {
        const t = j / (EDGE_SAMPLES - 1), u = 1 - t
        const o = (k * EDGE_SAMPLES + j) * 3
        this.edgePts[o] = u * u * ax + 2 * u * t * cx + t * t * bx
        this.edgePts[o + 1] = u * u * ay + 2 * u * t * cy + t * t * by
        this.edgePts[o + 2] = u * u * az + 2 * u * t * cz + t * t * bz
      }
    })
  }

  private refreshNeighbours(): void {
    this.neighbours = new Set()
    const focus = this.hover >= 0 ? this.hover : this.selected
    if (focus < 0) return
    for (const e of this.edges) {
      if (e.s === focus) this.neighbours.add(e.t)
      if (e.t === focus) this.neighbours.add(e.s)
    }
  }

  // ---------- pulses ----------

  private pathTo(v: number) {
    let cached = this.pathCache.get(v)
    if (cached) return cached
    const list: number[] = []
    for (let c = v; c >= 0 && list.length < 400; c = this.m.parent[c]!) list.push(c)
    list.reverse()
    const path = Int32Array.from(list)
    const cum = new Float32Array(path.length)
    // The soma centre is the implicit origin of every path.
    let total = Math.hypot(this.m.x[path[0]!]!, this.m.y[path[0]!]!, this.m.z[path[0]!]!) - SOMA_RADIUS * 0.5
    cum[0] = total
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1]!, b = path[i]!
      total += Math.hypot(this.m.x[b]! - this.m.x[a]!, this.m.y[b]! - this.m.y[a]!, this.m.z[b]! - this.m.z[a]!)
      cum[i] = total
    }
    cached = { path, cum, total }
    this.pathCache.set(v, cached)
    return cached
  }

  private spawnPulse(): Pulse {
    const r = Math.random()
    let v: number, node = -1
    if (this.nodes.length && r < 0.55) {
      node = Math.floor(Math.random() * this.nodes.length)
      v = this.nodeVertex[node]!
    } else {
      const layer = Math.floor(Math.random() * 3) as LayerIndex
      const sites = this.m.sites[layer]
      v = sites[Math.floor(Math.random() * sites.length)] ?? 0
    }
    const p = this.pathTo(v)
    return { ...p, t: Math.random() * 0.15 * p.total, speed: 150 + Math.random() * 170, group: this.m.group[v]!, node }
  }

  private seedPulses(): void {
    const target = 46
    this.pulses = Array.from({ length: target }, () => { const p = this.spawnPulse(); p.t = Math.random() * p.total; return p })
    this.edgePulses = []
  }

  // ---------- input ----------

  private bindInput(): void {
    const c = this.canvas
    const on = <K extends keyof HTMLElementEventMap>(el: HTMLElement, type: K, fn: (e: HTMLElementEventMap[K]) => void, opt?: AddEventListenerOptions) => {
      el.addEventListener(type, fn as EventListener, opt)
      this.disposers.push(() => el.removeEventListener(type, fn as EventListener, opt))
    }
    const local = (e: PointerEvent | WheelEvent | MouseEvent) => { const b = c.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top } }
    on(c, 'pointerdown', e => {
      c.setPointerCapture(e.pointerId)
      const p = local(e)
      this.pointers.set(e.pointerId, p)
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()]
        this.pinch = Math.hypot(a!.x - b!.x, a!.y - b!.y)
      }
      this.press = { x: p.x, y: p.y, t: performance.now(), moved: false, button: e.button, shift: e.shiftKey }
      this.yawTarget = null
      this.yawVel = this.pitchVel = 0
    })
    on(c, 'pointermove', e => {
      const p = local(e)
      const prev = this.pointers.get(e.pointerId)
      if (!prev) { this.updateHover(p.x, p.y); return }
      this.pointers.set(e.pointerId, p)
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()]
        const d = Math.hypot(a!.x - b!.x, a!.y - b!.y)
        if (this.pinch > 0) this.zoomAt((a!.x + b!.x) / 2, (a!.y + b!.y) / 2, d / this.pinch)
        this.pinch = d
        if (this.press) this.press.moved = true
        return
      }
      const dx = p.x - prev.x, dy = p.y - prev.y
      if (this.press && Math.hypot(p.x - this.press.x, p.y - this.press.y) > 4) this.press.moved = true
      if (this.press && (this.press.button === 1 || this.press.button === 2 || this.press.shift)) {
        this.panX += dx; this.panY += dy
      } else {
        this.yaw += dx * 0.0062
        this.pitch = Math.max(-1.25, Math.min(1.25, this.pitch + dy * 0.0055))
        this.yawVel = dx * 0.0062 * 60
        this.pitchVel = dy * 0.0055 * 60
      }
      this.hideTooltip()
      this.invalidate()
    })
    const end = (e: PointerEvent) => {
      const press = this.press
      this.pointers.delete(e.pointerId)
      if (this.pointers.size < 2) this.pinch = 0
      if (this.pointers.size === 0) this.press = null
      // pointercancel = the browser took the gesture (e.g. a pan-y scroll on touch): never treat it as a click.
      if (e.type !== 'pointercancel' && press && !press.moved && performance.now() - press.t < 600 && press.button === 0) {
        const p = local(e)
        const hit = this.pick(p.x, p.y)
        this.opts.onSelect(hit >= 0 ? this.nodes[hit]!.id : null)
      }
      if (this.opts.reducedMotion) { this.yawVel = this.pitchVel = 0 }
    }
    on(c, 'pointerup', end)
    on(c, 'pointercancel', end)
    on(c, 'pointerleave', () => { if (!this.press) { this.hover = -1; this.refreshNeighbours(); this.hideTooltip(); this.invalidate() } })
    on(c, 'contextmenu', e => e.preventDefault())
    on(c, 'wheel', e => {
      // In the narrow/stacked layout the canvas is pan-y: a plain wheel scrolls the panel instead of
      // trapping it; pinch (ctrl+wheel) and the toolbar still zoom.
      if (!e.ctrlKey && getComputedStyle(c).touchAction === 'pan-y') return
      e.preventDefault()
      const p = local(e)
      this.zoomAt(p.x, p.y, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0014)))
    }, { passive: false })
    on(c, 'dblclick', () => this.resetView())
    on(c, 'keydown', e => {
      const k = e.key
      if (k === 'ArrowLeft') this.yaw -= 0.12
      else if (k === 'ArrowRight') this.yaw += 0.12
      else if (k === 'ArrowUp') this.pitch = Math.max(-1.25, this.pitch - 0.1)
      else if (k === 'ArrowDown') this.pitch = Math.min(1.25, this.pitch + 0.1)
      else if (k === '+' || k === '=') this.zoomBy(1.15)
      else if (k === '-' || k === '_') this.zoomBy(1 / 1.15)
      else if (k === '0') this.resetView()
      else return
      e.preventDefault()
      this.yawTarget = null
      this.invalidate()
    })
  }

  private zoomAt(mx: number, my: number, k: number): void {
    const next = Math.max(0.55, Math.min(3.4, this.zoom * k))
    const kk = next / this.zoom
    const cx = this.width / 2, cy = this.height / 2
    this.panX = mx - cx - (mx - cx - this.panX) * kk
    this.panY = my - cy - (my - cy - this.panY) * kk
    if (next <= 1.0001) { this.panX *= 0.85; this.panY *= 0.85 }
    this.zoom = next
    this.invalidate()
  }

  private pick(x: number, y: number): number {
    let best = -1, bestScore = Infinity
    for (let i = 0; i < this.nodes.length; i++) {
      if (this.nOcc[i]) continue
      const d = Math.hypot(this.nsx[i]! - x, this.nsy[i]! - y)
      const reach = Math.max(12, this.nodeRadius(i) + 8)
      if (d > reach) continue
      const score = d + this.nsz[i]! * 0.004
      if (score < bestScore) { bestScore = score; best = i }
    }
    return best
  }

  private updateHover(x: number, y: number): void {
    const hit = this.pick(x, y)
    if (hit !== this.hover) { this.hover = hit; this.refreshNeighbours(); this.invalidate() }
    this.canvas.style.cursor = hit >= 0 ? 'pointer' : 'grab'
    if (hit < 0) { this.hideTooltip(); return }
    const node = this.nodes[hit]!
    const tip = this.tooltip
    if (tip.dataset.id !== node.id) {
      tip.dataset.id = node.id
      tip.replaceChildren()
      const kind = document.createElement('span')
      kind.className = 'xjm-tip-kind'
      kind.dataset.layer = String(node.layer)
      kind.textContent = node.kindLabel
      const text = document.createElement('span')
      text.className = 'xjm-tip-text'
      text.textContent = node.label.length > 90 ? node.label.slice(0, 90) + '…' : node.label
      tip.append(kind, text)
    }
    const tx = Math.min(this.width - 300, x + 14), ty = Math.max(8, y - 44)
    tip.style.transform = `translate(${Math.max(8, tx)}px, ${ty}px)`
    tip.hidden = false
  }

  private hideTooltip(): void { this.tooltip.hidden = true; delete this.tooltip.dataset.id }

  private invalidate(): void { this.dirty = true }

  // ---------- simulation ----------

  private step(dt: number): void {
    this.time += dt
    const dragging = this.press !== null && this.pointers.size > 0
    if (this.yawTarget !== null) {
      let d = this.yawTarget - this.yaw
      d = Math.atan2(Math.sin(d), Math.cos(d))
      this.yaw += d * Math.min(1, dt * 4.2)
      if (Math.abs(d) < 0.002) this.yawTarget = null
    } else if (!dragging) {
      if (Math.abs(this.yawVel) > 1e-4 || Math.abs(this.pitchVel) > 1e-4) {
        this.yaw += this.yawVel * dt
        this.pitch = Math.max(-1.25, Math.min(1.25, this.pitch + this.pitchVel * dt))
        const decay = Math.exp(-dt * 3.2)
        this.yawVel *= decay; this.pitchVel *= decay
      }
      if (this.autoRotate) this.yaw += dt * 0.085
    }
    if (this.opts.reducedMotion) return
    for (let i = 0; i < this.flash.length; i++) if (this.flash[i]! > 0) this.flash[i] = Math.max(0, this.flash[i]! - dt * 1.4)
    for (let i = 0; i < this.pulses.length; i++) {
      const p = this.pulses[i]!
      p.t += p.speed * dt
      if (p.t >= p.total) {
        if (p.node >= 0 && p.node < this.flash.length) this.flash[p.node] = 1
        this.pulses[i] = this.spawnPulse()
      }
    }
    if (this.edges.length) {
      const want = Math.min(28, Math.ceil(this.edges.length * 0.35))
      while (this.edgePulses.length < want) this.edgePulses.push({ edge: Math.floor(Math.random() * this.edges.length), t: -Math.random() * 1.6, speed: 0.35 + Math.random() * 0.3 })
      for (let i = this.edgePulses.length - 1; i >= 0; i--) {
        const ep = this.edgePulses[i]!
        ep.t += ep.speed * dt
        if (ep.t >= 1) {
          const e = this.edges[ep.edge]
          if (e) this.flash[e.t] = Math.max(this.flash[e.t]!, 0.8)
          this.edgePulses.splice(i, 1)
        }
      }
    }
  }

  // ---------- rendering ----------

  private nodeRadius(i: number): number {
    const nd = this.nodes[i]!
    return (1.6 + nd.weight * 2.0) * Math.max(0.6, this.nsp[i]! * this.fBase * this.zoom * 1.15)
  }

  private render(): void {
    const { ctx, m } = this
    const W = this.width, H = this.height
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
    ctx.clearRect(0, 0, W, H)

    const f = this.fBase * this.zoom
    const cx = W / 2 + this.panX, cy = H / 2 + this.panY + 8 * f
    const cyaw = Math.cos(this.yaw), syaw = Math.sin(this.yaw)
    const cp = Math.cos(this.pitch), spt = Math.sin(this.pitch)
    const D = 1500
    const project = (x: number, y: number, z: number, out: Float32Array, o: number) => {
      const x1 = x * cyaw - z * syaw
      const z1 = x * syaw + z * cyaw
      const y2 = y * cp - z1 * spt
      const z2 = y * spt + z1 * cp
      const k = D / (D + z2)
      out[o] = cx + x1 * f * k
      out[o + 1] = cy + y2 * f * k
      out[o + 2] = z2
      return k
    }
    const tmp = new Float32Array(3)

    // Orbiting point light in world space; brightness follows true 3D distance.
    const lt = this.time * 0.22
    const lx = Math.cos(lt) * 300, ly = -120 + Math.sin(lt * 0.7) * 110, lz = Math.sin(lt) * 300
    for (let i = 0; i < m.count; i++) {
      this.sp[i] = project(m.x[i]!, m.y[i]!, m.z[i]!, tmp, 0)
      this.sx[i] = tmp[0]!; this.sy[i] = tmp[1]!; this.sz[i] = tmp[2]!
      const dx = m.x[i]! - lx, dy = m.y[i]! - ly, dz = m.z[i]! - lz
      const ds = m.x[i]! ** 2 + m.y[i]! ** 2 + m.z[i]! ** 2
      this.light[i] = 0.42 + 0.95 / (1 + (dx * dx + dy * dy + dz * dz) / 52000) + 0.55 / (1 + ds / 26000)
    }
    const somaK = project(0, 0, 0, tmp, 0)
    this.somaScreen = { x: tmp[0]!, y: tmp[1]!, r: SOMA_RADIUS * f * somaK, z: tmp[2]! }
    const lightK = project(lx, ly, lz, tmp, 0)
    const lightScreen = { x: tmp[0]!, y: tmp[1]!, z: tmp[2]!, k: lightK }

    // Depth bands relative to the soma: 0..2 behind it, 3..5 in front.
    const somaZ = this.somaScreen.z
    const band = (z: number) => {
      const d = z - somaZ
      return d > 230 ? 0 : d > 110 ? 1 : d > 0 ? 2 : d > -110 ? 3 : d > -230 ? 4 : 5
    }
    const fogOf = (b: number) => [0.3, 0.48, 0.68, 0.84, 0.95, 1][b]!

    this.bucketLen.fill(0)
    for (let s = 0; s < m.segCount; s++) {
      const a = m.segA[s]!, b = m.segB[s]!
      const bd = band((this.sz[a]! + this.sz[b]!) * 0.5)
      const li = (this.light[a]! + this.light[b]!) * 0.5
      const lc = li < 0.78 ? 0 : li < 1.12 ? 1 : 2
      const key = ((m.group[b]! * BANDS + bd) * WIDTH_CLASSES + this.segWidth[s]!) * LIGHT_CLASSES + lc
      this.buckets[key]![this.bucketLen[key]!++] = s
    }

    // Records.
    const nCount = this.nodes.length
    for (let i = 0; i < nCount; i++) {
      this.nsp[i] = project(this.nx[i]!, this.ny[i]!, this.nz[i]!, tmp, 0)
      this.nsx[i] = tmp[0]!; this.nsy[i] = tmp[1]!; this.nsz[i] = tmp[2]!
      const behind = this.nsz[i]! > somaZ
      this.nOcc[i] = behind && Math.hypot(this.nsx[i]! - this.somaScreen.x, this.nsy[i]! - this.somaScreen.y) < this.somaScreen.r * 0.96 ? 1 : 0
    }
    for (let e = 0; e < this.edges.length; e++) {
      for (let j = 0; j < EDGE_SAMPLES; j++) {
        const o = (e * EDGE_SAMPLES + j) * 3
        project(this.edgePts[o]!, this.edgePts[o + 1]!, this.edgePts[o + 2]!, this.edgeScr, o)
      }
    }
    this.order = Array.from({ length: nCount }, (_, i) => i).sort((a, b) => this.nsz[b]! - this.nsz[a]!)

    const dim = this.dimmed ? 0.45 : 1
    const zoomW = Math.max(0.7, Math.min(2.4, f * 1.25))
    this.drawStrata(project, dim, true)
    this.drawDust(project, dim)

    let nodeCursor = 0
    for (let bd = 0; bd < BANDS; bd++) {
      this.drawFibreBand(bd, fogOf(bd) * dim, zoomW)
      this.drawEdgeBand(bd, band, fogOf(bd) * dim, zoomW)
      // Records belonging to this band, back to front.
      while (nodeCursor < this.order.length && band(this.nsz[this.order[nodeCursor]!]!) <= bd) {
        this.drawNode(this.order[nodeCursor]!, fogOf(bd), dim)
        nodeCursor++
      }
      if (bd === 2) { this.drawPulses(project, false, somaZ); this.drawSoma(lightScreen, dim) }
    }
    this.drawPulses(project, true, somaZ)
    this.drawStrata(project, dim, false)
    this.drawSelection()
  }

  private drawFibreBand(bd: number, fog: number, zoomW: number): void {
    const { ctx, m } = this
    const widths = [2.5, 1.35, 0.78, 0.42]
    const lightAlpha = [0.34, 0.56, 0.86]
    ctx.lineCap = 'round'
    for (let g = 0; g < GROUPS; g++) {
      const rgb = LAYER_RGB[g]!
      for (let w = 0; w < WIDTH_CLASSES; w++) {
        for (let lc = 0; lc < LIGHT_CLASSES; lc++) {
          const key = ((g * BANDS + bd) * WIDTH_CLASSES + w) * LIGHT_CLASSES + lc
          const len = this.bucketLen[key]!
          if (!len) continue
          const list = this.buckets[key]!
          ctx.beginPath()
          for (let i = 0; i < len; i++) {
            const s = list[i]!, a = m.segA[s]!, b = m.segB[s]!
            ctx.moveTo(this.sx[a]!, this.sy[a]!)
            ctx.lineTo(this.sx[b]!, this.sy[b]!)
          }
          const alpha = lightAlpha[lc]! * fog * (w === 3 ? 0.7 : 1)
          const lw = widths[w]! * zoomW * (0.82 + bd * 0.05)
          if (w <= 1) {
            ctx.globalCompositeOperation = 'lighter'
            ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(alpha * 0.07).toFixed(3)})`
            ctx.lineWidth = lw * 4.2
            ctx.stroke()
          }
          ctx.globalCompositeOperation = bd >= 3 ? 'lighter' : 'source-over'
          // Lit side is whiter; this sells the point light on thick trunks.
          const lift = lc === 2 ? 0.32 : lc === 1 ? 0.12 : 0
          const r = Math.round(rgb[0] + (255 - rgb[0]) * lift), gg = Math.round(rgb[1] + (255 - rgb[1]) * lift), b2 = Math.round(rgb[2] + (255 - rgb[2]) * lift)
          ctx.strokeStyle = `rgba(${r},${gg},${b2},${alpha.toFixed(3)})`
          ctx.lineWidth = lw
          ctx.stroke()
        }
      }
    }
    ctx.globalCompositeOperation = 'source-over'
  }

  private drawEdgeBand(bd: number, band: (z: number) => number, fog: number, zoomW: number): void {
    if (!this.edges.length) return
    const { ctx } = this
    const sel = this.hover >= 0 ? this.hover : this.selected
    ctx.lineCap = 'round'
    for (let pass = 0; pass < 3; pass++) {
      for (let layer = 0; layer < 3; layer++) {
        ctx.beginPath()
        let any = false
        for (let e = 0; e < this.edges.length; e++) {
          const edge = this.edges[e]!
          if (edge.layer !== layer) continue
          const active = sel >= 0 && (edge.s === sel || edge.t === sel)
          const hl = this.highlight !== null && (this.highlight.has(edge.s) || this.highlight.has(edge.t))
          const fading = this.fading >= 0 && (edge.s === this.fading || edge.t === this.fading)
          if ((fading ? 2 : active || hl ? 1 : 0) !== pass) continue
          let pen = false
          for (let j = 0; j < EDGE_SAMPLES - 1; j++) {
            const o = (e * EDGE_SAMPLES + j) * 3
            const z = (this.edgeScr[o + 2]! + this.edgeScr[o + 5]!) * 0.5
            if (band(z) !== bd) { pen = false; continue }
            if (!pen) { ctx.moveTo(this.edgeScr[o]!, this.edgeScr[o + 1]!); pen = true }
            ctx.lineTo(this.edgeScr[o + 3]!, this.edgeScr[o + 4]!)
            any = true
          }
        }
        if (!any) continue
        const rgb = LAYER_RGB[layer]!
        const quiet = sel >= 0 || this.highlight !== null
        const fade = Math.max(0, Math.min(1, (this.fadeUntil - performance.now()) / 300))
        const alpha = pass === 2 ? 0.9 * fade : pass === 1 ? 0.9 : quiet ? 0.1 : 0.27
        ctx.globalCompositeOperation = 'lighter'
        ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(alpha * fog * 0.35).toFixed(3)})`
        ctx.lineWidth = (pass === 1 ? 5 : 3) * zoomW
        ctx.stroke()
        ctx.strokeStyle = `rgba(${Math.min(255, rgb[0] + 40)},${Math.min(255, rgb[1] + 30)},${Math.min(255, rgb[2] + 30)},${(alpha * fog).toFixed(3)})`
        ctx.lineWidth = (pass === 1 ? 1.4 : 0.8) * zoomW
        ctx.stroke()
      }
    }
    ctx.globalCompositeOperation = 'source-over'
  }

  private drawNode(i: number, fog: number, dim: number): void {
    const { ctx } = this
    const nd = this.nodes[i]!
    const x = this.nsx[i]!, y = this.nsy[i]!
    let alpha = fog * (this.nOcc[i] ? 0 : 1)
    if (alpha <= 0) return
    const focusSet = this.highlight
    const isSel = i === this.selected, isHover = i === this.hover
    const focus = this.hover >= 0 ? this.hover : this.selected
    const related = i === focus || this.neighbours.has(i)
    if (focusSet && !focusSet.has(i)) alpha *= 0.22
    else if (focus >= 0 && !related) alpha *= 0.3
    if (i === this.fading) alpha *= Math.max(0, Math.min(1, (this.fadeUntil - performance.now()) / 300))
    alpha *= dim
    const r = this.nodeRadius(i) * (isHover || isSel ? 1.35 : 1)
    const fl = this.flash[i]!
    ctx.globalCompositeOperation = 'lighter'
    ctx.globalAlpha = Math.min(1, alpha * (0.38 + fl * 0.6 + (focusSet?.has(i) ? 0.4 : 0)))
    const halo = r * (3.2 + fl * 3)
    ctx.drawImage(this.sprites[nd.layer]!, x - halo, y - halo, halo * 2, halo * 2)
    ctx.globalAlpha = Math.min(1, alpha)
    ctx.globalCompositeOperation = 'source-over'
    const rgb = LAYER_RGB[nd.layer]!
    ctx.fillStyle = `rgb(${Math.round(rgb[0] * 0.35 + 165)},${Math.round(rgb[1] * 0.35 + 165)},${Math.round(rgb[2] * 0.35 + 165)})`
    if (nd.derived) {
      // Derived/read-only records: a diamond outline, never the same glyph as raw text.
      ctx.beginPath()
      ctx.moveTo(x, y - r * 1.25); ctx.lineTo(x + r * 1.25, y); ctx.lineTo(x, y + r * 1.25); ctx.lineTo(x - r * 1.25, y); ctx.closePath()
      ctx.strokeStyle = ctx.fillStyle
      ctx.lineWidth = Math.max(1, r * 0.42)
      ctx.stroke()
    } else {
      ctx.beginPath()
      ctx.arc(x, y, r, 0, TAU)
      ctx.fill()
    }
    ctx.globalAlpha = 1
  }

  private drawSoma(light: { x: number; y: number }, dim: number): void {
    const { ctx } = this
    const { x, y, r } = this.somaScreen
    // Bloom behind the cell body.
    ctx.globalCompositeOperation = 'lighter'
    const bloom = ctx.createRadialGradient(x, y, r * 0.6, x, y, r * 5.2)
    bloom.addColorStop(0, `rgba(170,196,255,${0.26 * dim})`)
    bloom.addColorStop(0.35, `rgba(120,150,230,${0.08 * dim})`)
    bloom.addColorStop(1, 'rgba(90,110,200,0)')
    ctx.fillStyle = bloom
    ctx.fillRect(x - r * 5.2, y - r * 5.2, r * 10.4, r * 10.4)
    ctx.globalCompositeOperation = 'source-over'
    // Opaque membrane: occludes everything painted behind it.
    const dx = light.x - x, dy = light.y - y, dl = Math.hypot(dx, dy) || 1
    const hx = x + (dx / dl) * r * 0.42, hy = y + (dy / dl) * r * 0.42
    const body = ctx.createRadialGradient(hx, hy, r * 0.05, x, y, r)
    body.addColorStop(0, '#f3f6ff')
    body.addColorStop(0.32, '#a9bce6')
    body.addColorStop(0.72, '#46557c')
    body.addColorStop(0.94, '#1a2032')
    body.addColorStop(1, '#2a3555')
    ctx.fillStyle = body
    ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill()
    // Nucleus and nucleolus, offset away from the light.
    const nxp = x - (dx / dl) * r * 0.12, nyp = y - (dy / dl) * r * 0.12
    const nuc = ctx.createRadialGradient(nxp, nyp, 0, nxp, nyp, r * 0.5)
    nuc.addColorStop(0, 'rgba(40,52,88,0.55)')
    nuc.addColorStop(0.8, 'rgba(60,76,120,0.35)')
    nuc.addColorStop(1, 'rgba(60,76,120,0)')
    ctx.fillStyle = nuc
    ctx.beginPath(); ctx.arc(nxp, nyp, r * 0.5, 0, TAU); ctx.fill()
    ctx.fillStyle = 'rgba(225,235,255,0.85)'
    ctx.beginPath(); ctx.arc(nxp + r * 0.1, nyp - r * 0.06, Math.max(1.2, r * 0.1), 0, TAU); ctx.fill()
    // Rim light.
    ctx.strokeStyle = 'rgba(190,210,255,0.55)'
    ctx.lineWidth = Math.max(1, r * 0.05)
    ctx.beginPath(); ctx.arc(x, y, r * 0.98, 0, TAU); ctx.stroke()
  }

  private drawPulses(project: (x: number, y: number, z: number, out: Float32Array, o: number) => number, front: boolean, somaZ: number): void {
    if (this.opts.reducedMotion) return
    const { ctx, m } = this
    const tmp = new Float32Array(3)
    ctx.globalCompositeOperation = 'lighter'
    const at = (p: Pulse, t: number): [number, number, number] | null => {
      if (t < 0) return null
      const cum = p.cum
      let lo = 0, hi = cum.length - 1
      if (t <= cum[0]!) {
        const v = p.path[0]!, k = t / cum[0]!
        return [m.x[v]! * k, m.y[v]! * k, m.z[v]! * k]
      }
      while (lo < hi - 1) { const mid = (lo + hi) >> 1; if (cum[mid]! < t) lo = mid; else hi = mid }
      const a = p.path[lo]!, b = p.path[hi]!
      const k = (t - cum[lo]!) / Math.max(1e-3, cum[hi]! - cum[lo]!)
      return [m.x[a]! + (m.x[b]! - m.x[a]!) * k, m.y[a]! + (m.y[b]! - m.y[a]!) * k, m.z[a]! + (m.z[b]! - m.z[a]!) * k]
    }
    for (const p of this.pulses) {
      const head = at(p, p.t)
      if (!head) continue
      const k = project(head[0], head[1], head[2], tmp, 0)
      if ((tmp[2]! <= somaZ) !== front) continue
      const hx = tmp[0]!, hy = tmp[1]!
      const fade = Math.min(1, p.t / 60) * Math.min(1, (p.total - p.t) / 40 + 0.3)
      const rgb = LAYER_RGB[p.group]!
      ctx.beginPath()
      ctx.moveTo(hx, hy)
      for (let s = 1; s <= 6; s++) {
        const q = at(p, p.t - s * 9)
        if (!q) break
        project(q[0], q[1], q[2], tmp, 0)
        ctx.lineTo(tmp[0]!, tmp[1]!)
      }
      ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(0.55 * fade).toFixed(3)})`
      ctx.lineWidth = 1.6 * k * Math.max(0.8, this.fBase * this.zoom)
      ctx.stroke()
      const s = 9 * k * Math.max(0.7, this.fBase * this.zoom)
      ctx.globalAlpha = 0.9 * fade
      ctx.drawImage(this.whiteSprite, hx - s, hy - s, s * 2, s * 2)
      ctx.globalAlpha = 1
    }
    for (const ep of this.edgePulses) {
      if (ep.t < 0) continue
      const pos = ep.t * (EDGE_SAMPLES - 1), j = Math.min(EDGE_SAMPLES - 2, Math.floor(pos)), u = pos - j
      const o = (ep.edge * EDGE_SAMPLES + j) * 3
      const x = this.edgeScr[o]! + (this.edgeScr[o + 3]! - this.edgeScr[o]!) * u
      const y = this.edgeScr[o + 1]! + (this.edgeScr[o + 4]! - this.edgeScr[o + 1]!) * u
      const z = this.edgeScr[o + 2]!
      if ((z <= somaZ) !== front) continue
      const edge = this.edges[ep.edge]!
      const s = 11 * Math.max(0.7, this.fBase * this.zoom)
      ctx.globalAlpha = 0.85
      ctx.drawImage(this.sprites[edge.layer]!, x - s, y - s, s * 2, s * 2)
      ctx.globalAlpha = 1
    }
    ctx.globalCompositeOperation = 'source-over'
  }

  private drawDust(project: (x: number, y: number, z: number, out: Float32Array, o: number) => number, dim: number): void {
    const { ctx } = this
    const tmp = new Float32Array(3)
    ctx.globalCompositeOperation = 'lighter'
    for (let li = 0; li < 3; li++) {
      const d = this.m.dust[li]!
      const rgb = LAYER_RGB[li]!
      ctx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`
      for (let i = 0; i < d.length; i += 4) {
        const k = project(d[i]!, d[i + 1]!, d[i + 2]!, tmp, 0)
        const tw = this.opts.reducedMotion ? 0.7 : 0.55 + 0.45 * Math.sin(this.time * 0.9 + d[i + 3]!)
        const depth = Math.max(0.15, Math.min(1, 1 - (tmp[2]! - this.somaScreen.z) / 700))
        ctx.globalAlpha = 0.32 * tw * depth * dim
        const s = Math.max(0.8, 1.5 * k * this.fBase * this.zoom)
        ctx.fillRect(tmp[0]! - s / 2, tmp[1]! - s / 2, s, s)
      }
    }
    ctx.globalAlpha = 1
    ctx.globalCompositeOperation = 'source-over'
  }

  /** Stratum boundary rings: back halves drawn first, front halves last, plus captions. */
  private drawStrata(project: (x: number, y: number, z: number, out: Float32Array, o: number) => number, dim: number, back: boolean): void {
    const { ctx } = this
    const tmp = new Float32Array(3)
    const steps = 120
    const somaZ = this.somaScreen.z
    const captions: Array<{ label: string; li: number; x: number; y: number; w: number; anchorX: number; anchorY: number }> = []
    for (let li = 0; li < 3; li++) {
      const st = STRATA[li]!
      const rgb = LAYER_RGB[li]!
      for (const [ratio, dash, alpha] of [[1.08, [], 0.3], [0.62, [2, 7], 0.17]] as Array<[number, number[], number]>) {
        ctx.beginPath()
        let pen = false
        for (let i = 0; i <= steps; i++) {
          const a = (i / steps) * TAU
          project(Math.cos(a) * st.radius * ratio, st.y, Math.sin(a) * st.radius * ratio, tmp, 0)
          const isBack = tmp[2]! > somaZ
          if (isBack !== back) { pen = false; continue }
          if (!pen) { ctx.moveTo(tmp[0]!, tmp[1]!); pen = true } else ctx.lineTo(tmp[0]!, tmp[1]!)
        }
        ctx.setLineDash(dash)
        ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(alpha * (back ? 0.55 : 1) * dim).toFixed(3)})`
        ctx.lineWidth = 1
        ctx.stroke()
      }
      ctx.setLineDash([])
      // Tick marks on the outer ring every 15 degrees.
      ctx.beginPath()
      for (let i = 0; i < 24; i++) {
        const a = (i / 24) * TAU
        const R1 = st.radius * 1.08, R2 = st.radius * (i % 6 === 0 ? 1.15 : 1.11)
        project(Math.cos(a) * R1, st.y, Math.sin(a) * R1, tmp, 0)
        if ((tmp[2]! > somaZ) !== back) continue
        const x1 = tmp[0]!, y1 = tmp[1]!
        project(Math.cos(a) * R2, st.y, Math.sin(a) * R2, tmp, 0)
        ctx.moveTo(x1, y1); ctx.lineTo(tmp[0]!, tmp[1]!)
      }
      ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(0.4 * (back ? 0.5 : 1) * dim).toFixed(3)})`
      ctx.stroke()
      if (!back) {
        // Caption anchored at the ring's right-most projected point.
        let bx = -Infinity, by = 0
        for (let i = 0; i < 48; i++) {
          const a = (i / 48) * TAU
          project(Math.cos(a) * st.radius * 1.15, st.y, Math.sin(a) * st.radius * 1.15, tmp, 0)
          if (tmp[0]! > bx) { bx = tmp[0]!; by = tmp[1]! }
        }
        const label = this.opts.layerCaptions[li]!
        ctx.font = '600 14px ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
        const w = ctx.measureText(label).width
        const lx = Math.max(12, Math.min(this.width - w - 18, bx + 12))
        captions.push({ label, li, x: lx, y: by + 5, w, anchorX: bx, anchorY: by })
      }
    }
    if (!back) {
      // Resolve captions in screen space after all three strata project. Near edge-on / tilted
      // views their natural anchors can coincide; labels must stay separate and in the viewport.
      const gap = 32
      const minY = Math.min(this.height * 0.35, this.width < 450 ? 174 : 116)
      const maxY = Math.max(minY + gap * 2, this.height - 104)
      captions.sort((a, b) => a.y - b.y)
      for (let i = 0; i < captions.length; i++) {
        captions[i]!.y = Math.max(minY, Math.min(maxY, captions[i]!.y), i ? captions[i - 1]!.y + gap : minY)
      }
      for (let i = captions.length - 1; i >= 0; i--) {
        captions[i]!.y = Math.min(captions[i]!.y, i < captions.length - 1 ? captions[i + 1]!.y - gap : maxY)
      }
      this.captionBoxes = []
      const textDim = Math.max(dim, 0.78)
      for (const c of captions) {
        const rgb = LAYER_RGB[c.li]!
        if (Math.abs(c.y - c.anchorY) > 20) {
          ctx.beginPath(); ctx.moveTo(c.anchorX, c.anchorY); ctx.lineTo(c.x - 10, c.y - 4)
          ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.32)`; ctx.lineWidth = 1; ctx.stroke()
        }
        ctx.fillStyle = `rgba(8,10,16,${0.80 * textDim})`
        ctx.beginPath(); ctx.roundRect(c.x - 8, c.y - 16, c.w + 16, 23, 6); ctx.fill()
        ctx.font = '600 14px ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
        ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${0.97 * textDim})`
        ctx.fillText(c.label, c.x, c.y)
        this.captionBoxes.push({ x: c.x - 8, y: c.y - 16, width: c.w + 16, height: 23 })
      }
    }
  }

  private drawSelection(): void {
    const { ctx } = this
    const mark = (i: number, strong: boolean) => {
      if (i < 0 || i >= this.nodes.length || this.nOcc[i]) return
      const x = this.nsx[i]!, y = this.nsy[i]!, r = this.nodeRadius(i) * 1.35 + 7
      const rgb = LAYER_RGB[this.nodes[i]!.layer]!
      ctx.strokeStyle = `rgba(${Math.min(255, rgb[0] + 60)},${Math.min(255, rgb[1] + 50)},${Math.min(255, rgb[2] + 40)},${strong ? 0.95 : 0.6})`
      ctx.lineWidth = strong ? 1.5 : 1
      ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.stroke()
      if (strong) {
        ctx.beginPath()
        for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]] as const) {
          ctx.moveTo(x + dx * (r + 3), y + dy * (r + 3)); ctx.lineTo(x + dx * (r + 9), y + dy * (r + 9))
        }
        ctx.stroke()
      }
    }
    if (this.highlight) for (const i of this.highlight) if (i !== this.selected) mark(i, false)
    mark(this.selected, true)
  }
}
