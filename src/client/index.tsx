import type { Context } from '@deepseek-ai/cordis'
import type { MainPanelId } from '@deepseek-ai/dsh-client-ui-layout/client'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
// Generated Typert contribution (ctx.remote.xujieMemory face + DTO codecs).
import type {} from '../../lib/typert.remote-client.js'
import contribution from '../../lib/typert.remote-client.js'
import { MemoryPanel } from './MemoryPanel.tsx'
import { NeuronGlyph } from './icons.tsx'
import PANEL_CSS from './panel.css'

/** One id brands the main panel and its sidebar launcher. */
export const PANEL_ID = 'xujie-memory' as MainPanelId

/** Browser services this contribution needs before apply runs. */
export const inject = ['slots', 'remote']

export function apply(ctx: Context): void {
  // 1. Mount the generated Remote contribution first. `remote.xujieMemory` only
  //    exists after this resolves, so injecting it before mounting would deadlock.
  //    $mount registers its own effect on this ctx; the returned disposer is also
  //    tied to this effect so unload unmounts the namespace.
  ctx.effect(() => ctx.remote.$mount(contribution), 'xujie-memory: remote contribution')

  // 2. Lifecycle-scoped stylesheet: every rule is .xjm-* prefixed and removed on unload.
  ctx.effect(() => {
    const style = document.createElement('style')
    style.dataset.xujieMemory = 'panel'
    style.textContent = PANEL_CSS
    document.head.append(style)
    return () => style.remove()
  }, 'xujie-memory: panel styles')

  // 3. Only after the namespace is live: register the main panel and its launcher.
  //    Registrations live on the injected child context, so they are withdrawn if
  //    the Remote namespace disappears (plugin unload or connection reset).
  ctx.inject(['slots', 'remote.xujieMemory'], inner => {
    const memoryRemote = inner.remote.xujieMemory
    inner.slots.inject('main', () => inner.slots.register({
      name: 'main',
      key: PANEL_ID,
      inject: () => ({ remote: memoryRemote }),
    }, MemoryPanel))
    inner.slots.inject('sidebar.panellist', () => inner.slots.register({
      name: 'sidebar.panellist',
      id: PANEL_ID,
      label: '记忆',
      order: 140,
    }, MemoryPanelIcon))
  })
}

/** Sidebar launcher glyph; the sidebar owns the button, label and active state. */
export function MemoryPanelIcon({ size }: PropsRuntime<'sidebar.panellist'>) {
  return <NeuronGlyph size={size} />
}

export { MemoryPanel }
