import type { Catalog, EditResult, ForgetResult, SearchHit, Snapshot, Trace } from './types.ts'

/** Structural RemoteResult: `{ ok: true, value }` or `{ ok: false, error }` (RemoteError instance). */
export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: { readonly code: string; readonly message: string; readonly details?: unknown } }

/** The typed Remote face generated from MemoryController (ctx.remote.xujieMemory). */
export interface XujieMemoryRemote {
  catalog(): Promise<Result<Catalog>>
  snapshot(scopeId: string): Promise<Result<Snapshot>>
  detail(scopeId: string, traceId: string): Promise<Result<Trace>>
  search(scopeId: string, query: string): Promise<Result<SearchHit[]>>
  edit(scopeId: string, traceId: string, content: string, expectedHash: string): Promise<Result<EditResult>>
  forget(scopeId: string, traceId: string, expectedHash: string): Promise<Result<ForgetResult>>
  manage(scopeId: string, action: string, accountId: string, targetId: string, value: string, kind: string, expected: string): Promise<Result<boolean>>
}

export type FailureKind = 'owner' | 'conflict' | 'not_found' | 'scope' | 'invalid' | 'emotion_limit' | 'unavailable' | 'unknown'

export class MemoryFailure extends Error {
  readonly code: string
  readonly reason: string
  readonly kind: FailureKind
  constructor(code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'MemoryFailure'
    this.code = code
    const reason = typeof details === 'object' && details !== null && typeof (details as { reason?: unknown }).reason === 'string'
      ? (details as { reason: string }).reason : ''
    this.reason = reason
    this.kind = code === 'xujie-memory/owner-required' ? 'owner'
      : /version_conflict/.test(reason) ? 'conflict'
      : /trace_not_found/.test(reason) ? 'not_found'
      : /scope_not_found/.test(reason) ? 'scope'
      : /emotion_limit/.test(reason) ? 'emotion_limit'
      : /invalid_emotion/.test(reason) ? 'invalid'
      : /invalid_content_length/.test(reason) ? 'invalid'
      : code === 'xujie-memory/rejected' ? 'unavailable' : 'unknown'
  }
}

/** Unwrap one RemoteResult; failures become typed UI errors, never silent empties. */
export function unwrap<T>(result: Result<T>): T {
  if (result && result.ok === true) return result.value
  const error = (result as { error?: { code?: string; message?: string; details?: unknown } } | undefined)?.error
  throw new MemoryFailure(error?.code ?? 'unknown', error?.message ?? '记忆库操作未完成', error?.details)
}

export function asFailure(error: unknown): MemoryFailure {
  if (error instanceof MemoryFailure) return error
  const e = error as { code?: unknown; message?: unknown; details?: unknown } | undefined
  if (e && typeof e.code === 'string') return new MemoryFailure(e.code, String(e.message ?? ''), e.details)
  return new MemoryFailure('unknown', error instanceof Error ? error.message : '记忆库操作未完成')
}

/** UI adapter: every scope ID comes from catalog; no user ID, kernel token or URL is accepted. */
export class MemoryAdapter {
  constructor(private readonly remote: XujieMemoryRemote) {}
  async catalog(): Promise<Catalog> { return unwrap(await this.remote.catalog()) }
  async snapshot(scopeId: string): Promise<Snapshot> { return unwrap(await this.remote.snapshot(scopeId)) }
  async detail(scopeId: string, traceId: string): Promise<Trace> { return unwrap(await this.remote.detail(scopeId, traceId)) }
  async search(scopeId: string, query: string): Promise<SearchHit[]> { return unwrap(await this.remote.search(scopeId, query)) }
  async edit(scopeId: string, traceId: string, content: string, expectedHash: string): Promise<EditResult> {
    return unwrap(await this.remote.edit(scopeId, traceId, content, expectedHash))
  }
  async forget(scopeId: string, traceId: string, expectedHash: string): Promise<ForgetResult> {
    return unwrap(await this.remote.forget(scopeId, traceId, expectedHash))
  }
  async manage(scopeId: string, action: string, accountId = '', targetId = '', value = '', kind = '', expected = ''): Promise<boolean> {
    return unwrap(await this.remote.manage(scopeId, action, accountId, targetId, value, kind, expected))
  }
}
