# dsh-plugin-xujie-memory

[简体中文](README.md) | English

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

## Installation

The package targets the DSH 0.2 preview bundle mechanism and Node.js `^22.19 || >=24`. Windows x64 includes Python and the kernel; no separate Python installation or pip dependencies are needed. On macOS and Linux it falls back to system Python 3.10 or newer.

```bash
dsh plugin --profile <your-profile> add <path-to-extracted-dsh-plugin-xujie-memory>
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
lib/index.js             DSH integration and kernel lifecycle
python/xujie_memory/     Python memory kernel
python/specs/            Interpretation rules
runtime/win32-x64/       Bundled Windows x64 Python runtime
cordis.patch.yml         DSH bundle patch
```

This repository contains the distributed 0.5.0 package. Its package metadata refers to development scripts and tests that are not included in the distribution.

