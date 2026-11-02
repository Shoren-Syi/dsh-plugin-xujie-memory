import type { Edge, Node, Snapshot } from './types.ts'
import type { FieldEdge, FieldNode } from './neural/engine.ts'
import type { LayerIndex } from './neural/morphology.ts'
import type { FailureKind } from './memory-adapter.ts'

export const LAYER_META = [
  { key: 'semantic', name: '语义记忆', en: 'SEMANTIC', hint: '稳定事实、状态与锚点' },
  { key: 'episodic', name: '情节记忆', en: 'EPISODIC', hint: '原话与阶段摘要' },
  { key: 'working', name: '工作记忆', en: 'WORKING', hint: '近期上下文与待澄清' },
] as const

export const KIND_LABEL: Record<string, string> = {
  trace: '原话', anchor: '锚点', recent: '近期', state: '状态', summary: '摘要', clarification: '待澄清',
}

export const STATUS_LABEL: Record<string, string> = { known: '已确认', stale: '可能过期', disputed: '存在分歧', active: '活跃' }

export function layerIndex(layer: string): LayerIndex {
  return layer === 'semantic' ? 0 : layer === 'working' ? 2 : 1
}

/** Raw-text kinds point at an editable trace; the rest are derived and read-only. */
export function isTraceBacked(node: Node): boolean {
  return node.editable === true && typeof node.trace_id === 'string' && node.trace_id !== ''
}

function weightOf(node: Node): number {
  if (node.kind === 'state') return 0.62
  if (node.kind === 'summary') return 0.58
  if (node.kind === 'clarification') return 0.42
  const importance = Math.max(1, Math.min(5, Number(node.importance ?? 1)))
  return Math.min(1, (importance - 1) / 4 * 0.7 + (node.anchor ? 0.3 : 0) + (node.kind === 'recent' ? 0.12 : 0))
}

export function toField(snapshot: Snapshot): { nodes: FieldNode[]; edges: FieldEdge[] } {
  const nodes = snapshot.nodes.map((n): FieldNode => ({
    id: n.id, layer: layerIndex(n.layer), derived: !isTraceBacked(n), weight: weightOf(n),
    label: n.content || '（空）', kindLabel: KIND_LABEL[n.kind] ?? n.kind,
  }))
  const edges = snapshot.edges.map((e: Edge): FieldEdge => ({ source: e.source, target: e.target }))
  return { nodes, edges }
}

export function layerCounts(snapshot: Snapshot | null): [number, number, number] {
  const c: [number, number, number] = [0, 0, 0]
  if (snapshot) for (const n of snapshot.nodes) c[layerIndex(n.layer)]++
  return c
}

export function formatTime(value: string | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id
}

export const FAILURE_TEXT: Record<FailureKind, { title: string; body: string }> = {
  owner: { title: '仅所有者可管理记忆', body: '记忆库只对当前 DSH 的所有者开放。共享会话与访客无法查看或修改。' },
  conflict: { title: '这条记忆已在别处更新', body: '版本不一致，保存或忘记已被拒绝。载入最新内容后再操作。' },
  not_found: { title: '记录不存在', body: '它可能已被编辑替换或已被忘记。' },
  scope: { title: '记忆范围不存在', body: '该范围可能已被移除，请重新选择。' },
  invalid: { title: '输入内容无效', body: '请检查内容长度和情绪强度后重试。' },
  emotion_limit: { title: '情绪数量已达上限', body: '最多设置 20 种情绪，请先删除不需要的项目。' },
  unavailable: { title: '记忆内核暂不可用', body: '内核可能正在启动或重启。稍后重试。' },
  unknown: { title: '操作未完成', body: '记忆库没有返回结果。稍后重试。' },
}

const SOURCE_LABEL: Record<string, string> = {
  user_statement: '用户陈述', assistant_statement: '助手陈述', assistant_reply: '助手回复',
  tool_result: '工具结果', system: '系统', import: '导入', manual: '手动录入', edit: '编辑',
}
export function sourceLabel(kind: string | null | undefined): string {
  if (!kind) return '—'
  return SOURCE_LABEL[kind] ?? kind
}
