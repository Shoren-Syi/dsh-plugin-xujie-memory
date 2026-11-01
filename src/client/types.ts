/**
 * Client-side mirror of the Host DTOs (release src/types.ts). Kept structural so the
 * client tree compiles standalone; the parent may replace this import with
 * `../types.ts` when merging into release-0.7.0/src/client.
 */
export type { Catalog, Snapshot, Trace, SearchHit, EditResult, ForgetResult, PersonCard, PersonTag, Node, Edge, Stats, ScopeRow } from '../types.ts'
