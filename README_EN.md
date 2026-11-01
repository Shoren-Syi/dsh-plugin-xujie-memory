# Xujie Memory 0.7.0 · DSH in-app memory workspace

[简体中文](README.md) | English

**Acceptance candidate for DeepSeek Harness 0.2.1-rc.5; not yet verified in a real Windows DSH desktop.**

## New in 0.7

- Native “记忆” page in DSH via the official `sidebar.panellist` and `main` slots; no separate website.
- A rotating 3D neuronal field with three visible memory layers, fuzzy recall, true person cards, and original-memory edit/forget actions. Decorative fibers are not stored memories or inferred semantic relationships; real evidence edges are distinguished in the legend.
- Browser management calls use generated Typert Remote contracts and require the authenticated DSH owner. No kernel token or direct kernel URL is exposed to the page.
- An edit atomically invalidates old evidence and creates a replacement trace, preserving original event time, importance and anchors. Save returns a new trace ID. Dependent facts/person cards and retrieval are recomputed. Conflicting versions are rejected.
- New `memory_edit` tool: search first to obtain `trace_id` and `content_hash`, then submit `expected_hash` with the correction.

## Upgrade and data boundaries

Use the same profile and `dbPath` as 0.6. Before the first old-schema migration, the kernel creates a consistent private backup named `<dbPath>.pre-0.7.0.bak` without overwriting an existing backup. Previously erased residual summaries/projections are repaired once.

**The migration backup contains pre-upgrade data. Keep it private.** Forgetting is application-level removal, not secure disk erasure; it does not erase DSH chats, exports, old backups or historical disk/WAL contents.

The graph defaults to the latest 250 original traces plus selected derived nodes. Management search covers all active original traces in the selected memory scope; automatic conversational recall retains its latest-800-candidate limit. Derived facts, summaries and clarifications are read-only; follow their evidence to edit original wording. Person cards are derived from actual records, not independent editable profile fields.

Install the supplied local candidate archive/extracted folder with the same method and profile used for 0.6, then restart DSH. Registry and GitHub examples from older releases do not install this unpublished candidate. Preserve your data folder and follow `ACCEPTANCE.md` before relying on the upgrade.


Seamless long-term memory built for DeepSeek Harness (DSH).

**The value of memory is not having to think about it.** Xujie-Memory records relevant information locally, assembles memory context, and injects it into the prompt without asking the model to decide whether to call a memory tool. The Windows x64 package includes a runtime and generally requires no extra configuration after installation.

## Architecture

```mermaid
flowchart LR
    A[Session events] --> B[Filter user messages and injected content]
    B --> C[Semantic extraction and admission gates]
    C --> D[SQLite event records and state projections]
    D --> E[Weight decay and relevant memory recall]
    E --> F[Exact and fuzzy matching]
    F --> G[Assemble three memory layers]
    G --> H[Inject through a prompt section]
```

Events and projections support traceability and replay; episodic memory and person profiles also have their own storage structures. The default synchronous prompt section reads a cache, so host timing can make the current turn use the previous turn's material. See **Timing and scope** below.

| Feature | Typical tool-calling memory | Xujie-Memory |
|---|---|---|
| Retrieval decision | The model usually decides when to call a tool | Context is assembled and injected automatically |
| Organization | Depends on the implementation | Semantic, episodic, and working memory |
| Latency | Depends on the tool and implementation | Kernel requests have a cost; the default section reads a cache, while asynchronous mode waits for material |
| Follow-up limits | Depends on the implementation and prompt | Each item is asked once; at most one per turn and six per session by default |
| Decay and fuzzy recall | Depends on the implementation | Supports weight decay and fuzzy search |

This compares common design approaches; individual tool-based systems can differ in features and performance.

## Memory layers

- **Semantic:** person card, facts, and explicitly pinned anchors.
- **Episodic:** original wording and summaries, with fuzzy search for related past events.
- **Working:** recent messages and unresolved items.

Importance scoring affects how slowly memories fade. Explicit requests such as “remember this” pin an anchor. Retrieval does not reset decay. The kernel also exposes `/memory/mark`, `/memory/curve`, and `/memory/search` endpoints.

## Active tools (0.7)

Injection is automatic, but **actively recalling** and **removing what was remembered wrong** need a hand. 0.7 registers four tools:

| Tool | What it does | Side effect |
|---|---|---|
| `memory_search` | Literal/fuzzy search over stored wording; returns content, `trace_id`, `content_hash`, weight | read-only |
| `memory_edit` | Atomic replacement using `trace_id`, content and `expected_hash` | write; new trace ID |
| `memory_forget` | Deletes one memory (soft delete: content cleared, FTS row removed, dependent facts invalidated and recomputed) | write; `erased_at` kept for audit |
| `memory_status` | Counts for stored/erased entries, states, anchors, open items, plus kernel health | read-only |

When the user says “forget that”, the model can search for the `trace_id` itself and delete it — no manual database surgery. Kernel endpoints: `POST /memory/search`, `POST /memory/forget`, `POST /memory/stats`.

Hosts without a `tools` service skip registration silently; injection keeps working. 0.7 includes the native graphical page and memory editing.

The current sentence is no longer injected back as a past event (0.6 fix): `/turn` excludes it by `trace_id`, `/context` by content (whitespace and punctuation ignored). Ask about something else and older memories still appear.

## Installation

This candidate targets the DSH 0.2.1-rc.5 bundle/client mechanism and Node.js `^22.19 || >=24`. Windows x64 includes Python and the kernel; no separate Python installation or pip dependencies are needed. On macOS and Linux it falls back to system Python 3.10 or newer.

```bash
dsh plugin --profile <your-profile> add <extracted-package-folder-containing-package.json>
```

Install by package name from npm:

```bash
dsh plugin --profile <your-profile> add dsh-plugin-xujie-memory
```

Or install the tagged version directly from GitHub:

```bash
# Historical 0.6 command only; NOT this unpublished 0.7 candidate:
dsh plugin --profile <your-profile> add github:Shoren-Syi/dsh-plugin-xujie-memory#v0.6.0
```

Start a new session. The plugin starts the bundled kernel automatically. By default the database is stored at `~/.xujie-memory/memory.db`. Kernel failure skips memory for that turn rather than blocking the conversation.

Optional profile configuration:

```yaml
- id: xujie-memory
  config:
    endpoint: "http://127.0.0.1:8765"
    autoStart: true
    dbPath: ""
    statusFile: ""
    maxAskedTotal: 6
```

Set `statusFile` to a path to record mounting and readiness status. The kernel watchdog checks every five seconds and restarts with backoff; logs are written to `~/.xujie-memory/kernel.log`. Requests require JSON and the `X-Xujie-Token` stored at `~/.xujie-memory/token`.

## Timing and scope

The default `scope: user` shares facts, preferences, anchors, and the person card across sessions. Set `scope: session` for session isolation. The follow-up limit is per session in either mode.

The plugin caches material for up to 32 sessions. The default synchronous `section.text` reads the cache; if the host assembles the prompt before `/turn` completes, it uses the previous turn's material. Set `asyncSection: true` only when the host supports an asynchronous section.

Only `user/message` events are ingested. Runtime context and agent-instruction messages are filtered out, as are assistant and tool events. Each unresolved item is asked at most once; the default session limit is six follow-ups. Reaching the limit does not delete the records.

## Package contents

```text
lib/index.js             DSH integration, tool registration and kernel lifecycle
python/xujie_memory/     Python memory kernel
python/specs/            Interpretation rules
runtime/win32-x64/       Bundled Windows x64 Python runtime
cordis.patch.yml         DSH bundle patch
```

This upgrade is based on the supplied 0.6.0 archive. After installing development dependencies, run `npm run build` and `npm test`: the new suite exercises real temporary SQLite/HTTP and a Cordis DI test double. Browser QA uses a simulated main slot with the built client factory and real temporary kernel, not the real DSH desktop. Run `npm pack` to package built artifacts. Windows x64 uses the bundled Python runtime; other platforms need Python 3.10 or newer. Set `PYTHON` to select an interpreter.

The supplied baseline did not contain its historical regression suite. The distribution did not include `test/kernel_regression.py` or the kernel synchronization script. The 31/31 result in the changelog is historical and cannot be reproduced from this repository; the smoke test does not replace that regression suite.

