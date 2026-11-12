window.__ModuleLoader__.load({ id: 'dsh-plugin-xujie-memory', factory: (require) => {
  const React = require('react')
  const h = React.createElement
  const id = 'hippocampus-deepseek'
  function Icon({ size = 20 }) {
    return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, 'aria-hidden': true },
      h('circle', { cx: 12, cy: 12, r: 3 }),
      h('circle', { cx: 5, cy: 6, r: 1.5 }), h('circle', { cx: 19, cy: 6, r: 1.5 }),
      h('circle', { cx: 5, cy: 18, r: 1.5 }), h('circle', { cx: 19, cy: 18, r: 1.5 }),
      h('path', { d: 'M6.3 7l3.4 3M17.7 7l-3.4 3M6.3 17l3.4-3m8 3-3.4-3' }))
  }
  function Panel() {
    return h('iframe', { title: '记忆看板', src: '/api/hippo.board', style: { display: 'block', border: 0, width: '100%', height: '100%', minHeight: 0, background: '#080d14' } })
  }
  // ------------------------------------------------------------ 顶部工具条兜底
  //
  // 宿主的会话头部是 `.headerActions{display:flex}` 且**没有 wrap**，
  // 各个插件往里面塞按钮（智能体团队 / 费用明细 / 归档 / 标签 / 移动到工作区…），
  // 窗口一窄，这一行就溢出压到标题上。宿主是 app.asar，改不了；
  // 在客户端补一条能换行的兜底样式：允许换行、允许子项收缩、长文字省略。
  // 只在 `headerActions` 上生效，不动其它布局。
  const HEADER_SHIM = `
[class*="_headerActions"]{flex-wrap:wrap;row-gap:4px;min-width:0;max-width:100%}
[class*="_headerActions"]>*{min-width:0;flex-shrink:1}
[class*="_headerActions"] button,[class*="_headerActions"] [role="button"]{min-width:0;max-width:100%}
[class*="_headerActions"] button>span,[class*="_headerActions"] [role="button"]>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
`
  function installHeaderShim() {
    if (typeof document === 'undefined') return
    const KEY = 'hippo-header-shim'
    if (document.getElementById(KEY)) return
    const style = document.createElement('style')
    style.id = KEY
    style.textContent = HEADER_SHIM
    document.head.appendChild(style)
  }

  function apply(ctx) {
    installHeaderShim()
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: id }, Panel))
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id, label: '记忆', order: 140 }, Icon))
  }
  return { name: id, inject: ['slots'], apply }
} })
