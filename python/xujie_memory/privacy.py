"""Removal of active projections that depend on explicitly erased evidence.

This is logical application erasure, not forensic disk erasure: SQLite WAL,
external exports/backups and the DSH conversation log are outside this API.
Audit identifiers, timestamps and hashes are retained, not replacement facts.
"""
from __future__ import annotations

import json


def repair_legacy(engine, profiles) -> None:
    """Once-only sweep for projections left by 0.6's incomplete forgetting."""
    with engine._tx() as conn:
        if conn.execute(
            "SELECT 1 FROM memory_maintenance WHERE name='privacy_repair_070'"
        ).fetchone():
            return
        scopes = set()
        for row in conn.execute(
            "SELECT trace_id,scope_id FROM traces WHERE erased_at IS NOT NULL"
        ).fetchall():
            keys = [r[0] for r in conn.execute(
                "SELECT DISTINCT e.state_key FROM events e JOIN event_evidence v"
                " ON e.event_id=v.event_id WHERE v.trace_id=?", (row["trace_id"],)
            ).fetchall()]
            purge_derived(engine, conn, row["trace_id"], keys)
            scopes.add(row["scope_id"])
        for scope in scopes:
            for profile in profiles.profiles(scope):
                profiles.derive_from_memory(scope, profile["account_id"])
        conn.execute(
            "INSERT INTO memory_maintenance(name,completed_at) VALUES (?,?)",
            ("privacy_repair_070", engine.now()),
        )


def purge_derived(engine, conn, trace_id: str, state_keys: list[str]) -> None:
    trace = conn.execute(
        "SELECT scope_id FROM traces WHERE trace_id=?", (trace_id,)
    ).fetchone()
    if trace is None:
        return
    scope = trace["scope_id"]
    # Merged summaries retain the original trace ids in input_refs_json too.
    # Clear content, not merely the active flag; otherwise it survives in detail.
    for row in conn.execute(
        "SELECT summary_id,input_refs_json FROM summaries WHERE scope_id=?", (scope,)
    ).fetchall():
        try:
            refs = json.loads(row["input_refs_json"])
        except (TypeError, ValueError):
            # An unreadable dependency cannot safely remain model-visible.
            refs = [trace_id]
        if trace_id in refs:
            conn.execute(
                "UPDATE summaries SET text='',lifecycle_state='archived',"
                "coverage_json='{}',omissions_json='[]' WHERE summary_id=?",
                (row["summary_id"],),
            )
    conn.execute(
        "UPDATE clarifications SET status='dismissed',detail_json='{}'"
        " WHERE scope_id=? AND EXISTS ("
        " SELECT 1 FROM json_each(clarifications.interpretation_ids_json) j"
        " JOIN interpretations i ON i.interpretation_id=j.value"
        " WHERE i.trace_id=?)", (scope, trace_id),
    )
    conn.execute(
        "UPDATE encodings SET payload_json='{}' WHERE trace_id=?", (trace_id,)
    )
    conn.execute(
        "UPDATE interpretations SET slots_json='{}',signals_json='{}' WHERE trace_id=?",
        (trace_id,),
    )
    conn.execute(
        "UPDATE events SET payload_json='{}' WHERE void=1 AND event_id IN"
        " (SELECT event_id FROM event_evidence WHERE trace_id=?)", (trace_id,),
    )
    conn.execute(
        "UPDATE person_tags SET value='',status='expired' WHERE scope_id=?"
        " AND evidence_trace_id=?", (scope, trace_id),
    )
    conn.execute(
        "UPDATE profile_alerts SET signals_json='{}',status='dismissed',resolved_at=?"
        " WHERE scope_id=? AND evidence_trace_id=?", (engine.now(), scope, trace_id),
    )
    for table in ("emotion_events", "relationship_events"):
        conn.execute(
            f"DELETE FROM {table} WHERE scope_id=? AND evidence_trace_id=?",
            (scope, trace_id),
        )
    conn.execute(
        "DELETE FROM grievances WHERE scope_id=? AND evidence_trace_id=?",
        (scope, trace_id),
    )
    conn.execute(
        "UPDATE lifecycle SET anchor=0,level='archived'"
        " WHERE scope_id=? AND ref_type='trace' AND ref_id=?", (scope, trace_id),
    )
    for key in state_keys:
        conn.execute("DELETE FROM state_snapshots WHERE state_key=?", (key,))
        conn.execute(
            "UPDATE states SET last_known_json=NULL WHERE state_key=?"
            " AND status NOT IN ('known','stale')", (key,),
        )