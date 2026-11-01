import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { Catalog, Node, PersonCard, SearchHit, Snapshot, Trace } from './types.ts'
import { MemoryAdapter, asFailure, type MemoryFailure, type XujieMemoryRemote } from './memory-adapter.ts'
import { FAILURE_TEXT, KIND_LABEL, LAYER_META, STATUS_LABEL, formatTime, isTraceBacked, layerCounts, layerIndex, shortId, sourceLabel, toField } from './model.ts'
import { NeuralField, type NeuralFieldHandle } from './NeuralField.tsx'
import { IconAlert, IconArrow, IconBack, IconClose, IconEdit, IconForget, IconLock, IconMinus, IconPause, IconPlay, IconPlus, IconReset, IconSearch } from './icons.tsx'

export interface MemoryPanelProps { remote: XujieMemoryRemote }

type Target = { nodeId: string } | { traceId: string }
type Tab = 'overview' | 'persona'

const TAG_KIND: Record<string, string> = { preference: '偏好', nickname: '称呼', debt: '往来', identity: '身份', relation: '关系' }
const MAX_CONTENT = 2000

function usePrefersReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)'
  const [reduced, setReduced] = useState(() => typeof matchMedia === 'function' && matchMedia(query).matches)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia(query)
    const on = () => setReduced(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return reduced
}

function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim()
  if (!q) return <>{text}</>
  const i = text.toLowerCase().indexOf(q.toLowerCase())
  if (i < 0) return <>{text}</>
  return <>{text.slice(0, i)}<mark className="xjm-mark">{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>
}

function LayerChip({ layer }: { layer: string }) {
  const li = layerIndex(layer)
  return <span className="xjm-chip" data-layer={li}><i className="xjm-dot" aria-hidden="true" />{LAYER_META[li].name}</span>
}

function StatusBlock({ icon, title, body, action }: { icon?: ReactNode; title: string; body: string; action?: ReactNode }) {
  return <div className="xjm-status" role="status">
    {icon && <span className="xjm-status-icon">{icon}</span>}
    <p className="xjm-status-title">{title}</p>
    <p className="xjm-status-body">{body}</p>
    {action}
  </div>
}

export function MemoryPanel({ remote }: MemoryPanelProps) {
  const adapter = useMemo(() => new MemoryAdapter(remote), [remote])
  const reducedMotion = usePrefersReducedMotion()
  const fieldRef = useRef<NeuralFieldHandle>(null)
  const epoch = useRef(0)
  const catalogEpoch = useRef(0)

  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [scopeId, setScopeId] = useState<string | null>(null)
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState<MemoryFailure | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchFailure, setSearchFailure] = useState<MemoryFailure | null>(null)
  const [searchNonce, setSearchNonce] = useState(0)

  const [tab, setTab] = useState<Tab>('overview')
  const [layerFocus, setLayerFocus] = useState<number | null>(null)
  const [autoRotate, setAutoRotate] = useState(true)

  const [target, setTarget] = useState<Target | null>(null)
  const [trace, setTrace] = useState<Trace | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailFailure, setDetailFailure] = useState<MemoryFailure | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState<'save' | 'forget' | null>(null)
  const busyRef = useRef<typeof busy>(null)
  busyRef.current = busy
  const [actionFailure, setActionFailure] = useState<MemoryFailure | null>(null)
  const [confirmForget, setConfirmForget] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [detailNonce, setDetailNonce] = useState(0)

  // ---- catalog ----
  useEffect(() => {
    const my = ++catalogEpoch.current
    setLoading(true); setFailure(null)
    adapter.catalog().then(cat => {
      if (my !== catalogEpoch.current) return
      setCatalog(cat)
      setScopeId(prev => {
        if (prev && cat.scopes.some(s => s.scope_id === prev)) return prev
        const best = [...cat.scopes].sort((a, b) => b.traces - a.traces)[0]
        return best?.scope_id ?? null
      })
      if (!cat.scopes.length) { setSnapshot(null); setLoading(false) }
    }, err => { if (my === catalogEpoch.current) { setFailure(asFailure(err)); setLoading(false) } })
  }, [adapter, reloadKey])

  // quiet catalog refresh after mutations (keeps per-scope counts in the selector honest)
  const refreshCatalog = useCallback(() => {
    const my = catalogEpoch.current
    adapter.catalog().then(cat => { if (my === catalogEpoch.current) setCatalog(cat) }, () => { /* non-fatal */ })
  }, [adapter])

  // ---- snapshot ----
  const loadSnapshot = useCallback(async (scope: string, quiet = false) => {
    const my = ++epoch.current
    if (!quiet) { setLoading(true); setFailure(null) }
    try {
      const snap = await adapter.snapshot(scope)
      if (my !== epoch.current) return null
      setSnapshot(snap)
      setFailure(null)
      return snap
    } catch (err) {
      if (my === epoch.current) setFailure(asFailure(err))
      return null
    } finally {
      if (my === epoch.current) setLoading(false)
    }
  }, [adapter])

  useEffect(() => {
    if (!scopeId) return
    setTarget(null); setHits(null); setLayerFocus(null)
    void loadSnapshot(scopeId)
  }, [scopeId, loadSnapshot, reloadKey])

  // ---- search ----
  useEffect(() => {
    const q = query.trim()
    if (!scopeId || !q) { setHits(null); setSearching(false); setSearchFailure(null); return }
    let alive = true
    setSearching(true)
    const t = setTimeout(() => {
      adapter.search(scopeId, q).then(
        list => { if (alive) { setHits(list); setSearchFailure(null) } },
        err => { if (alive) { setHits(null); setSearchFailure(asFailure(err)) } },
      ).finally(() => { if (alive) setSearching(false) })
    }, 260)
    return () => { alive = false; clearTimeout(t) }
  }, [adapter, scopeId, query, searchNonce])

  // ---- derived ----
  const nodeById = useMemo(() => new Map((snapshot?.nodes ?? []).map(n => [n.id, n])), [snapshot])
  const field = useMemo(() => snapshot ? toField(snapshot) : { nodes: [], edges: [] }, [snapshot])
  const counts = useMemo(() => layerCounts(snapshot), [snapshot])
  const captions = useMemo(() => LAYER_META.map((m, i) => `${m.name}  ${m.en} · ${counts[i]}`) as unknown as readonly [string, string, string], [counts])
  const targetNode: Node | null = target && 'nodeId' in target ? nodeById.get(target.nodeId) ?? null : null
  const traceId: string | null = target ? ('traceId' in target ? target.traceId : (targetNode && isTraceBacked(targetNode) ? targetNode.trace_id! : null)) : null
  const selectedGraphId = target ? ('nodeId' in target ? target.nodeId : (nodeById.has(target.traceId) ? target.traceId : null)) : null
  const highlightIds = useMemo(() => {
    if (hits) return hits.map(h => h.trace_id)
    if (layerFocus !== null && snapshot) return snapshot.nodes.filter(n => layerIndex(n.layer) === layerFocus).map(n => n.id)
    return null
  }, [hits, layerFocus, snapshot])

  // ---- detail ----
  useEffect(() => {
    setEditing(false); setConfirmForget(false); setActionFailure(null); setDetailFailure(null)
    // Never let the previous record stay visible/editable while another one loads.
    setTrace(prev => (prev && prev.trace_id === traceId ? prev : null))
    if (!scopeId || !traceId) { setDetailLoading(false); return }
    let alive = true
    setDetailLoading(true)
    adapter.detail(scopeId, traceId).then(
      t => { if (alive && t.trace_id === traceId) { setTrace(t); setDraft(t.content) } },
      err => { if (alive) { setTrace(null); setDetailFailure(asFailure(err)) } },
    ).finally(() => { if (alive) setDetailLoading(false) })
    return () => { alive = false }
  }, [adapter, scopeId, traceId, detailNonce, target])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3600)
    return () => clearTimeout(t)
  }, [toast])

  // Every user-initiated selection goes through here: blocked while a save/forget is in flight
  // (so an async result cannot land on a different record), and it drops the previous record in the
  // same render so no frame shows the old trace's actions under the new node's header.
  const selectTarget = useCallback((next: Target | null) => {
    if (busyRef.current) return
    setTrace(null); setEditing(false); setConfirmForget(false); setActionFailure(null)
    setDetailLoading(next !== null)
    setTarget(next)
  }, [])

  const openTrace = useCallback((id: string) => {
    selectTarget(nodeById.has(id) ? { nodeId: id } : { traceId: id })
  }, [nodeById, selectTarget])

  const onFieldSelect = useCallback((id: string | null) => {
    if (id === null) { if (!editing) selectTarget(null); return }
    if (target && 'nodeId' in target && target.nodeId === id) return
    selectTarget({ nodeId: id })
  }, [editing, target, selectTarget])

  const save = async () => {
    if (!scopeId || !trace || trace.trace_id !== traceId || busyRef.current) return
    const content = draft.trim()
    if (!content || content.length > MAX_CONTENT) return
    setBusy('save'); setActionFailure(null)
    try {
      const result = await adapter.edit(scopeId, trace.trace_id, content, trace.content_hash)
      if (!result.changed) { setEditing(false); setToast('内容未变化，没有生成新版本'); return }
      setEditing(false)
      setTrace(result.trace)
      const pending = result.pending ?? 0
      setToast(pending > 0 ? `已保存为新版本，产生 ${pending} 条待澄清` : '已保存为新版本，旧版本已替换')
      const snap = await loadSnapshot(scopeId, true)
      refreshCatalog()
      const newId = result.trace.trace_id
      setTarget(snap?.nodes.some(n => n.id === newId) ? { nodeId: newId } : { traceId: newId })
      if (query.trim()) setSearchNonce(n => n + 1)
    } catch (err) {
      setActionFailure(asFailure(err))
    } finally { setBusy(null) }
  }

  const forget = async () => {
    if (!scopeId || !trace || trace.trace_id !== traceId || busyRef.current) return
    setBusy('forget'); setActionFailure(null)
    try {
      await adapter.forget(scopeId, trace.trace_id, trace.content_hash)
      setTarget(null); setTrace(null); setConfirmForget(false)
      setToast('已忘记这条记忆，相关摘要与人设已重新推导')
      await loadSnapshot(scopeId, true)
      refreshCatalog()
      if (query.trim()) setSearchNonce(n => n + 1)
    } catch (err) {
      setActionFailure(asFailure(err))
    } finally { setBusy(null) }
  }

  const reloadLatest = () => { setActionFailure(null); setDetailNonce(n => n + 1); if (scopeId) void loadSnapshot(scopeId, true) }

  // ---- render helpers ----
  const scopes = catalog?.scopes ?? []
  const stats = snapshot?.stats
  const pageFailure = failure
  const isEmpty = !loading && !pageFailure && (scopes.length === 0 || (snapshot !== null && snapshot.nodes.length === 0))
  const showDetail = target !== null

  const relationsOf = (node: Node): Array<{ node: Node; kind: string }> => {
    if (!snapshot) return []
    const out: Array<{ node: Node; kind: string }> = []
    const seen = new Set<string>()
    for (const e of snapshot.edges) {
      const other = e.source === node.id ? e.target : e.target === node.id ? e.source : null
      if (!other || seen.has(other)) continue
      const n = nodeById.get(other)
      if (n) { seen.add(other); out.push({ node: n, kind: e.kind }) }
    }
    return out
  }

  const stageOverlay = (() => {
    if (pageFailure) {
      const t = FAILURE_TEXT[pageFailure.kind]
      return <StatusBlock icon={pageFailure.kind === 'owner' ? <IconLock /> : <IconAlert />} title={t.title} body={t.body}
        action={pageFailure.kind !== 'owner' ? <button type="button" className="xjm-btn xjm-btn-ghost-dark" onClick={() => setReloadKey(k => k + 1)}>重试</button> : undefined} />
    }
    if (loading && !snapshot) return <div className="xjm-status" role="status"><span className="xjm-spinner" aria-hidden="true" /><p className="xjm-status-title">正在读取记忆</p></div>
    if (isEmpty) return <StatusBlock title="还没有记忆" body={scopes.length ? '这个范围里还没有原话。与智能体对话后，原话、状态与摘要会在这里生长出来。' : '还没有任何记忆范围。与智能体对话后，这里会出现第一批记忆。'} />
    return null
  })()

  const renderOverview = () => <>
    <section className="xjm-section" aria-label="三层记忆">
      <h2 className="xjm-h2">三层结构</h2>
      <div className="xjm-layers">
        {LAYER_META.map((m, i) => <button key={m.key} type="button" className="xjm-layer" data-layer={i} aria-pressed={layerFocus === i}
          disabled={!snapshot || counts[i] === 0} onClick={() => setLayerFocus(layerFocus === i ? null : i)}>
          <i className="xjm-dot" aria-hidden="true" />
          <span className="xjm-layer-text"><span className="xjm-layer-name">{m.name}</span><span className="xjm-layer-hint">{m.hint}</span></span>
          <span className="xjm-layer-count">{counts[i]}</span>
        </button>)}
      </div>
    </section>
    {stats && <section className="xjm-section" aria-label="统计">
      <dl className="xjm-stats">
        <div><dt>原话</dt><dd>{stats.traces}</dd></div>
        <div><dt>状态</dt><dd>{stats.states}</dd></div>
        <div><dt>锚点</dt><dd>{stats.anchors}</dd></div>
        <div><dt>摘要</dt><dd>{stats.summaries}</dd></div>
        <div><dt>待澄清</dt><dd>{stats.clarifications_open}</dd></div>
        <div><dt>已忘记</dt><dd>{stats.traces_erased}</dd></div>
      </dl>
      {snapshot?.truncated && <p className="xjm-note">图中显示最近 {snapshot.limit} 条原话，共 {stats.traces} 条。更早的记录可通过搜索查看与编辑。</p>}
    </section>}
    {snapshot && snapshot.nodes.some(n => n.kind === 'trace') && <section className="xjm-section" aria-label="最近原话">
      <h2 className="xjm-h2">最近原话</h2>
      <ul className="xjm-list">
        {snapshot.nodes.filter(n => n.kind === 'trace').slice(0, 6).map(n => <li key={n.id}>
          <button type="button" className="xjm-row" onClick={() => selectTarget({ nodeId: n.id })}>
            <span className="xjm-row-text">{n.content}</span>
            <span className="xjm-row-meta">{formatTime(n.recorded_at)}{n.anchor ? ' · 锚点' : ''}</span>
          </button>
        </li>)}
      </ul>
    </section>}
  </>

  const renderPersona = (cards: PersonCard[]) => cards.length === 0
    ? <p className="xjm-empty-line">还没有人设卡。对话中出现称呼、偏好等信息后会自动推导。</p>
    : <>
      <p className="xjm-note">人设卡由原话推导，只读。编辑或忘记原话后会重新推导。</p>
      {cards.map(card => {
        const groups = new Map<string, typeof card.tags>()
        for (const t of card.tags) groups.set(t.kind, [...(groups.get(t.kind) ?? []), t])
        return <article key={card.account_id} className="xjm-card">
          <header className="xjm-card-head">
            <span className="xjm-avatar" aria-hidden="true">{(card.display_name || card.account_id).slice(0, 1)}</span>
            <span className="xjm-card-name">{card.display_name}</span>
            <span className="xjm-card-status">{STATUS_LABEL[card.status] ?? card.status}</span>
          </header>
          {card.tags.length === 0 ? <p className="xjm-empty-line">暂无标签</p> : [...groups].map(([kind, tags]) => <div key={kind} className="xjm-tag-group">
            <span className="xjm-tag-kind">{TAG_KIND[kind] ?? kind}</span>
            <span className="xjm-tags">{tags.map(t => <span key={t.tag_id} className="xjm-tag" title={`来源 ${t.source} · 置信 ${Math.round(t.confidence * 100)}%`}>{t.value}</span>)}</span>
          </div>)}
        </article>
      })}
    </>

  const renderHits = () => {
    if (searchFailure) { const t = FAILURE_TEXT[searchFailure.kind]; return <p className="xjm-inline-error" role="alert">{t.title}。{t.body}</p> }
    if (!hits) return <p className="xjm-empty-line">正在检索…</p>
    if (!hits.length) return <p className="xjm-empty-line">没有找到与「{query.trim()}」相关的原话。</p>
    const inGraph = hits.filter(h => nodeById.has(h.trace_id)).length
    return <>
      <p className="xjm-result-count">{hits.length} 条结果{inGraph < hits.length ? ` · ${hits.length - inGraph} 条在图外` : ''}</p>
      <ul className="xjm-list">
        {hits.map(h => <li key={h.trace_id}>
          <button type="button" className="xjm-row" aria-current={traceId === h.trace_id ? 'true' : undefined} onClick={() => openTrace(h.trace_id)}>
            <span className="xjm-row-text"><Highlight text={h.content} query={query} /></span>
            <span className="xjm-row-meta">
              {formatTime(h.recorded_at)}
              {h.anchor && <span className="xjm-badge">锚点</span>}
              {!nodeById.has(h.trace_id) && <span className="xjm-badge xjm-badge-muted">图外</span>}
            </span>
          </button>
        </li>)}
      </ul>
    </>
  }

  const renderDetail = () => {
    const node = targetNode
    const derived = node !== null && !isTraceBacked(node)
    const layer = node?.layer ?? 'episodic'
    const kind = node ? (KIND_LABEL[node.kind] ?? node.kind) : '原话'
    const head = <div className="xjm-detail-head">
      <button type="button" className="xjm-icon-btn" aria-label="返回" disabled={busy !== null} onClick={() => selectTarget(null)}><IconBack /></button>
      <LayerChip layer={layer} />
      <span className="xjm-kind">{kind}</span>
      <span className="xjm-spacer" />
      {derived ? <span className="xjm-badge xjm-badge-muted">只读 · 派生</span> : !node && traceId ? <span className="xjm-badge xjm-badge-muted">图外</span> : null}
    </div>

    if (derived && node) {
      const evidence = node.evidence ?? []
      return <div className="xjm-detail">
        {head}
        <p className="xjm-content">{node.content}</p>
        <dl className="xjm-meta">
          {node.status && <div><dt>状态</dt><dd>{STATUS_LABEL[node.status] ?? node.status}</dd></div>}
          {node.recorded_at && <div><dt>生成时间</dt><dd>{formatTime(node.recorded_at)}</dd></div>}
          <div><dt>标识</dt><dd className="xjm-mono" title={node.id}>{shortId(node.id)}</dd></div>
        </dl>
        <p className="xjm-note">派生记忆不能直接编辑。要更正它，请编辑或忘记下面的证据原话。</p>
        {node.kind === 'clarification'
          ? <p className="xjm-empty-line">待澄清问题会在后续对话中由智能体向你确认。</p>
          : <section className="xjm-section" aria-label="证据原话">
            <h2 className="xjm-h2">证据原话 · {evidence.length}</h2>
            {evidence.length === 0 ? <p className="xjm-empty-line">没有仍然有效的证据。</p> : <ul className="xjm-list">
              {evidence.map(id => { const ev = nodeById.get(id); return <li key={id}>
                <button type="button" className="xjm-row" onClick={() => openTrace(id)}>
                  <span className="xjm-row-text">{ev ? ev.content : `图外原话 ${shortId(id)}`}</span>
                  <span className="xjm-row-meta">{ev ? formatTime(ev.recorded_at) : '点击载入'}<IconArrow /></span>
                </button>
              </li> })}
            </ul>}
          </section>}
      </div>
    }

    if (detailFailure) {
      const t = FAILURE_TEXT[detailFailure.kind]
      return <div className="xjm-detail">{head}<StatusBlock icon={<IconAlert />} title={t.title} body={t.body} action={<button type="button" className="xjm-btn" onClick={() => setDetailNonce(n => n + 1)}>重试</button>} /></div>
    }
    // Only a record that matches the current selection may be shown or mutated; while loading or on
    // any mismatch, no edit/forget controls are rendered at all.
    const live = trace && traceId && trace.trace_id === traceId ? trace : null
    if (!live) return <div className="xjm-detail">{head}{traceId ? <div className="xjm-skeleton" aria-busy="true" aria-label="正在载入记录"><i /><i /><i /></div> : null}</div>
    const len = draft.trim().length
    const dirty = draft.trim() !== live.content
    const conflict = actionFailure?.kind === 'conflict'
    const relations = node ? relationsOf(node) : []
    return <div className="xjm-detail">
      {head}
      {actionFailure && <div className={conflict ? 'xjm-banner xjm-banner-warn' : 'xjm-banner xjm-banner-error'} role="alert">
        <p><b>{FAILURE_TEXT[actionFailure.kind].title}</b>{FAILURE_TEXT[actionFailure.kind].body}</p>
        {(conflict || actionFailure.kind === 'not_found') && <button type="button" className="xjm-btn" onClick={reloadLatest}>载入最新</button>}
      </div>}
      {editing
        ? <div className="xjm-editor">
          <label className="xjm-visually-hidden" htmlFor="xjm-draft">记忆内容</label>
          <textarea id="xjm-draft" className="xjm-textarea" value={draft} maxLength={MAX_CONTENT} autoFocus
            onChange={e => setDraft(e.target.value)} disabled={busy !== null}
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void save() } if (e.key === 'Escape') { setEditing(false); setDraft(live.content) } }} />
          <div className="xjm-editor-foot">
            <span className={len === 0 || len > MAX_CONTENT ? 'xjm-count xjm-count-bad' : 'xjm-count'}>{len} / {MAX_CONTENT}</span>
            <span className="xjm-spacer" />
            <button type="button" className="xjm-btn" disabled={busy !== null} onClick={() => { setEditing(false); setDraft(live.content); setActionFailure(null) }}>取消</button>
            <button type="button" className="xjm-btn xjm-btn-primary" disabled={busy !== null || !dirty || len === 0 || len > MAX_CONTENT} onClick={() => void save()}>{busy === 'save' ? '保存中…' : '保存'}</button>
          </div>
          <p className="xjm-note">保存会以新版本替换这条原话（生成新的记录 ID），重要度与锚点随之保留；依赖它的状态、摘要和人设会重新推导。</p>
        </div>
        : <p className="xjm-content">{live.content}</p>}
      {!editing && !confirmForget && <div className="xjm-actions">
        <button type="button" className="xjm-btn" onClick={() => { setDraft(live.content); setEditing(true); setActionFailure(null) }}><IconEdit />编辑</button>
        <button type="button" className="xjm-btn xjm-btn-danger-quiet" onClick={() => { setConfirmForget(true); setActionFailure(null) }}><IconForget />忘记</button>
      </div>}
      {confirmForget && <div className="xjm-confirm" role="alertdialog" aria-labelledby="xjm-forget-title">
        <p id="xjm-forget-title" className="xjm-confirm-title">忘记这条原话？</p>
        <p className="xjm-confirm-body">它和由它派生的摘要、状态证据会被清除，人设卡将重新推导。此操作无法撤销。</p>
        <div className="xjm-actions">
          <button type="button" className="xjm-btn" disabled={busy !== null} onClick={() => setConfirmForget(false)}>取消</button>
          <button type="button" className="xjm-btn xjm-btn-danger" disabled={busy !== null} onClick={() => void forget()}>{busy === 'forget' ? '正在忘记…' : '确认忘记'}</button>
        </div>
      </div>}
      <dl className="xjm-meta">
        <div><dt>记录时间</dt><dd>{formatTime(live.recorded_at)}</dd></div>
        <div><dt>来源</dt><dd>{sourceLabel(live.source_kind)}</dd></div>
        <div><dt>重要度</dt><dd>{Number(live.importance).toFixed(1)}</dd></div>
        <div><dt>优先级</dt><dd>{Number(live.priority).toFixed(2)}</dd></div>
        <div><dt>锚点</dt><dd>{live.anchor ? '是' : '否'}</dd></div>
        <div><dt>标识</dt><dd className="xjm-mono" title={live.trace_id}>{shortId(live.trace_id)}</dd></div>
      </dl>
      {relations.length > 0 && <section className="xjm-section" aria-label="关联">
        <h2 className="xjm-h2">关联 · {relations.length}</h2>
        <ul className="xjm-list">
          {relations.map(({ node: other }) => <li key={other.id}>
            <button type="button" className="xjm-row xjm-row-compact" disabled={busy !== null} onClick={() => selectTarget({ nodeId: other.id })}>
              <span className="xjm-row-lead"><LayerChip layer={other.layer} /><span className="xjm-kind">{KIND_LABEL[other.kind] ?? other.kind}</span></span>
              <span className="xjm-row-text xjm-row-text-1">{other.content}</span>
            </button>
          </li>)}
        </ul>
      </section>}
    </div>
  }

  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') setQuery('')
    if (e.key === 'Enter') setSearchNonce(n => n + 1)
  }

  const canSearch = Boolean(scopeId) && !pageFailure
  const nodesN = snapshot?.nodes.length ?? 0, edgesN = snapshot?.edges.length ?? 0

  return <div className="xjm-root">
    <div className="xjm-shell">
      <section className="xjm-stage" aria-label="三层神经记忆图">
        <NeuralField ref={fieldRef} nodes={field.nodes} edges={field.edges} selectedId={selectedGraphId} highlightIds={highlightIds}
          dimmed={Boolean(stageOverlay)} reducedMotion={reducedMotion} captions={captions} onSelect={onFieldSelect}
          label={`三层记忆神经图：语义 ${counts[0]}、情节 ${counts[1]}、工作 ${counts[2]} 个节点，${edgesN} 条关联。拖动旋转，滚轮缩放，方向键也可旋转。`} />
        <div className="xjm-stage-top">
        <header className="xjm-stage-head">
          <p className="xjm-eyebrow">NEURAL MEMORY</p>
          <h1 className="xjm-title">记忆</h1>
          <p className="xjm-sub">{snapshot ? `${nodesN} 个节点 · ${edgesN} 条关联` : '\u00a0'}</p>
        </header>
        <div className="xjm-controls" role="toolbar" aria-label="视图">
          <button type="button" className="xjm-tool" aria-label={autoRotate && !reducedMotion ? '暂停自转' : '开始自转'} aria-pressed={autoRotate && !reducedMotion}
            disabled={reducedMotion} title={reducedMotion ? '已遵循系统“减少动态效果”设置' : undefined}
            onClick={() => { const next = !autoRotate; setAutoRotate(next); fieldRef.current?.setAutoRotate(next) }}>{autoRotate && !reducedMotion ? <IconPause /> : <IconPlay />}</button>
          <span className="xjm-tool-sep" aria-hidden="true" />
          <button type="button" className="xjm-tool" aria-label="缩小" onClick={() => fieldRef.current?.zoomOut()}><IconMinus /></button>
          <button type="button" className="xjm-tool" aria-label="放大" onClick={() => fieldRef.current?.zoomIn()}><IconPlus /></button>
          <button type="button" className="xjm-tool" aria-label="重置视角" onClick={() => fieldRef.current?.reset()}><IconReset /></button>
        </div>
        </div>
        <div className="xjm-legend" aria-hidden={Boolean(stageOverlay)}>
          <span><i className="xjm-g-dot" />原话 · 可编辑</span>
          <span><i className="xjm-g-diamond" />派生 · 只读</span>
          <span><i className="xjm-g-link" />真实关联</span>
          <span className="xjm-legend-quiet">纤维与脉冲为神经结构，不代表记录</span>
        </div>
        {stageOverlay && <div className="xjm-overlay">{stageOverlay}</div>}
      </section>

      <aside className="xjm-inspector" aria-label="记忆检查器">
        <div className="xjm-insp-head">
          <div className="xjm-scope">
            <label className="xjm-scope-label" htmlFor="xjm-scope">记忆库</label>
            <select id="xjm-scope" className="xjm-select" value={scopeId ?? ''} disabled={!scopes.length || busy !== null}
              onChange={e => setScopeId(e.target.value || null)}>
              {!scopes.length && <option value="">{catalog === null && !failure ? "读取中…" : "无可用范围"}</option>}
              {scopes.map(s => <option key={s.scope_id} value={s.scope_id}>{s.agent_id || shortId(s.scope_id)} · {s.traces} 条</option>)}
            </select>
          </div>
          <div className="xjm-search">
            <IconSearch />
            <input type="search" className="xjm-search-input" placeholder="搜索原话，包括图外记录" aria-label="搜索记忆"
              value={query} disabled={!canSearch} onChange={e => setQuery(e.target.value)} onKeyDown={onSearchKey} />
            {searching && <span className="xjm-spinner xjm-spinner-sm" aria-hidden="true" />}
            {query && <button type="button" className="xjm-icon-btn xjm-icon-btn-sm" aria-label="清除搜索" onClick={() => setQuery('')}><IconClose /></button>}
          </div>
          {!showDetail && !query.trim() && <div className="xjm-tabs" role="tablist" aria-label="检查器视图">
            <button type="button" role="tab" aria-selected={tab === 'overview'} className="xjm-tab" onClick={() => setTab('overview')}>概览</button>
            <button type="button" role="tab" aria-selected={tab === 'persona'} className="xjm-tab" onClick={() => setTab('persona')}>人设卡{snapshot?.cards.length ? <span className="xjm-tab-count">{snapshot.cards.length}</span> : null}</button>
          </div>}
        </div>
        <div className="xjm-insp-body">
          {pageFailure ? <StatusBlock icon={pageFailure.kind === 'owner' ? <IconLock /> : <IconAlert />} title={FAILURE_TEXT[pageFailure.kind].title} body={FAILURE_TEXT[pageFailure.kind].body}
              action={pageFailure.kind !== 'owner' ? <button type="button" className="xjm-btn" onClick={() => setReloadKey(k => k + 1)}>重试</button> : undefined} />
            : loading && !snapshot ? <div className="xjm-skeleton" aria-hidden="true"><i /><i /><i /><i /></div>
            : showDetail ? renderDetail()
            : query.trim() ? renderHits()
            : isEmpty ? <p className="xjm-empty-line">暂无可管理的记忆。</p>
            : tab === 'persona' ? renderPersona(snapshot?.cards ?? [])
            : renderOverview()}
        </div>
        <div className="xjm-toast-slot" aria-live="polite">{toast && <div className="xjm-toast">{toast}</div>}</div>
      </aside>
    </div>
  </div>
}
