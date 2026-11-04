/** 三层立体记忆场。纤维是结构装饰，真实关联由快照提供。 */
export type LayerIndex = 0 | 1 | 2
export type FibreGroup = LayerIndex | 3
export interface Stratum { y: number; radius: number }
export const STRATA: readonly Stratum[] = [
  { y: -185, radius: 265 }, { y: 0, radius: 300 }, { y: 185, radius: 265 },
]
export const SOMA_RADIUS = 40

export interface Morphology {
  count: number
  x: Float32Array
  y: Float32Array
  z: Float32Array
  r: Float32Array
  parent: Int32Array
  group: Uint8Array
  kind: Uint8Array
  segA: Int32Array
  segB: Int32Array
  segCount: number
  sites: [Int32Array, Int32Array, Int32Array]
  dust: [Float32Array, Float32Array, Float32Array]
}

export function buildMorphology(seed = 0x7a11e): Morphology {
  let state = seed >>> 0
  const random = () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  const xs: number[] = [], ys: number[] = [], zs: number[] = [], radii: number[] = []
  const parents: number[] = [], groups: number[] = [], kinds: number[] = [], as: number[] = [], bs: number[] = []
  const sites: number[][] = [[], [], []]
  const add = (x: number, y: number, z: number, radius: number, parent: number, group: number) => {
    const i = xs.length
    xs.push(x); ys.push(y); zs.push(z); radii.push(radius); parents.push(parent); groups.push(group); kinds.push(0)
    if (parent >= 0) { as.push(parent); bs.push(i) }
    return i
  }
  // 层内从局部节点生长分支，只有少数主干跨层，避免所有纤维汇聚成亮团。
  const siteCount = 720
  for (let layer = 0; layer < STRATA.length; layer++) {
    const stratum = STRATA[layer]!, local: number[] = []
    for (let i = 0; i < siteCount; i++) {
      const angle = random() * Math.PI * 2, radius = Math.sqrt(random()) * stratum.radius
      const x = Math.cos(angle) * radius, y = stratum.y + (random() - 0.5) * 28, z = Math.sin(angle) * radius
      let parent = -1, nearest = 65 * 65
      if (i < 24 && layer > 0) {
        const previous = sites[layer - 1]!
        parent = previous[Math.floor(i * previous.length / 24)]!
      } else if (i % 3 !== 0) {
        for (const candidate of local) {
          const d = (xs[candidate]! - x) ** 2 + (ys[candidate]! - y) ** 2 + (zs[candidate]! - z) ** 2
          if (d < nearest) { nearest = d; parent = candidate }
        }
      }
      if (parent >= 0) {
        const bend = (random() - 0.5) * 18
        parent = add((xs[parent]! + x) / 2 + bend, (ys[parent]! + y) / 2 + bend * .3,
          (zs[parent]! + z) / 2 - bend, 0.28, parent, layer)
      }
      const point = add(x, y, z, 0.38, parent, layer)
      local.push(point)
      sites[layer]!.push(point)
    }
  }
  const dust = STRATA.map(stratum => {
    const points = new Float32Array(240 * 4)
    for (let i = 0; i < points.length; i += 4) {
      const angle = random() * Math.PI * 2, radius = Math.sqrt(random()) * stratum.radius
      points[i] = Math.cos(angle) * radius; points[i + 1] = stratum.y + (random() - 0.5) * 30
      points[i + 2] = Math.sin(angle) * radius; points[i + 3] = random() * Math.PI * 2
    }
    return points
  }) as [Float32Array, Float32Array, Float32Array]
  return { count: xs.length, x: Float32Array.from(xs), y: Float32Array.from(ys), z: Float32Array.from(zs),
    r: Float32Array.from(radii), parent: Int32Array.from(parents), group: Uint8Array.from(groups), kind: Uint8Array.from(kinds),
    segA: Int32Array.from(as), segB: Int32Array.from(bs), segCount: as.length,
    sites: sites.map(s => Int32Array.from(s)) as [Int32Array, Int32Array, Int32Array], dust }
}
