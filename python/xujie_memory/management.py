"""Token-protected data plane for the DSH owner-only, in-app memory panel.

The Host authenticates its owner using the DSH Gateway. The browser never gets
the kernel token. Explicit scope ids come from catalog(), never guessed user ids.
Source traces stay immutable: edits atomically replace evidence with a new trace.
"""
from __future__ import annotations

import json
import uuid

from .memory import Memory
from .profile import ProfileStore, TAG_KINDS, _override_key


class ManagementError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _clarification_text(kind, detail):
    if kind == "missing_slot":
        names = {"object": "具体对象", "subject": "涉及的人", "quantity": "数量",
                 "unit": "单位", "value": "具体内容", "creditor": "债权人", "debtor": "债务人"}
        missing = detail.get("missing")
        fields = [names.get(str(item), "必要信息") for item in missing] if isinstance(missing, list) else []
        return "这条记忆还缺少" + "、".join(dict.fromkeys(fields or ["必要信息"])) + "，暂时不能作为确定事实。"
    if kind == "conflict":
        return "这条记忆与已有信息存在分歧，需要进一步确认。"
    if kind == "unconfirmed":
        return "这条记忆还没有确认，暂时不能作为确定事实。"
    return "这条记忆的含义还不明确，需要结合原话确认。"


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
                relation = parts[2] if len(parts) > 2 else ""
                subject = parts[3] if len(parts) > 3 else ""
                label = {"preference": "偏好", "nickname": "称呼", "debt": "往来"}.get(relation, "记忆状态")
                if subject and subject != "-":
                    label += " · " + subject
                try:
                    value = json.loads(r["value_json"] or "null")
                except (TypeError, ValueError):
                    value = None
                value = str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else "请查看关联原话"
                nodes.append({"id": r["state_key"], "layer": "semantic", "kind": "state",
                              "content": label + "：" + value,
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
                "SELECT request_id,kind,detail_json,interpretation_ids_json,created_at FROM clarifications"
                " WHERE scope_id=? AND status='open' ORDER BY created_at DESC LIMIT 50",
                (scope_id,),
            ).fetchall()
            for r in pending:
                try:
                    detail = json.loads(r["detail_json"] or "{}")
                    ids = json.loads(r["interpretation_ids_json"] or "[]")
                except (TypeError, ValueError):
                    detail, ids = {}, []
                detail = detail if isinstance(detail, dict) else {}
                ids = [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []
                evidence = []
                original = ""
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    sources = conn.execute(
                        "SELECT DISTINCT t.trace_id,t.content FROM interpretations i JOIN traces t ON t.trace_id=i.trace_id"
                        f" WHERE i.interpretation_id IN ({placeholders}) AND t.scope_id=? AND t.erased_at IS NULL",
                        (*ids, scope_id),
                    ).fetchall()
                    evidence = [row["trace_id"] for row in sources]
                    if sources:
                        original = sources[0]["content"]
                description = _clarification_text(r["kind"], detail)
                if original:
                    description += " 原话：" + original[:120] + ("…" if len(original) > 120 else "")
                nodes.append({"id": r["request_id"], "layer": "working",
                              "kind": "clarification", "editable": False,
                              "content": description,
                              "recorded_at": r["created_at"], "evidence": evidence})
                for trace_id in evidence:
                    if trace_id in visible:
                        edges.append({"source": trace_id, "target": r["request_id"],
                                      "kind": "clarification_evidence"})
            cards = []
            store = ProfileStore(self.engine)
            for row in store.profiles(scope_id):
                cards.append(store.card(scope_id, row["account_id"], limit=50))
            stats = self.memory.stats(scope_id)
            emotions = [dict(r) for r in conn.execute(
                "SELECT name,intensity FROM ai_emotion_settings WHERE scope_id=? ORDER BY updated_at,name",
                (scope_id,),
            ).fetchall()]
        return {"ok": True, "scope_id": scope_id, "scope_mode": self.service.scope_mode,
                "nodes": nodes, "edges": edges, "cards": cards, "stats": stats, "emotions": emotions,
                "truncated": stats["traces"] > len(rows),
                "limit": limit, "render_links_are_not_semantic_inference": True}

    def search(self, scope_id, query, limit=30):
        self.scope(scope_id)
        return {"ok": True, "hits": self.memory.search(scope_id, query, limit=limit, candidates=None)}

    def action(self, scope_id, action, account_id, target_id, value, kind, expected):
        self.scope(scope_id)
        if action in ("tag_add", "tag_edit", "tag_delete"):
            return self._tag_action(scope_id, action, account_id, target_id, value, kind, expected)
        if action in ("clarify", "archive"):
            return self._clarification_action(scope_id, action, target_id, value)
        if action in ("emotion_set", "emotion_delete"):
            return self._emotion_action(scope_id, action, target_id, value)
        raise ManagementError("unknown_action")

    def _emotion_action(self, scope_id, action, name, value):
        name = name.strip()
        if not name or len(name) > 20 or any(ord(ch) < 32 for ch in name):
            raise ManagementError("invalid_emotion_name")
        with self.engine._tx() as conn:
            if action == "emotion_delete":
                result = conn.execute("DELETE FROM ai_emotion_settings WHERE scope_id=? AND name=?", (scope_id, name))
                if not result.rowcount:
                    raise ManagementError("emotion_not_found")
            else:
                try:
                    intensity = int(value)
                except ValueError as error:
                    raise ManagementError("invalid_emotion_intensity") from error
                if not 0 <= intensity <= 100:
                    raise ManagementError("invalid_emotion_intensity")
                exists = conn.execute("SELECT 1 FROM ai_emotion_settings WHERE scope_id=? AND name=?", (scope_id, name)).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM ai_emotion_settings WHERE scope_id=?", (scope_id,)).fetchone()[0]
                if not exists and count >= 20:
                    raise ManagementError("emotion_limit")
                conn.execute(
                    "INSERT INTO ai_emotion_settings(scope_id,name,intensity,updated_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(scope_id,name) DO UPDATE SET intensity=excluded.intensity,updated_at=excluded.updated_at",
                    (scope_id, name, intensity, self.engine.now()),
                )
        return {"ok": True}

    def _tag_action(self, scope_id, action, account_id, target_id, value, kind, expected):
        store = ProfileStore(self.engine)
        profile = store.get(scope_id, account_id)
        if not profile:
            raise ManagementError("profile_not_found")
        value = value.strip()
        if action != "tag_delete" and (not value or len(value) > 120):
            raise ManagementError("invalid_content_length")
        with self.engine._tx() as conn:
            if action == "tag_add":
                if kind not in TAG_KINDS:
                    raise ManagementError("invalid_tag_kind")
            else:
                tag = next((item for item in store.tags(scope_id, account_id)
                            if item["tag_id"] == target_id), None)
                if tag is None:
                    raise ManagementError("tag_not_found")
                if tag["value"] != expected:
                    raise ManagementError("version_conflict")
                kind = tag["kind"]
                if target_id.startswith("manual:"):
                    conn.execute("DELETE FROM person_tag_overrides WHERE profile_id=? AND kind=? AND target_key=?",
                                 (profile["profile_id"], kind, target_id))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO person_tag_overrides(profile_id,scope_id,kind,target_key,value,updated_at)"
                        " VALUES (?,?,?,?,NULL,?)",
                        (profile["profile_id"], scope_id, kind, _override_key(tag), self.engine.now()),
                    )
            if action != "tag_delete":
                conn.execute(
                    "INSERT INTO person_tag_overrides(profile_id,scope_id,kind,target_key,value,updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (profile["profile_id"], scope_id, kind, "manual:" + uuid.uuid4().hex, value, self.engine.now()),
                )
        return {"ok": True}

    def _clarification_action(self, scope_id, action, request_id, value):
        value = value.strip()
        if action == "clarify" and (not value or len(value) > 2000):
            raise ManagementError("invalid_content_length")
        with self.engine._tx() as conn:
            row = conn.execute(
                "SELECT * FROM clarifications WHERE scope_id=? AND request_id=? AND status='open'",
                (scope_id, request_id),
            ).fetchone()
            if row is None:
                raise ManagementError("clarification_not_found")
            if action == "clarify":
                ids = json.loads(row["interpretation_ids_json"] or "[]")
                actor = None
                if ids:
                    original = conn.execute(
                        "SELECT t.actor_id FROM interpretations i JOIN traces t ON t.trace_id=i.trace_id"
                        " WHERE i.interpretation_id=? AND t.scope_id=?", (ids[0], scope_id),
                    ).fetchone()
                    actor = original["actor_id"] if original else None
                if not actor:
                    raise ManagementError("clarification_source_not_found")
                self.service.orchestrator.ingest.ingest(
                    scope_id, "clarify:" + request_id + ":" + uuid.uuid4().hex,
                    actor, "user_statement", value, context={"peer_id": "assistant"},
                    confirmed=True, pattern_escalation=False,
                )
            conn.execute("UPDATE clarifications SET status=? WHERE scope_id=? AND request_id=?",
                         ("answered" if action == "clarify" else "dismissed", scope_id, request_id))
        return {"ok": True}

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
