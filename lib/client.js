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
  function apply(ctx) {
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: id }, Panel))
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id, label: '记忆', order: 140 }, Icon))
  }
  return { name: id, inject: ['slots'], apply }
} })
