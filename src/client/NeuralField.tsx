import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from 'react'
import { NeuralEngine, type FieldEdge, type FieldNode } from './neural/engine.ts'
import type { PersonCard } from './types.ts'

export interface NeuralFieldHandle {
  zoomIn(): void
  zoomOut(): void
  reset(): void
  setAutoRotate(on: boolean): void
  fadeNode(id: string): Promise<void>
  screenOf(id: string): { x: number; y: number } | null
}

export interface NeuralFieldProps {
  nodes: FieldNode[]
  edges: FieldEdge[]
  selectedId: string | null
  highlightIds: readonly string[] | null
  dimmed: boolean
  reducedMotion: boolean
  captions: readonly [string, string, string]
  onSelect: (id: string | null) => void
  label: string
  popup?: ReactNode
  cards: readonly PersonCard[]
  onCardSelect: (accountId: string) => void
}

/** React shell for the canvas engine. The engine owns its RAF loop and listeners and is disposed on unmount. */
export const NeuralField = forwardRef<NeuralFieldHandle, NeuralFieldProps>(function NeuralField(props, ref) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const engineRef = useRef<NeuralEngine | null>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const popupLinkRef = useRef<SVGPathElement>(null)
  const cardsRef = useRef<HTMLDivElement>(null)
  const onSelectRef = useRef(props.onSelect)
  onSelectRef.current = props.onSelect
  const [failed, setFailed] = useState(false)
  const [cardCapacity, setCardCapacity] = useState(24)
  const [cardPage, setCardPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(props.cards.length / cardCapacity))
  const currentPage = Math.min(cardPage, pageCount - 1)
  const visibleCards = props.cards.slice(currentPage * cardCapacity, (currentPage + 1) * cardCapacity)

  useEffect(() => {
    const canvas = canvasRef.current, tip = tipRef.current, wrap = wrapRef.current
    if (!canvas || !tip || !wrap) return
    let engine: NeuralEngine
    try {
      engine = new NeuralEngine(canvas, tip, { reducedMotion: props.reducedMotion, layerCaptions: props.captions, onSelect: id => onSelectRef.current(id) })
    } catch {
      setFailed(true)
      return
    }
    engineRef.current = engine
    const qa = (globalThis as { __XJM_QA__?: { engine?: unknown } }).__XJM_QA__
    if (qa) qa.engine = engine
    const ro = new ResizeObserver(entries => {
      const r = entries[0]?.contentRect
      if (r) {
        engine.resize(r.width, r.height)
        const radius = 330 * Math.min(r.width / 1060, r.height / 900)
        setCardCapacity(Math.max(8, Math.min(100, Math.floor(2 * Math.PI * radius / 30))))
      }
    })
    ro.observe(wrap)
    const rect = wrap.getBoundingClientRect()
    engine.resize(rect.width, rect.height)
    const io = typeof IntersectionObserver === 'function'
      ? new IntersectionObserver(entries => engine.setVisible(entries.some(e => e.isIntersecting)))
      : null
    io?.observe(wrap)
    engine.start()
    return () => {
      ro.disconnect()
      io?.disconnect()
      engine.dispose()
      engineRef.current = null
      if (qa && qa.engine === engine) qa.engine = undefined
    }
    // The engine is created once per mount; later prop changes are pushed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { engineRef.current?.setOptions({ reducedMotion: props.reducedMotion, layerCaptions: props.captions, onSelect: id => onSelectRef.current(id) }) }, [props.reducedMotion, props.captions])
  useEffect(() => { engineRef.current?.setData(props.nodes, props.edges) }, [props.nodes, props.edges])
  useEffect(() => { engineRef.current?.setSelected(props.selectedId) }, [props.selectedId, props.nodes])
  useEffect(() => { engineRef.current?.setHighlight(props.highlightIds) }, [props.highlightIds, props.nodes])
  useEffect(() => { engineRef.current?.setDimmed(props.dimmed) }, [props.dimmed])
  useEffect(() => {
    if (!props.selectedId || !props.popup) return
    let frame = 0
    const position = () => {
      const point = engineRef.current?.screenOf(props.selectedId!)
      const popup = popupRef.current
      const wrap = wrapRef.current
      if (point && popup && wrap) {
        const left = Math.max(12, Math.min(wrap.clientWidth - popup.offsetWidth - 12, point.x + 18))
        const top = Math.max(70, Math.min(wrap.clientHeight - popup.offsetHeight - 16, point.y - popup.offsetHeight - 16))
        popup.style.transform = `translate(${left}px, ${top}px)`
        popup.style.visibility = 'visible'
        const endX = Math.max(left, Math.min(left + popup.offsetWidth, point.x))
        const endY = Math.max(top, Math.min(top + popup.offsetHeight, point.y))
        popupLinkRef.current?.setAttribute('d', `M ${point.x} ${point.y} L ${endX} ${endY}`)
      }
      frame = requestAnimationFrame(position)
    }
    frame = requestAnimationFrame(position)
    return () => cancelAnimationFrame(frame)
  }, [props.selectedId, props.popup])

  useEffect(() => {
    if (!props.cards.length || props.selectedId || props.dimmed) return
    let frame = 0
    const position = () => {
      const geometry = engineRef.current?.ringScreen(), wrap = wrapRef.current, group = cardsRef.current
      if (geometry && wrap && group) {
        const cards = Array.from(group.children) as HTMLElement[]
        cards.forEach((card, i) => {
          const angle = -Math.PI / 2 + i * 2 * Math.PI / cards.length
          const distance = geometry.radius * 1.1
          const x = geometry.x + Math.cos(angle) * distance - card.offsetWidth / 2
          const y = geometry.y + Math.sin(angle) * distance - card.offsetHeight / 2
          card.style.transform = `translate(${x}px, ${y}px)`
          card.style.visibility = x >= 0 && y >= 0 && x + card.offsetWidth <= wrap.clientWidth && y + card.offsetHeight <= wrap.clientHeight ? 'visible' : 'hidden'
        })
      }
      frame = requestAnimationFrame(position)
    }
    frame = requestAnimationFrame(position)
    return () => cancelAnimationFrame(frame)
  }, [props.cards, props.selectedId, props.dimmed, currentPage, cardCapacity])

  useImperativeHandle(ref, () => ({
    zoomIn: () => engineRef.current?.zoomBy(1.2),
    zoomOut: () => engineRef.current?.zoomBy(1 / 1.2),
    reset: () => engineRef.current?.resetView(),
    setAutoRotate: on => engineRef.current?.setAutoRotate(on),
    fadeNode: id => engineRef.current?.fadeNode(id) ?? Promise.resolve(),
    screenOf: id => engineRef.current?.screenOf(id) ?? null,
  }), [])

  return <div className="xjm-field" ref={wrapRef}>
    {failed
      ? <p className="xjm-field-fallback">此环境无法绘制神经图。右侧列表仍可检索与编辑全部记忆。</p>
      : <canvas ref={canvasRef} className="xjm-canvas" tabIndex={0} role="img" aria-label={props.label} />}
    <div ref={tipRef} className="xjm-tip" hidden aria-hidden="true" />
    {!props.selectedId && !props.dimmed && props.cards.length > 0 && <div className="xjm-orbit-cards" ref={cardsRef} aria-label="人设卡">
      {visibleCards.map((card, i) => <button type="button" key={card.account_id} className="xjm-orbit-card"
        data-horizontal={Math.cos(-Math.PI / 2 + i * 2 * Math.PI / visibleCards.length) > 0 ? 'right' : 'left'}
        data-vertical={Math.sin(-Math.PI / 2 + i * 2 * Math.PI / visibleCards.length) > 0 ? 'bottom' : 'top'}
        aria-label={`打开人设卡：${card.display_name || card.account_id}`} onClick={() => props.onCardSelect(card.account_id)}>
        <span className="xjm-orbit-initial" aria-hidden="true" style={{ transform: `rotate(${i * 360 / visibleCards.length}deg)` }} />
        <span className="xjm-orbit-popover"><span className="xjm-orbit-caption">PERSONA / 人设卡</span>
          <strong>{card.display_name || card.account_id}</strong>
          <span className="xjm-orbit-tags">{card.tags.slice(0, 2).map(tag => tag.value).join(' · ') || '暂无标签'}</span>
        </span>
      </button>)}
    </div>}
    {!props.selectedId && !props.dimmed && pageCount > 1 && <div className="xjm-orbit-pages" aria-label="人设卡分页">
      <button type="button" disabled={currentPage === 0} onClick={() => setCardPage(currentPage - 1)} aria-label="上一页人设卡">‹</button>
      <span>{currentPage + 1} / {pageCount} · {props.cards.length} 张人设卡</span>
      <button type="button" disabled={currentPage + 1 >= pageCount} onClick={() => setCardPage(currentPage + 1)} aria-label="下一页人设卡">›</button>
    </div>}
    {props.selectedId && props.popup && <><svg className="xjm-popup-link" aria-hidden="true"><path ref={popupLinkRef} /></svg><div ref={popupRef} className="xjm-node-popup">{props.popup}</div></>}
  </div>
})
