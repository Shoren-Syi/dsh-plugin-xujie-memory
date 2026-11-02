import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from 'react'
import { NeuralEngine, type FieldEdge, type FieldNode } from './neural/engine.ts'

export interface NeuralFieldHandle {
  zoomIn(): void
  zoomOut(): void
  reset(): void
  setAutoRotate(on: boolean): void
  fadeNode(id: string): Promise<void>
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
}

/** React shell for the canvas engine. The engine owns its RAF loop and listeners and is disposed on unmount. */
export const NeuralField = forwardRef<NeuralFieldHandle, NeuralFieldProps>(function NeuralField(props, ref) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const engineRef = useRef<NeuralEngine | null>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const onSelectRef = useRef(props.onSelect)
  onSelectRef.current = props.onSelect
  const [failed, setFailed] = useState(false)

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
      if (r) engine.resize(r.width, r.height)
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
      }
      frame = requestAnimationFrame(position)
    }
    frame = requestAnimationFrame(position)
    return () => cancelAnimationFrame(frame)
  }, [props.selectedId, props.popup])

  useImperativeHandle(ref, () => ({
    zoomIn: () => engineRef.current?.zoomBy(1.2),
    zoomOut: () => engineRef.current?.zoomBy(1 / 1.2),
    reset: () => engineRef.current?.resetView(),
    setAutoRotate: on => engineRef.current?.setAutoRotate(on),
    fadeNode: id => engineRef.current?.fadeNode(id) ?? Promise.resolve(),
  }), [])

  return <div className="xjm-field" ref={wrapRef}>
    {failed
      ? <p className="xjm-field-fallback">此环境无法绘制神经图。右侧列表仍可检索与编辑全部记忆。</p>
      : <canvas ref={canvasRef} className="xjm-canvas" tabIndex={0} role="img" aria-label={props.label} />}
    <div ref={tipRef} className="xjm-tip" hidden aria-hidden="true" />
    {props.selectedId && props.popup && <div ref={popupRef} className="xjm-node-popup">{props.popup}</div>}
  </div>
})
