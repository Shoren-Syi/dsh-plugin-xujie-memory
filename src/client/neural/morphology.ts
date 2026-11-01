/**
 * Procedural 3D morphology of one pyramidal neuron spanning three memory strata.
 *
 * The fibres are structure only: they never represent a memory record. Real
 * records are placed later as synaptic boutons on vertices of the stratum that
 * matches their layer, and real relations are drawn as separate connections.
 */

export type LayerIndex = 0 | 1 | 2
/** 0 semantic (apical tuft), 1 episodic (equatorial), 2 working (basal), 3 neutral trunk. */
export type FibreGroup = LayerIndex | 3

export interface Stratum { y: number; radius: number; half: number }

/** World-space strata. Screen-up is negative y. */
export const STRATA: readonly Stratum[] = [
  { y: -262, radius: 340, half: 48 },
  { y: 0, radius: 390, half: 44 },
  { y: 258, radius: 330, half: 48 },
]

export const SOMA_RADIUS = 40

export interface Morphology {
  count: number
  x: Float32Array
  y: Float32Array
  z: Float32Array
  /** Fibre radius at the vertex in world units. */
  r: Float32Array
  parent: Int32Array
  group: Uint8Array
  /** 0 dendrite, 1 spine, 2 axon. */
  kind: Uint8Array
  segA: Int32Array
  segB: Int32Array
  segCount: number
  /** Vertex indices suitable for synaptic boutons, per stratum. */
  sites: [Int32Array, Int32Array, Int32Array]
  /** Diffuse extracellular particles per stratum: xyz + phase. */
  dust: [Float32Array, Float32Array, Float32Array]
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

interface V { x: number; y: number; z: number }
const norm = (v: V): V => { const l = Math.hypot(v.x, v.y, v.z) || 1; return { x: v.x / l, y: v.y / l, z: v.z / l } }
const cross = (a: V, b: V): V => ({ x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x })

export function buildMorphology(seed = 0x7a11e): Morphology {
  const rnd = mulberry32(seed)
  const gauss = () => (rnd() + rnd() + rnd() - 1.5) / 1.5
  const cap = 40000
  const x = new Float32Array(cap), y = new Float32Array(cap), z = new Float32Array(cap)
  const r = new Float32Array(cap), parent = new Int32Array(cap)
  const group = new Uint8Array(cap), kind = new Uint8Array(cap)
  const segA = new Int32Array(cap), segB = new Int32Array(cap)
  const siteLists: number[][] = [[], [], []]
  const branchDepth = new Uint8Array(cap)
  let n = 0, s = 0

  const add = (p: V, rad: number, par: number, g: FibreGroup, k: number, depth: number): number => {
    if (n >= cap) return -1
    x[n] = p.x; y[n] = p.y; z[n] = p.z; r[n] = rad; parent[n] = par; kind[n] = k; branchDepth[n] = depth
    // Fibres close to the soma read as one neutral trunk system.
    group[n] = Math.hypot(p.x, p.y, p.z) < SOMA_RADIUS * 2.1 && k !== 1 ? 3 : g
    if (par >= 0 && s < cap) { segA[s] = par; segB[s] = n; s++ }
    return n++
  }

  const perpendicular = (d: V): V => {
    const helper = Math.abs(d.y) < 0.9 ? { x: 0, y: 1, z: 0 } : { x: 1, y: 0, z: 0 }
    const a = norm(cross(d, helper)), b = cross(d, a)
    const t = rnd() * Math.PI * 2
    return norm({ x: a.x * Math.cos(t) + b.x * Math.sin(t), y: a.y * Math.cos(t) + b.y * Math.sin(t), z: a.z * Math.cos(t) + b.z * Math.sin(t) })
  }

  interface Branch { from: number; pos: V; dir: V; len: number; rad: number; depth: number; g: FibreGroup; target: Stratum | null; maxDepth: number; jitter: number; k: number }
  const stack: Branch[] = []

  const grow = (b: Branch) => {
    let pos = b.pos, dir = b.dir, rad = b.rad, prev = b.from, travelled = 0
    let sinceBranch = 0
    // Branches born inside their stratum stay confined to it.
    const confined = b.target !== null && Math.abs(b.pos.y - b.target.y) < b.target.half * 1.5
    while (travelled < b.len && rad > 0.16) {
      const step = (b.depth === 0 ? 10 : 8) + rnd() * 4
      dir = norm({ x: dir.x + gauss() * b.jitter, y: dir.y + gauss() * b.jitter, z: dir.z + gauss() * b.jitter })
      if (b.target) {
        const t = b.target
        const dy = (t.y - pos.y) / (t.half * (confined ? 3 : 4.5))
        dir = norm({ x: dir.x, y: dir.y + Math.max(-0.5, Math.min(0.6, dy)) * (confined ? 1.5 : 0.32), z: dir.z })
        const rr = Math.hypot(pos.x, pos.z) || 1
        const inside = Math.abs(pos.y - t.y) < t.half
        if (inside) {
          // Inside the stratum fibres flatten out and spread to fill its disc.
          dir = norm({ x: dir.x + (rr < t.radius * 0.72 ? (pos.x / rr) * 0.2 : 0), y: dir.y * 0.55, z: dir.z + (rr < t.radius * 0.72 ? (pos.z / rr) * 0.2 : 0) })
        }
        if (rr > t.radius * 0.9) {
          const pull = 0.5 * (rr / t.radius - 0.9) * 4
          dir = norm({ x: dir.x - (pos.x / rr) * pull, y: dir.y, z: dir.z - (pos.z / rr) * pull })
        }
      }
      pos = { x: pos.x + dir.x * step, y: pos.y + dir.y * step, z: pos.z + dir.z * step }
      travelled += step; sinceBranch += step
      rad *= b.depth === 0 ? 0.984 : 0.975
      const idx = add(pos, rad, prev, b.g, b.k, b.depth)
      if (idx < 0) return
      prev = idx
      const inStratum = b.target && Math.abs(pos.y - b.target.y) < b.target.half * 1.7
      if (inStratum && b.k === 0 && b.depth >= 1 && rnd() < 0.6) siteLists[b.g as number]?.push(idx)
      // Dendritic spines: short, very fine side processes that give the field its texture.
      if (b.k !== 2 && b.depth >= 1 && rnd() < 0.5) {
        const sp = perpendicular(dir), len = 3 + rnd() * 6
        add({ x: pos.x + sp.x * len, y: pos.y + sp.y * len, z: pos.z + sp.z * len }, 0.22, idx, b.g, 1, b.depth + 1)
      }
      const remaining = b.len - travelled
      if (b.depth < b.maxDepth && sinceBranch > 26 && remaining > 40 && rnd() < 0.17 + b.depth * 0.015) {
        sinceBranch = 0
        const p = perpendicular(dir), ang = 0.45 + rnd() * 0.55
        const cd = norm({ x: dir.x * Math.cos(ang) + p.x * Math.sin(ang), y: dir.y * Math.cos(ang) + p.y * Math.sin(ang), z: dir.z * Math.cos(ang) + p.z * Math.sin(ang) })
        // Bifurcation also deflects the parent slightly away.
        dir = norm({ x: dir.x - p.x * 0.25, y: dir.y - p.y * 0.25, z: dir.z - p.z * 0.25 })
        stack.push({ ...b, from: idx, pos, dir: cd, len: remaining * (0.62 + rnd() * 0.3), rad: Math.max(0.3, rad * 0.74), depth: b.depth + 1, jitter: Math.min(0.42, b.jitter + 0.04) })
      }
    }
    if (prev >= 0 && b.k === 0 && b.target) siteLists[b.g as number]?.push(prev)
  }

  const drain = () => { while (stack.length) grow(stack.pop()!) }
  const somaAt = (d: V): V => ({ x: d.x * SOMA_RADIUS * 0.92, y: d.y * SOMA_RADIUS * 0.92, z: d.z * SOMA_RADIUS * 0.92 })

  // Apical trunk: a thick, slightly wandering shaft to the semantic tuft.
  {
    let pos: V = somaAt({ x: 0, y: -1, z: 0 })
    let prev = add(pos, 6.2, -1, 3, 0, 0)
    let rad = 6.2
    const top = STRATA[0]!.y + 18
    let dir: V = { x: 0.04, y: -1, z: 0.02 }
    const obliqueAt = new Set([5, 9, 13, 16])
    for (let i = 0; pos.y > top; i++) {
      dir = norm({ x: dir.x + gauss() * 0.05 - pos.x * 0.002, y: -1, z: dir.z + gauss() * 0.05 - pos.z * 0.002 })
      pos = { x: pos.x + dir.x * 11, y: pos.y + dir.y * 11, z: pos.z + dir.z * 11 }
      rad = Math.max(3.2, rad * 0.975)
      prev = add(pos, rad, prev, 0, 0, 0)
      if (obliqueAt.has(i)) {
        const a = rnd() * Math.PI * 2
        stack.push({ from: prev, pos, dir: norm({ x: Math.cos(a), y: -0.55, z: Math.sin(a) }), len: 240 + rnd() * 80, rad: 1.7, depth: 1, g: 0, target: STRATA[0]!, maxDepth: 4, jitter: 0.2, k: 0 })
      }
    }
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2 + rnd() * 0.5
      stack.push({ from: prev, pos, dir: norm({ x: Math.cos(a), y: -0.18, z: Math.sin(a) }), len: 300 + rnd() * 90, rad: 2.6, depth: 1, g: 0, target: STRATA[0]!, maxDepth: 5, jitter: 0.17, k: 0 })
    }
    drain()
  }

  // Equatorial dendrites into the episodic stratum.
  for (let i = 0; i < 10; i++) {
    const a = (i / 10) * Math.PI * 2 + rnd() * 0.4
    const d = norm({ x: Math.cos(a), y: gauss() * 0.12, z: Math.sin(a) })
    const start = somaAt(d)
    const root = add(start, 4.2, -1, 3, 0, 0)
    stack.push({ from: root, pos: start, dir: d, len: 400 + rnd() * 70, rad: 3.6, depth: 0, g: 1, target: STRATA[1]!, maxDepth: 5, jitter: 0.15, k: 0 })
    drain()
  }

  // Basal dendrites descending into the working stratum.
  for (let i = 0; i < 7; i++) {
    const a = (i / 7) * Math.PI * 2 + rnd() * 0.5
    const d = norm({ x: Math.cos(a) * 0.8, y: 0.6, z: Math.sin(a) * 0.8 })
    const start = somaAt(d)
    const root = add(start, 3.8, -1, 3, 0, 0)
    stack.push({ from: root, pos: start, dir: d, len: 440 + rnd() * 70, rad: 3.2, depth: 0, g: 2, target: STRATA[2]!, maxDepth: 5, jitter: 0.15, k: 0 })
    drain()
  }

  // Axon: a fine straight process with collaterals into the working stratum.
  {
    let pos = somaAt(norm({ x: 0.18, y: 1, z: -0.12 }))
    let prev = add(pos, 1.9, -1, 3, 2, 0)
    let dir = norm({ x: 0.12, y: 1, z: -0.08 })
    for (let i = 0; i < 31; i++) {
      dir = norm({ x: dir.x + gauss() * 0.03, y: 1, z: dir.z + gauss() * 0.03 })
      pos = { x: pos.x + dir.x * 11, y: pos.y + dir.y * 11, z: pos.z + dir.z * 11 }
      prev = add(pos, 1.4, prev, 2, 2, 0)
      if (i === 17 || i === 21 || i === 25 || i === 30) {
        const a = rnd() * Math.PI * 2
        stack.push({ from: prev, pos, dir: norm({ x: Math.cos(a), y: 0.05, z: Math.sin(a) }), len: 200 + rnd() * 80, rad: 1.0, depth: 2, g: 2, target: STRATA[2]!, maxDepth: 4, jitter: 0.2, k: 0 })
      }
    }
    drain()
  }

  const sites = siteLists.map(list => Int32Array.from(new Set(list))) as [Int32Array, Int32Array, Int32Array]
  const dust = STRATA.map((st, li) => {
    const count = li === 1 ? 520 : 440
    const out = new Float32Array(count * 4)
    for (let i = 0; i < count; i++) {
      const rr = st.radius * Math.sqrt(rnd()) * 1.08, a = rnd() * Math.PI * 2
      out[i * 4] = Math.cos(a) * rr
      out[i * 4 + 1] = st.y + gauss() * st.half * 0.9
      out[i * 4 + 2] = Math.sin(a) * rr
      out[i * 4 + 3] = rnd() * Math.PI * 2
    }
    return out
  }) as [Float32Array, Float32Array, Float32Array]

  return { count: n, x, y, z, r, parent, group, kind, segA, segB, segCount: s, sites, dust }
}
