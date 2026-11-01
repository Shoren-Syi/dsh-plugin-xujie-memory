"""Token-protected data plane for the DSH owner-only, in-app memory panel.

The Host authenticates its owner using the DSH Gateway. The browser never gets
the kernel token. Explicit scope ids come from catalog(), never guessed user ids.
Source traces stay immutable: edits atomically replace evidence with a new trace.
"""
from __future__ import annotations

import json
import uuid

from .memory import Memory
from .profile import ProfileStore


class ManagementError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Management:
    def __init__(self, service):
        self.service = service
        self.engine = service.engine
        self.memory = Memory(self.engine)

    def scope(self, scope_id):
        row = self.engine._conn().execute(
            "SELECT * FROM scopes WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if row is None:
            raise ManagementError("scope_not_found")
        return dict(row)

    def catalog(self):
        rows = self.engine._conn().execute(
            "SELECT s.scope_id,s.agent_id,COUNT(t.trace_id) AS traces FROM scopes s"
            " LEFT JOIN traces t ON s.scope_id=t.scope_id AND t.erased_at IS NULL"
            " GROUP BY s.scope_id ORDER BY s.agent_id,s.scope_id"
        ).fetchall()
        return {"ok": True, "scope_mode": self.service.scope_mode,
                "scopes": [dict(r) for r in rows]}

    def detail(self, scope_id, trace_id):
        self.scope(scope_id)
        row = self.engine.get_trace(trace_id)
        if row is None or row["scope_id"] != scope_id or row["erased_at"]:
            raise ManagementError("trace_not_found")
        weights = self.memory.lifecycle.batch_priority([trace_id]).get(trace_id, {})
        return {"ok": True, "trace": {
            "id": trace_id, "trace_id": trace_id, "scope_id": scope_id,
            "content": row["content"], "content_hash": row["content_hash"],
            "recorded_at": row["recorded_at"], "source_kind": row["source_kind"],
            "importance": weights.get("base", 1), "anchor": bool(weights.get("anchor")),
            "priority": weights.get("priority", 0),
        }}

    def snapshot(self, scope_id, limit=250):
        self.scope(scope_id)
        conn = self.engine._conn()
        limit = max(1, min(500, int(limit)))
        # A short read transaction prevents mixed snapshots during concurrent edits.
        with self.engine._tx():
            rows = conn.execute(
                "SELECT trace_id,content,content_hash,recorded_at FROM traces"
                " WHERE scope_id=? AND erased_at IS NULL"
                " ORDER BY recorded_at DESC,trace_id LIMIT ?", (scope_id, limit)
            ).fetchall()
            weights = self.memory.lifecycle.batch_priority([r["trace_id"] for r in rows])
            nodes, edges = [], []
            visible = {r["trace_id"] for r in rows}
            for i, r in enumerate(rows):
                w = weights.get(r["trace_id"], {})
                node = {"id": r["trace_id"], "trace_id": r["trace_id"],
                        "layer": "episodic", "kind": "trace", "content": r["content"],
                        "content_hash": r["content_hash"], "recorded_at": r["recorded_at"],
                        "importance": w.get("base", 1), "anchor": bool(w.get("anchor")),
                        "priority": w.get("priority", 0), "editable": True}
                nodes.append(node)
                if node["anchor"]:
                    aid = "anchor:" + r["trace_id"]
                    nodes.append({**node, "id": aid, "layer": "semantic", "kind": "anchor"})
                    edges.append({"source": r["trace_id"], "target": aid,
                                  "kind": "anchor_projection"})
                if i < 8:
                    wid = "working:" + r["trace_id"]
                    nodes.append({**node, "id": wid, "layer": "working", "kind": "recent"})
                    edges.append({"source": r["trace_id"], "target": wid,
                                  "kind": "working_projection"})
            states = conn.execute(
                "SELECT state_key,value_json,status,evaluated_as_of FROM states"
                " WHERE scope_id=? AND status IN ('known','stale','disputed')"
                " ORDER BY evaluated_as_of DESC,state_key LIMIT ?", (scope_id, limit)
            ).fetchall()
            for r in states:
                evidence = conn.execute(
                    "SELECT DISTINCT v.trace_id FROM event_evidence v"
                    " JOIN events e ON e.event_id=v.event_id"
                    " JOIN traces t ON t.trace_id=v.trace_id"
                    " WHERE e.state_key=? AND e.void=0 AND t.erased_at IS NULL"
                    " ORDER BY v.trace_id", (r["state_key"],),
                ).fetchall()
                parts = r["state_key"].split("|")
                label = " · ".join(parts[1:4])
                nodes.append({"id": r["state_key"], "layer": "semantic", "kind": "state",
                              "content": label + "： " + (r["value_json"] or "未知"),
                              "status": r["status"], "editable": False,
                              "evidence": [e["trace_id"] for e in evidence]})
                for e in evidence:
                    if e["trace_id"] in visible:
                        edges.append({"source": e["trace_id"], "target": r["state_key"],
                                      "kind": "evidence"})
            summaries = conn.execute(
                "SELECT summary_id,text,input_refs_json,created_at FROM summaries"
                " WHERE scope_id=? AND lifecycle_state='active' AND text<>''"
                " ORDER BY created_at DESC,summary_id LIMIT 60", (scope_id,),
            ).fetchall()
            for r in summaries:
                try:
                    refs = json.loads(r["input_refs_json"])
                except (TypeError, ValueError):
                    continue
                nodes.append({"id": r["summary_id"], "layer": "episodic", "kind": "summary",
                              "content": r["text"], "editable": False,
                              "recorded_at": r["created_at"], "evidence": refs})
                for ref in refs:
                    if ref in visible:
                        edges.append({"source": ref, "target": r["summary_id"],
                                      "kind": "summary_input"})
            pending = conn.execute(
                "SELECT request_id,kind,detail_json FROM clarifications"
                " WHERE scope_id=? AND status='open' ORDER BY created_at DESC LIMIT 50",
                (scope_id,),
            ).fetchall()
            for r in pending:
                nodes.append({"id": r["request_id"], "layer": "working",
                              "kind": "clarification", "editable": False,
                              "content": r["kind"] + "：" + (r["detail_json"] or "")})
            cards = []
            store = ProfileStore(self.engine)
            for row in store.profiles(scope_id):
                cards.append(store.card(scope_id, row["account_id"], limit=50))
            stats = self.memory.stats(scope_id)
        return {"ok": True, "scope_id": scope_id, "scope_mode": self.service.scope_mode,
                "nodes": nodes, "edges": edges, "cards": cards, "stats": stats,
                "truncated": stats["traces"] > len(rows),
                "limit": limit, "render_links_are_not_semantic_inference": True}

    def search(self, scope_id, query, limit=30):
        self.scope(scope_id)
        return {"ok": True, "hits": self.memory.search(scope_id, query, limit=limit, candidates=None)}

    def edit(self, scope_id, trace_id, content, expected_hash):
        self.scope(scope_id)
        content = content.strip()
        if not content or len(content) > 2000:
            raise ManagementError("invalid_content_length")
        with self.engine._tx():
            trace = self.detail(scope_id, trace_id)["trace"]
            if not expected_hash or trace["content_hash"] != expected_hash:
                raise ManagementError("version_conflict")
            if trace["content"] == content:
                return {"ok": True, "changed": False, "trace": trace}
            original = self.engine.get_trace(trace_id)
            actor = original["actor_id"]
            occurred_at = original.get("source_time") or original["recorded_at"]
            self.memory.forget(scope_id, trace_id)
            # A saved edit is an explicit user correction, not a fabricated
            # conversational turn. Do not run profile-watching/auto-replies.
            result = self.service.orchestrator.ingest.ingest(
                scope_id, "edit:" + trace_id + ":" + uuid.uuid4().hex,
                actor, "user_statement", content,
                context={"peer_id": "assistant", "effective_at": occurred_at}, confirmed=True,
                pattern_escalation=False,
            )
            self.engine._conn().execute(
                "UPDATE traces SET source_time=?,source_time_precision=? WHERE trace_id=?",
                (occurred_at, original.get("source_time_precision"), result.trace_id),
            )
            self.memory.mark(scope_id, result.trace_id, trace["importance"])
            if trace["anchor"]:
                self.memory.lifecycle.set_anchor(result.trace_id, True)
            store = ProfileStore(self.engine)
            for row in store.profiles(scope_id):
                store.derive_from_memory(scope_id, row["account_id"])
            self.engine._conn().execute(
                "INSERT INTO memory_edits(edit_id,scope_id,old_trace_id,new_trace_id,edited_at)"
                " VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, scope_id, trace_id, result.trace_id, self.engine.now()),
            )
            saved = self.detail(scope_id, result.trace_id)["trace"]
        return {"ok": True, "changed": True, "replaced_trace_id": trace_id, "trace": saved,
                "pending": len(result.clarifications)}

    def forget(self, scope_id, trace_id, expected_hash):
        self.scope(scope_id)
        with self.engine._tx():
            trace = self.detail(scope_id, trace_id)["trace"]
            if not expected_hash or trace["content_hash"] != expected_hash:
                raise ManagementError("version_conflict")
            result = self.memory.forget(scope_id, trace_id)
        return {"ok": True, **result}
