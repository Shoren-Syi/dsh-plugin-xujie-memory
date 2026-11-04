"""为有明确会话来源的旧原话创建可追溯的抽取式片段摘要。"""

from __future__ import annotations

import json
import re
from collections import Counter

from .summary import SummaryStore, validate_omissions

def _topic_members(rows) -> tuple[str, list]:
    """只挑选明确提到同一作品的原话，其他话题不能混入摘要。"""
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(set(re.findall(r'《[^《》\n]{2,40}》', row['content'])))
    candidates = [title for title, count in counts.items() if count >= 2]
    if not candidates:
        return '', []
    title = max(candidates, key=lambda value: (counts[value], value))
    return title, [row for row in rows if title in row['content']]


def _statement(text: str, title: str) -> str:
    """仅保留明确含作品名的陈述句，避免把提问写成事实。"""
    for sentence in re.split(r'(?<=[。！？!?])|\n', text.strip()):
        sentence = sentence.strip()
        if title not in sentence or '?' in sentence or '？' in sentence:
            continue
        outside_title = sentence.replace(title, '')
        if re.search(r'(?:吗|么|呢)[。！!]?$', outside_title):
            continue
        return sentence
    return ''


def compact_session(engine, scope_id: str, session: str) -> str | None:
    """压缩一批旧记录；保留近期原话，不跨会话猜测事件边界。"""
    with engine._tx() as conn:
        cursor = conn.execute(
            'SELECT recorded_at,trace_id FROM episode_compaction_cursors WHERE scope_id=? AND session_id=?',
            (scope_id, session),
        ).fetchone()
        rows = conn.execute(
            "SELECT trace_id,content,recorded_at FROM traces WHERE scope_id=? AND erased_at IS NULL"
            " AND json_valid(context_ref) AND json_extract(context_ref,'$._xjm_session')=?"
            " AND (? IS NULL OR (recorded_at,trace_id) > (?,?))"
            " AND NOT EXISTS (SELECT 1 FROM episode_summary_inputs e JOIN summaries s ON s.summary_id=e.summary_id"
            " WHERE e.scope_id=traces.scope_id AND e.trace_id=traces.trace_id AND s.lifecycle_state='active' AND s.validation_status='ok' AND s.text<>'')"
            " ORDER BY recorded_at,trace_id LIMIT 132",
            (scope_id, session, cursor['recorded_at'] if cursor else None,
             cursor['recorded_at'] if cursor else None, cursor['trace_id'] if cursor else None),
        ).fetchall()
        if len(rows) < 12:
            if cursor:
                conn.execute('DELETE FROM episode_compaction_cursors WHERE scope_id=? AND session_id=?', (scope_id, session))
            return None
        batch = None
        summary = ''
        for start in range(0, len(rows) - 11, 4):
            title, candidate = _topic_members(rows[start:start + 8])
            if not candidate:
                continue
            statements = [(row, _statement(row['content'], title)) for row in candidate]
            statements = [(row, part) for row, part in statements if part]
            if len(statements) < 2:
                continue
            candidate = [row for row, _ in statements]
            texts = [row['content'] for row in candidate]
            excerpt = '；'.join(dict.fromkeys(part for _, part in statements))
            numbers = set(re.findall(r'\d+', ''.join(texts)))
            if (len(excerpt) > 400 or len(excerpt) >= sum(map(len, texts))
                    or not numbers.issubset(set(re.findall(r'\d+', excerpt)))
                    or validate_omissions(texts, excerpt)):
                continue
            batch, summary = candidate, excerpt
            break
        if batch is None:
            if len(rows) == 132:
                last = rows[127]
                conn.execute(
                    'INSERT INTO episode_compaction_cursors VALUES (?,?,?,?)'
                    ' ON CONFLICT(scope_id,session_id) DO UPDATE SET recorded_at=excluded.recorded_at,trace_id=excluded.trace_id',
                    (scope_id, session, last['recorded_at'], last['trace_id']),
                )
            elif cursor:
                conn.execute('DELETE FROM episode_compaction_cursors WHERE scope_id=? AND session_id=?', (scope_id, session))
            return None
        result = SummaryStore(engine).create(
            scope_id, [row['trace_id'] for row in batch],
            summarizer=lambda _texts: summary,
            generator_version='session-extractive-v1',
            coverage={'kind': 'conversation_excerpt', 'complete': False},
        )
        conn.executemany(
            'INSERT INTO episode_summary_inputs(scope_id,trace_id,summary_id) VALUES (?,?,?)',
            [(scope_id, row['trace_id'], result.summary_id) for row in batch],
        )
        return result.summary_id


def compact_recall(engine, scope_id: str, related: list[dict], query: str, limit: int) -> list[dict]:
    """仅当摘要能替代多条命中且更短时，使用有证据的会话片段。"""
    if len(related) < 2:
        return related
    rows = engine._conn().execute(
        "SELECT text,input_refs_json FROM summaries WHERE scope_id=? AND lifecycle_state='active'"
        " AND validation_status='ok' AND generator_version='session-extractive-v1'"
        " ORDER BY created_at DESC LIMIT 100", (scope_id,),
    ).fetchall()
    remaining = list(related)
    for row in rows:
        refs = set(json.loads(row['input_refs_json']))
        matched = [item for item in remaining if item['trace_id'] in refs]
        text = row['text']
        if len(matched) < 2 or not text:
            continue
        normalized_query = ''.join(ch for ch in query.casefold() if ch.isalnum())
        if normalized_query and any(
            ''.join(ch for ch in (engine.get_trace(tid) or {}).get('content', '').casefold() if ch.isalnum()) == normalized_query
            for tid in refs
        ):
            continue
        if any(not any(part.strip() and part.strip() in text
                       for part in re.split(r'[。！？\n]', item['content'].strip())) for item in matched):
            continue
        if len(text) + 18 >= sum(len(item['content']) for item in matched) or len(text) > limit:
            continue
        first = remaining.index(matched[0])
        remaining = [item for item in remaining if item['trace_id'] not in refs]
        remaining.insert(first, {**matched[0], 'content': '会话片段摘录（可能不完整）：' + text})
    return remaining
