import type { Context } from '@deepseek-ai/cordis'
import { Remote, RemoteError, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { Catalog, Snapshot, Trace, SearchHit, EditResult, ForgetResult, PersonCard } from './types.ts'

declare module '@deepseek-ai/cordis' {
  interface Context { xujieMemory: MemoryController }
}

/** Trusted Node-only bridge; it is never exported to the browser. */
export interface KernelBridge {
  request(path: string, body: Record<string, string | number>): Promise<any>
}

declare module '@deepseek-ai/dsh-typert-protocol' {
  interface RemoteErrorDetailsMap {
    'xujie-memory/owner-required': { action: string }
    'xujie-memory/rejected': { reason: string }
  }
}

/** Memory management is private to the authenticated DSH owner, not shared sessions. */
export class MemoryController extends TypertRemoteService {
  constructor(ctx: Context, private readonly bridge: KernelBridge) {
    super(ctx, 'xujieMemory')
  }

  private owner(): void {
    if (this.ctx.invocation?.peer.principal.kind !== 'owner') {
      throw new RemoteError('xujie-memory/owner-required', '只有 DSH 所有者可以管理记忆库', { action: 'memory/manage' })
    }
  }

  private async call(path: string, body: Record<string, string | number>): Promise<any> {
    this.owner()
    try { return await this.bridge.request(path, body) }
    catch (error) {
      throw new RemoteError('xujie-memory/rejected', '记忆库操作未完成', {
        reason: error instanceof Error ? error.message : 'kernel_unavailable',
      })
    }
  }

  @Remote
  async catalog(): Promise<Catalog> {
    const r = await this.call('/manage/catalog', {})
    return { scope_mode: r.scope_mode, scopes: r.scopes }
  }

  @Remote
  async snapshot(scopeId: string): Promise<Snapshot> {
    const r = await this.call('/manage/snapshot', { scope_id: scopeId })
    const cards: PersonCard[] = r.cards.map((c: any) => ({
      account_id: c.profile.account_id, display_name: c.profile.display_name ?? c.profile.account_id,
      status: c.profile.status, tags: c.tags.map((t: any) => ({
        tag_id: t.tag_id, kind: t.kind, value: t.value, source: t.source, confidence: t.confidence,
      })),
    }))
    const { scope_id, scope_mode, nodes, edges, emotions, truncated, limit } = r
    // Lifecycle statistics may carry an internal scope key; do not copy it to DTO.
    const { traces, traces_erased, events, states, anchors, clarifications_open, summaries } = r.stats
    return { scope_id, scope_mode, nodes, edges, cards, emotions, truncated, limit,
      stats: { traces, traces_erased, events, states, anchors, clarifications_open, summaries } }
  }

  @Remote
  async detail(scopeId: string, traceId: string): Promise<Trace> {
    return (await this.call('/manage/detail', { scope_id: scopeId, trace_id: traceId })).trace
  }

  @Remote
  async search(scopeId: string, query: string): Promise<SearchHit[]> {
    const r = await this.call('/manage/search', { scope_id: scopeId, query })
    return r.hits.map((h: any) => ({
      trace_id: h.trace_id, content: h.content, content_hash: h.content_hash,
      score: h.score, priority: Number(h.priority ?? 0), importance: Number(h.importance ?? 1),
      anchor: Boolean(h.anchor), recorded_at: h.recorded_at ?? '', layer: h.layer,
    }))
  }

  @Remote
  async edit(scopeId: string, traceId: string, content: string, expectedHash: string): Promise<EditResult> {
    const r = await this.call('/manage/edit', { scope_id: scopeId, trace_id: traceId, content, expected_hash: expectedHash })
    return { changed: r.changed, trace: r.trace,
      ...(r.replaced_trace_id ? { replaced_trace_id: r.replaced_trace_id, pending: r.pending } : {}) }
  }

  @Remote
  async forget(scopeId: string, traceId: string, expectedHash: string): Promise<ForgetResult> {
    const r = await this.call('/manage/forget', { scope_id: scopeId, trace_id: traceId, expected_hash: expectedHash })
    return { trace_id: r.trace_id, erased: r.erased, already_erased: r.already_erased, state_keys: r.state_keys }
  }

  @Remote
  async manage(scopeId: string, action: string, accountId: string, targetId: string,
               value: string, kind: string, expected: string): Promise<boolean> {
    await this.call('/manage/action', { scope_id: scopeId, action, account_id: accountId,
      target_id: targetId, value, kind, expected })
    return true
  }

  @Remote
  async qqGet(scopeId: string): Promise<string> {
    return JSON.stringify(await this.call('/manage/qq/get', { scope_id: scopeId }))
  }

  @Remote
  async qqEnsure(): Promise<string> {
    const result = await this.call('/manage/qq/ensure', {})
    return result.scope_id
  }

  @Remote
  async qqSave(scopeId: string, settings: string): Promise<boolean> {
    await this.call('/manage/qq/save', { scope_id: scopeId, settings })
    return true
  }
}
