/** Public JSON DTOs for the owner-only memory management page. */
export interface ScopeRow { scope_id: string; agent_id: string; traces: number }
export interface Trace { id: string; trace_id: string; scope_id: string; content: string; content_hash: string; recorded_at: string; source_kind: string; importance: number; anchor: boolean; priority: number }
export interface Node { id: string; layer: string; kind: string; content: string; editable: boolean; trace_id?: string; content_hash?: string; recorded_at?: string; importance?: number; priority?: number; anchor?: boolean; status?: string; evidence?: string[] }
export interface Edge { source: string; target: string; kind: string }
export interface PersonTag { tag_id: string; kind: string; value: string; source: string; confidence: number }
export interface PersonCard { account_id: string; display_name: string; status: string; tags: PersonTag[] }
export interface Stats { traces: number; traces_erased: number; events: number; states: number; anchors: number; clarifications_open: number; summaries: number }
export interface Catalog { scope_mode: string; scopes: ScopeRow[] }
export interface Snapshot { scope_id: string; scope_mode: string; nodes: Node[]; edges: Edge[]; cards: PersonCard[]; stats: Stats; truncated: boolean; limit: number }
export interface SearchHit { trace_id: string; content: string; content_hash: string; score: number; priority: number; anchor: boolean; importance: number; recorded_at: string; layer: string }
export interface EditResult { changed: boolean; trace: Trace; replaced_trace_id?: string; pending?: number }
export interface ForgetResult { trace_id: string; erased: boolean; already_erased: boolean; state_keys: string[] }
