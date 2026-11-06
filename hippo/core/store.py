"""DAO 层：全项目唯一允许出现 SQL 的业务模块（迁移执行器除外）。

约定：
  - 所有语句参数化，禁止字符串拼接值。
  - 对外只返回 models 里的 Pydantic 对象。
  - 需要多语句原子性的方法，内部用 transaction()；单语句不用，省一次 BEGIN。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..log import get_logger
from .db import connect, table_exists, transaction
from .facts import KeyTable, decide_fact, load_keys
from .keywords import STOPWORDS, clean_keywords
from .schema import anchor_raw
from .models import (
    Affect, CardTrait, Curiosity, Doubt, Entity, Event, Fact, LedgerEntry, Metric,
    Plan, Turn,
)

log = get_logger("store")

# 倒排聚合阶段的超量系数（M10.1）。聚合时还不知道 session / 是否被取代，
# 只能先多取一点，过滤完再截。4 是实测的折中：
# 单 session 库上它是纯浪费（取 4 倍还只用前 1 倍），多 session 库上它是补偿。
# 调大到 20 会让聚合阶段白扫 3 倍的行；调到 1 会在 15 窗口的库上丢结果。
KW_OVERFETCH = 4


def _one_edit(a: str, b: str) -> bool:
    """两个词的编辑距离是否 ≤1（替换 / 插入 / 删除 一个字符）。

    不用 DP 矩阵：长度差和"一处不同"这两条判据就够，而且快 20 倍——
    这段代码在查询路径上会被调用几百次。
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = 0
        for x, y in zip(a, b):
            if x != y:
                diff += 1
                if diff > 1:
                    return False
        return True
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


class StoreError(RuntimeError):
    pass


class NoEvidenceError(StoreError):
    """角色卡特征必须挂证据（手册 M7.2）。"""


@dataclass
class FactWrite:
    """一次事实写入的结果。

    `action` 不是"成功/失败"两态，而是六种：reject / doubt / same / refine /
    replace / insert。写报告和日志时要分开统计——把 refine 算进 replace，
    "用户搬了几次家"就会虚高，而那正是这个模块要回答的问题。
    """

    id: int | None
    action: str
    decision: Any  # facts.Decision，这里用 Any 是为了不把 dataclass 类型拧成循环导入

    @property
    def written(self) -> bool:
        return self.action in ("insert", "refine", "replace", "same")


# 批量导入时临时摘除的 FTS 触发器（与 001_init.sql 保持一致）
_FTS_TRIGGERS = ("events_ai", "events_ad", "events_au")

_KEYS: KeyTable | None = None


def fact_keys() -> KeyTable:
    """事实词表。惰性加载：导入模块时读文件会让 `import hippo.store` 有 IO 副作用。"""
    global _KEYS
    if _KEYS is None:
        _KEYS = load_keys()
    return _KEYS

_CREATE_FTS_TRIGGERS = (
    """
    CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
        INSERT INTO events_fts(rowid, summary, keywords)
        VALUES (new.id, new.summary, new.keywords);
    END
    """,
    """
    CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
        INSERT INTO events_fts(events_fts, rowid, summary, keywords)
        VALUES ('delete', old.id, old.summary, old.keywords);
    END
    """,
    """
    CREATE TRIGGER events_au AFTER UPDATE OF summary, keywords ON events BEGIN
        INSERT INTO events_fts(events_fts, rowid, summary, keywords)
        VALUES ('delete', old.id, old.summary, old.keywords);
        INSERT INTO events_fts(rowid, summary, keywords)
        VALUES (new.id, new.summary, new.keywords);
    END
    """,
)

_BULK_EVENT_SQL = (
    "INSERT INTO events(id, session, event_time, ingest_time, tier, weight, w0,"
    " keywords, summary, raw) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _now() -> int:
    return int(time.time())


def _split_tokens(keywords: str | Sequence[str]) -> list[str]:
    """切分并清洗关键词。清洗策略在 keywords.py，这里只是调用点。

    放在写入层而不是调用方，是为了让"脏关键词进不来"成为结构性保证：
    以后无论谁调 add_event，都不需要记得先清洗。
    """
    return clean_keywords(keywords)


def _dedupe_tokens(keywords: str | Sequence[str]) -> list[str]:
    """只做机械去重，不做策略清洗。

    调用方已经用实体名上下文清洗过一遍时走这条（见 add_event 的 precleaned）：
    再洗一次会把"猫"这种单字实体名当成长度不合规砍掉，
    而实体名恰恰是最该保留的关键词。
    """
    if isinstance(keywords, str):
        raw = keywords.replace("，", " ").replace(",", " ").split()
    else:
        raw = list(keywords)
    seen, out = set(), []
    for t in raw:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class Store:
    def __init__(self, conn: sqlite3.Connection, cfg: Any = None):
        self.conn = conn
        # 配置挂在 store 上，而不是让检索层自己去 load_config：
        # 一次检索要读 4 个 section（retrieve/rank/pack/decay），
        # 每个都去解析一遍 yaml 是 4 次文件 IO，而这条路径每轮对话都走。
        #
        # `cfg=None` 时检索层用代码里的默认值（不是"没有配置"，是"用同一套默认"）。
        # 测试基本都走这条：它们要的是行为，不是配置。
        #
        # explore_mode（M25.3）：好奇探索期间为 True，所有写操作在 tx()
        # 里被拒。放在 store 上而不是当参数往下传——写路径有 38 个方法，
        # 靠"记得传参"是守不住的。
        self.explore_mode = False
        if cfg is not None:
            self.attach_config(cfg)

    def attach_config(self, cfg: Any) -> None:
        self.cfg = cfg
        for name in ("retrieve", "rank", "pack", "decay"):
            section = getattr(cfg, name, None)
            if section is not None:
                setattr(self, f"{name}_cfg", section)

    # ------------------------------------------------------------------ 生命周期
    @classmethod
    def open(cls, path: str | Path, cfg: Any = None) -> "Store":
        """打开库，并**顺手把 schema 升到最新**。

        为什么 open 要迁移：用户 `pip install -U` 之后不会记得敲 `hippo migrate`。
        实测代价是这样的——老库（v3）缺 `cached_tokens` 列，`run_stats()`
        抛 `no such column`，而 `hippo stats` 里那句 `except Exception: pass`
        把它吞了，于是报表少了一整段，看起来"这个库没有抽取记录"。

        **升级完包之后功能静默变少**，是比崩溃更难查的一类故障。
        迁移本身幂等（已是最新就返回空列表），一次 SELECT 的开销可以忽略。

        显式的 `hippo migrate` 仍然保留：它会把每一步打印出来，
        排查迁移失败时要用。
        """
        from .migrate import migrate

        conn = connect(path)
        try:
            applied = migrate(conn)
            if applied:
                log.info("schema.auto_migrated", extra={"fields": {
                    "path": str(path), "applied": applied}})
        except Exception:  # noqa: BLE001
            # 迁移失败就把连接还回去，让调用方看到真实错误，
            # 而不是拿着一个半新半旧的库继续跑。
            conn.close()
            raise
        return cls(conn, cfg)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """可重入事务。

        M5.3 要把 entities→events→traits→facts→plans→doubts 写成一个原子块，
        而每个写方法内部自己也要事务。直接嵌套会 `cannot start a transaction
        within a transaction`。

        两条路：把每个方法拆成"带事务/不带事务"两份（方法数翻倍），
        或者让事务可重入（十行）。选后者。

        判据是 conn.in_transaction：isolation_level=None 时单条语句自动提交，
        语句结束后它必然是 False，所以外层没开事务时这里照常 BEGIN。
        """
        # 守卫放在最前面，连着 in_transaction 分支一起拦：不然"已经在事务里"
        # 就成了绕过只读限制的后门。
        self._assert_writable()
        if self.conn.in_transaction:
            yield self.conn
        else:
            with transaction(self.conn) as c:
                yield c

    def _assert_writable(self) -> None:
        """只读模式（M25.3）下写库一律拒绝。

        三个不走 tx() 的写方法（mark_doubt_asked / bump_doubt_impact /
        set_state）必须自己调它——它们是单语句自动提交，没有闸门。
        这条守卫有没有漏，test_m25_3 会拿一张方法清单逐个试。
        """
        if self.explore_mode:
            raise PermissionError("explore_mode：好奇探索只读，不许写库（M25.3）")

    # ------------------------------------------------------------------ events
    def add_event(
        self,
        *,
        session: str,
        raw: str,
        summary: str = "",
        keywords: str | Sequence[str] = "",
        event_time: int | None = None,
        event_time_end: int | None = None,
        ingest_time: int | None = None,
        tier: int = 2,
        w0: float = 0.5,
        weight: float | None = None,
        arousal: float = 0.0,
        entities: Iterable[int] | None = None,
        source: str = "user",
        kind: str = "event",
        pinned: int = 0,
        time_precision: str = "day",
        time_guessed: int = 0,
        summary_truncated: int = 0,
        summary_flagged: int = 0,
        source_text: str | None = None,
        precleaned: bool = False,
        giveup_level: str = "",
    ) -> int:
        if not raw or not raw.strip():
            raise StoreError("raw 不可为空：原文是唯一真相源")
        # 写入时校验，而不是只靠数据库触发器。触发器防的是"事后改"，
        # 这里防的是"一开始就写了个模型润色过的句子"——后者更常见，
        # 因为 raw 是模型给的，而模型天生喜欢把话说通顺。
        if source_text is not None and anchor_raw(raw, source_text) != raw:
            raise StoreError(
                "raw 不是原文片段：原文是唯一真相源，请先 anchor_raw() 再入库")
        now = _now()
        ingest_time = ingest_time if ingest_time is not None else now
        event_time = event_time if event_time is not None else ingest_time
        weight = w0 if weight is None else weight
        tokens = _dedupe_tokens(keywords) if precleaned else _split_tokens(keywords)

        with self.tx():
            cur = self.conn.execute(
                """
                INSERT INTO events (
                    session, event_time, event_time_end, ingest_time, last_access,
                    time_precision, time_guessed, tier, weight, w0, arousal,
                    recall_count, keywords, summary, raw, entities,
                    source, kind, pinned, summary_truncated, summary_flagged,
                    giveup_level
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session, event_time, event_time_end, ingest_time, None,
                    time_precision, time_guessed, tier, weight, w0, arousal,
                    0, " ".join(tokens), summary, raw,
                    json.dumps(list(entities or []), ensure_ascii=False),
                    source, kind, pinned, int(summary_truncated),
                    int(summary_flagged), giveup_level,
                ),
            )
            event_id = int(cur.lastrowid)
            if tokens:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)",
                    # 索引键统一小写：`AI` 和 `ai` 在倒排里必须是同一个键，
                    # 否则用户输 "go" 查不到库里存的 "Go"——而它看起来
                    # 完全正常（检索返回空，AI 说"不记得"）。
                    # 显示用的原文在 events.keywords 里，不受影响。
                    [(t.lower(), event_id) for t in tokens],
                )
        return event_id

    def bulk_import_events(self, rows: Iterable[Sequence[Any]]) -> int:
        """批量导入事件。行格式：
        (session, event_time, ingest_time, tier, weight, w0, keywords, summary, raw)

        逐行 add_event 会让 trigram 索引逐条重建，10 万条要 7s；
        这里摘掉触发器批量写、最后一次性 rebuild，约 1.4s。
        归档还原、语料灌库、迁移都走这条路。
        """
        rows = list(rows)
        if not rows:
            return 0
        with self.tx():
            base = int(self.conn.execute(
                "SELECT coalesce(max(id), 0) FROM events").fetchone()[0])
            payload: list[tuple[Any, ...]] = []
            kw_pairs: list[tuple[str, int]] = []
            for i, r in enumerate(rows, start=1):
                if len(r) != 9:
                    raise StoreError("bulk_import_events 每行必须是 9 个字段")
                eid = base + i
                payload.append((eid, *r))
                for t in _split_tokens(r[6]):
                    kw_pairs.append((t, eid))

            for name in _FTS_TRIGGERS:
                self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.conn.executemany(_BULK_EVENT_SQL, payload)
            if kw_pairs:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)",
                    [(t.lower(), eid) for t, eid in kw_pairs],
                )
            for sql in _CREATE_FTS_TRIGGERS:
                self.conn.execute(sql)
            self.conn.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
        return len(payload)

    def get_event(self, event_id: int) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def get_events(self, ids: Sequence[int]) -> list[Event]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE id IN ({marks})", tuple(ids)
        ).fetchall()
        by_id = {r["id"]: Event.from_row(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]   # 保持入参顺序

    def fetch_events(
        self,
        ids: Sequence[int],
        session: str,
        *,
        include_historical: bool = False,
    ) -> list[Event]:
        """按 id 批量取，**过 session / 归档 / 取代**。

        与 `get_events` 的区别：那个是给"我已经知道这个 id 想要它"的调用方用的
        （CLI 单查、测试校验），这个是给检索用的——检索拿到的 id 来自倒排，
        可能是别的 session 的，也可能是已被取代的。

        session 过滤在 Python 里做（理由见 `_kw_fetch`）：写进 SQL 会让
        查询计划器改选 session 索引扫全表。
        """
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        cond = [f"id IN ({marks})", "archived = 0"]
        if not include_historical:
            cond.append("superseded_by IS NULL")
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(cond)}", tuple(ids)).fetchall()
        out = []
        for r in rows:
            if r["session"] != session:
                continue
            out.append(Event.from_row(r))
        return out

    def top_events(
        self,
        session: str,
        candidates: Sequence[int],
        *,
        limit: int = 16,
        include_historical: bool = False,
    ) -> list[Event]:
        """候选集里权重最高的几条。L2 的关键词零命中时的兜底（M10.2）。

        为什么要兜底：M9 实测"我几点睡""我吃辣吗"这类查询认不出实体，
        关键词也命中不了（库里存的是"晚11点睡""不吃辣"）。返回空的话
        AI 只能答"不记得"，而事实就在库里。

        这里**不是**按相关度降级，是"这次没找到线索，按重要性给点东西"。
        """
        if not candidates:
            return []
        marks = ",".join("?" * len(candidates))
        cond = [f"id IN ({marks})", "archived = 0"]
        if not include_historical:
            cond.append("superseded_by IS NULL")
        # 多取一些再按 session 滤（同 _kw_fetch 的理由）
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(cond)} "
            f"ORDER BY weight DESC, event_time DESC, id DESC LIMIT ?",
            (*candidates, limit * 4)).fetchall()
        out = [Event.from_row(r) for r in rows if r["session"] == session]
        return out[:limit]

    def list_pinned(
        self, session: str, *, include_historical: bool = False,
    ) -> list[Event]:
        """用户钉住的记忆。不管检索命中与否都必须进 prompt。"""
        cond = ["session = ?", "archived = 0", "pinned = 1"]
        if not include_historical:
            cond.append("superseded_by IS NULL")
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(cond)} ORDER BY id",
            (session,)).fetchall()
        return [Event.from_row(r) for r in rows]

    # ------------------------------------------------------------ M16.2 钉住
    def set_pinned(self, event_id: int, pinned: bool = True) -> bool:
        """钉住 / 撤销钉住。返回这条事件现在是不是 pinned。

        钉住时顺手把 `tier` 改成 1（hot）：pinned 的语义是"永远在场"，
        而 tier 决定注入格式。不改的话，一条用户明确要求记住的事会以
        cold 的"仅关键词"形态出现——形式上在场，实际上等于没记住。

        撤销时**不**把 tier 改回去：它会由下一次 `recompute` 按权重算，
        手改回去等于替公式做决定。
        """
        with self.tx():
            if pinned:
                cur = self.conn.execute(
                    "UPDATE events SET pinned = 1, tier = 1 WHERE id = ?",
                    (int(event_id),))
            else:
                cur = self.conn.execute(
                    "UPDATE events SET pinned = 0 WHERE id = ?", (int(event_id),))
        return bool(pinned and cur.rowcount)

    def count_pinned(self, session: str) -> int:
        return int(self.conn.execute(
            "SELECT count(*) FROM events WHERE session = ? AND pinned = 1"
            " AND archived = 0 AND superseded_by IS NULL", (session,)).fetchone()[0])

    def pin_warn_once(self, session: str, n: int, tokens: int) -> bool:
        """C 段超预算时"提醒一次"。返回 True 表示这次该提醒。

        为什么不是"只提醒一次然后永远不提"：用户清理到 8 条以下之后又攒到
        12 条，是**新的一次失控**，该再说一声。所以判据是"上次提醒时的条数"
        而不是一个布尔。反过来，只要条数没涨就不重复——每轮都提一次
        会让提醒变成噪声，用户连看都不看。
        """
        if n <= 0:
            return False
        mark = self.get_state(session, "pin_warned_at", "0")
        last = int(mark.split(":")[0] or 0) if mark else 0
        if n > last:
            with self.tx():
                self.set_state(session, "pin_warned_at", f"{n}:{tokens}")
            return True
        return False

    def raws_of_session(self, session: str, *, skip_kind: str = "") -> list[str]:
        """一个 session 里所有事件的原文。

        评测用：要算 raw 锚定率，必须拿到"入过库的 raw"，
        而不是"模型说过但被丢弃的 raw"。两者差得远，混起来算会高估。

        skip_kind="raw" 可以排掉降级兜底事件——它们的 raw 是整窗对话，
        一定能锚上，算进分母会虚高锚定率。
        """
        sql = ("SELECT raw FROM events WHERE session = ? AND archived = 0")
        args: list[Any] = [session]
        if skip_kind:
            sql += " AND kind != ?"
            args.append(skip_kind)
        rows = self.conn.execute(sql + " ORDER BY id", tuple(args)).fetchall()
        return [str(r["raw"]) for r in rows]

    def summary_flag_counts(self, session: str) -> dict[str, int]:
        """摘要质量标记的计数。评测和 `hippo stats` 都要看这两个数。

        分开统计是刻意的：truncated 上升说明"我们砍得多了"（可以改阈值），
        flagged 上升说明"模型写得差了"（要改 prompt）。合成一个数就分不清了。
        """
        row = self.conn.execute(
            "SELECT coalesce(sum(summary_truncated),0) AS t,"
            " coalesce(sum(summary_flagged),0) AS f, count(*) AS n"
            " FROM events WHERE session = ? AND archived = 0 AND kind != 'raw'",
            (session,)).fetchone()
        return {"truncated": int(row["t"]), "flagged": int(row["f"]),
                "n": int(row["n"])}

    def summary_flag_totals(self) -> dict[str, int]:
        """全库的摘要标记合计。

        **不能按 `list_sessions()` 循环累加**——那个列表来自 turns 表，
        而事件可能存在于没有 turn 记录的会话里（导入、回放、手动补录）。
        按 turns 遍历会漏掉它们，表现是"统计少了一段"，且不报错。
        """
        row = self.conn.execute(
            "SELECT coalesce(sum(summary_truncated),0) AS t,"
            " coalesce(sum(summary_flagged),0) AS f, count(*) AS n"
            " FROM events WHERE archived = 0 AND kind != 'raw'").fetchone()
        return {"truncated": int(row["t"]), "flagged": int(row["f"]),
                "n": int(row["n"])}

    def event_ids_of_session(self, session: str, *, skip_kind: str = "") -> list[int]:
        """同 raws_of_session，但要 id。评测算自召回要用它配对。"""
        sql = "SELECT id FROM events WHERE session = ? AND archived = 0"
        args: list[Any] = [session]
        if skip_kind:
            sql += " AND kind != ?"
            args.append(skip_kind)
        rows = self.conn.execute(sql + " ORDER BY id", tuple(args)).fetchall()
        return [int(r["id"]) for r in rows]

    def window_detail(self, session: str) -> dict[str, list[dict[str, Any]]]:
        """一个 session 抽出来的四类结构化记忆，供人工核对。

        评测必须能一眼看到 facts 的 owner 对不对。
        "fact：用户/宠物/猫" 这种错误，光看事件数永远发现不了——
        数量全对，内容全错，这是最坏的一种"指标绿"。
        """
        facts = [
            {"entity": r["owner"] or "?", "key": r["key"], "value": r["value"],
             "conf": round(float(r["confidence"] or 0), 2)}
            for r in self.conn.execute(
                "SELECT e.canonical AS owner, f.key, f.value, f.confidence "
                "FROM facts f LEFT JOIN entities e ON e.id = f.entity_id "
                "WHERE f.session = ? AND f.valid_to IS NULL ORDER BY f.id",
                (session,)).fetchall()
        ]
        traits = [
            {"entity": r["owner"] or "?", "trait": r["trait"], "status": r["status"]}
            for r in self.conn.execute(
                # card_traits 没有 session 列，只能靠 entity 归属过滤。
                # 写 LEFT JOIN + WHERE e.session 会退化成 INNER JOIN，
                # 行为一样但读起来像在保留孤儿行——这里直接用子查询说清楚。
                "SELECT e.canonical AS owner, t.trait, t.status "
                "FROM card_traits t JOIN entities e ON e.id = t.entity_id "
                "WHERE e.session = ? AND t.valid_to IS NULL ORDER BY t.id",
                (session,)).fetchall()
        ]
        plans = [
            {"content": r["content"], "due": r["due_at"], "recur": r["recur"]}
            for r in self.conn.execute(
                "SELECT content, due_at, recur FROM plans "
                "WHERE session = ? AND fired = 0 ORDER BY id",
                (session,)).fetchall()
        ]
        doubts = [
            {"q": r["question"], "conf": round(float(r["confidence"] or 0), 2)}
            for r in self.conn.execute(
                "SELECT question, confidence FROM doubts "
                "WHERE session = ? AND resolved = 0 ORDER BY id",
                (session,)).fetchall()
        ]
        return {"facts": facts, "traits": traits, "plans": plans, "doubts": doubts}

    def search_by_tokens(
        self,
        session: str,
        tokens: Sequence[str],
        *,
        limit: int = 40,
        candidates: Sequence[int] | None = None,
        include_historical: bool = False,
    ) -> list[tuple[Event, int]]:
        """关键词倒排检索（M10.1）。返回 (事件, 命中数)，按命中数排序。

        保留这个名字是因为 M1/M6 的测试在用它；真正的实现在 `kw_search`，
        这里只是不带前缀兜底的薄包装。
        """
        return [(ev, hit) for ev, hit, _m in self.kw_search(
            session, tokens, limit=limit, candidates=candidates,
            include_historical=include_historical, prefix_fallback=False)]

    def kw_search(
        self,
        session: str,
        tokens: Sequence[str],
        *,
        limit: int = 40,
        candidates: Sequence[int] | None = None,
        include_historical: bool = False,
        prefix_fallback: bool = True,
        prefix_min_hits: int = 3,
    ) -> list[tuple[Event, int, list[str]]]:
        """倒排检索（M10.1）。返回 (事件, 命中数, 命中的 token)，按命中数排序。

        ## 为什么是两段，而不是一条 join

        一条 SQL 的写法（`kw_index JOIN events ... GROUP BY e.id`）看起来更自然，
        实测却慢 4 倍：SQLite 的查询计划会**先按 session 扫 events**，
        再对每一条回查倒排索引（`idx_kw_event`）。8000 条事件 = 8000 次索引查找，
        16.75ms，而倒排聚合本身只要 2ms。

        所以拆成：
          1. 只在 kw_index 上聚合，拿到 (event_id, hit, tokens)（2ms）
          2. 用主键把这几百行取回来，再过 session / 状态

        第 1 段的 `ORDER BY hit DESC LIMIT` 是在**不完整的语境**下做的：
        还不知道哪个 session、是否被取代、weight 多少。所以先超量取
        `limit × KW_OVERFETCH` 条，过滤完再截。多 session 的库上这个放大
        就是补偿（真机单 session 时它只是白取一点）。

        ## 前缀兜底

        `token >= '老王' AND token < '老王\uffff'` = "库里的词以老王开头"
        （老王头 ⊃ 老王）。只在精确命中不足 `prefix_min_hits` 条时才跑（D64）。
        手册写的是 `token LIKE :p || '%'`，等价，但 LIKE 在 SQLite 里默认
        大小写不敏感，用不上 BINARY 索引；范围比较能用。
        """
        tokens = [t for t in tokens if t]
        if not tokens:
            return []

        # 自适应超量：先按 limit 取，过滤（session/取代/归档）后不够才放大。
        # 固定放大 4 倍在单 session 库上是纯浪费——多构造 120 个 Event，
        # 每个 ~7µs，就是 0.8ms，而 M10.1 的总预算只有 8ms。
        cap = limit
        while True:
            rows, n_agg = self._kw_run(
                session, tokens, limit, cap, candidates,
                include_historical, prefix_fallback, prefix_min_hits)
            # n_agg < cap：倒排里就这么多条，再放大也还是这些
            if len(rows) >= limit or n_agg < cap or cap >= limit * KW_OVERFETCH:
                return rows
            cap = min(cap * 2, limit * KW_OVERFETCH)

    def _kw_run(
        self,
        session: str,
        tokens: Sequence[str],
        limit: int,
        cap: int,
        candidates: Sequence[int] | None,
        include_historical: bool,
        prefix_fallback: bool,
        prefix_min_hits: int,
    ) -> tuple[list[tuple[Event, int, list[str]]], int]:
        merged: dict[int, tuple[int, list[str]]] = {}
        for eid, hit, toks in self._kw_aggregate(tokens, "exact", candidates, cap):
            merged[eid] = (hit, toks)
        n_exact = len(merged)

        if prefix_fallback and len(merged) < prefix_min_hits:
            for eid, hit, toks in self._kw_aggregate(tokens, "prefix", candidates, cap):
                if eid in merged:
                    # 同一个词既精确命中又被前缀命中，不该算两分：token 取并集
                    # 后重算命中数。SQL 层面几乎不会走到（IN 和范围有交集），
                    # 留着是因为"两条路都命中"在语义上合法，结果不能因此翻倍。
                    both = set(merged[eid][1]) | set(toks)
                    merged[eid] = (len(both), sorted(both))
                else:
                    merged[eid] = (hit, toks)

        # M27.1 第四档：**后缀**。前缀管"老王头 ⊃ 老王"，后缀管
        # "陈医生 ⊃ 医生"——中文里"姓+职业""地名+中心词"全是这个结构，
        # 而前缀一个都捞不到。M26 的 P23（"看哪个医生"）就漏在这里。
        #
        # 只在**前面全空**时跑，理由和错别字兜底一样：`LIKE '%x'` 用不上
        # BINARY 索引，是 31k 行的一次全扫，实测 1.6ms。常态不付这个钱。
        cfg = getattr(self, "retrieve_cfg", None)
        if (getattr(cfg, "suffix_fallback", True) and prefix_fallback
                and not merged):
            for eid, hit, toks in self._kw_aggregate(tokens, "suffix", candidates, cap):
                merged.setdefault(eid, (hit, toks))

        if prefix_fallback and not merged:
            # 第三档：错别字。**必须排在 prefix 之后**，且只在前面全落空时跑。
            # 顺序错了会互相顶掉：实测"年终"先被纠成"年龄/年糕"（5 条），
            # merged 一非空，prefix 阶段就被跳过，"年终奖"再也进不来（事故 61）。
            for eid, hit, toks in self._kw_fuzzy(tokens, candidates, cap):
                merged.setdefault(eid, (hit, toks))

        if not merged:
            return [], 0
        return self._kw_fetch(session, merged, limit, include_historical), max(n_exact, len(merged))

    def kw_lookup(self, tokens: Sequence[str]) -> set[str]:
        """这些片段里，哪些是库里真的出现过的词。

        用途：M12 的话题判断。把一句话切成 n-gram 后，只有库内词才有
        跨句比较的价值——跨词边界的碎片（"租涨"）在任何两句话之间都不会重合，
        留着只会稀释覆盖率。
        """
        if not tokens:
            return set()
        marks = ",".join("?" * len(tokens))
        rows = self.conn.execute(
            f"SELECT DISTINCT token FROM kw_index WHERE token IN ({marks})",
            list(tokens),
        ).fetchall()
        return {r["token"] for r in rows}

    def co_occur(self, a: Sequence[str], b: Sequence[str]) -> bool:
        """这两组词有没有在**同一条事件**里出现过。

        用途：判断"这一句是不是还在说刚才那件事"。词面重合判不了这个
        ——"我搬家了"→"房租涨了多少"，两句一个共同的词都没有，但
        它们是同一条记忆里的两句话（事件 1 的关键词是"搬家 朝阳 海淀 房租"）。

        代价一次 JOIN，走 idx_kw_event。比引入 embedding 便宜三个数量级，
        也比"话题聚类"早一个里程碑就能用。
        """
        if not a or not b:
            return False
        ma = ",".join("?" * len(a))
        mb = ",".join("?" * len(b))
        row = self.conn.execute(
            f"SELECT 1 FROM kw_index x JOIN kw_index y ON x.event_id = y.event_id "
            f"WHERE x.token IN ({ma}) AND y.token IN ({mb}) LIMIT 1",
            [*a, *b],
        ).fetchone()
        return row is not None

    def kw_vocab(self) -> dict[str, tuple[str, ...]]:
        """kw_index 里出现过的词，按**首字**分桶。

        只用于错别字兜底（M10.3）。按首字分桶是因为中文错字大多错在
        第二个字以后（"老汪/老王"、"甲壮腺/甲状腺"），首字对得上；
        这样比较次数从"查询片段 × 全词表"降到"× 同首字的几十个"。

        不缓存：`DISTINCT` 走 PK 索引，2 万行 2~4ms，而它只在
        "精确和前缀都没命中"时才跑。缓存一旦失效（增量写入没清）
        会让检索静默退化成"永远查不到新词"，比慢一点糟得多。
        """
        rows = self.conn.execute(
            "SELECT DISTINCT token FROM kw_index "
            "WHERE length(token) BETWEEN 2 AND 6").fetchall()
        buckets: dict[str, list[str]] = {}
        for r in rows:
            t = r["token"]
            buckets.setdefault(t[0], []).append(t)
        return {k: tuple(v) for k, v in buckets.items()}

    def _kw_fuzzy(
        self, tokens: Sequence[str], candidates: Sequence[int] | None, cap: int,
    ) -> list[tuple[int, int, list[str]]]:
        """错别字兜底：找与查询片段**编辑距离 ≤1** 的库内词，再用它们查一遍。

        "老汪" 和 "老王" 字面上一个字符都不共享，n-gram 和前序遍历都救不了。
        这是最后一道，只在前面全部落空时跑（一次查询最多跑一次）。
        """
        vocab = self.kw_vocab()
        fixed: list[str] = []
        for t in tokens:
            if len(t) < 2:
                continue
            # 首字是虚词的片段不纠。中文里"被骂/被挠""给他的/给他的"这种
            # "虚词 + 实词"结构，纠错改的是虚词那一半，而虚词没有区分度：
            # 实测"组里多少人看到我被骂"被纠成"被挠"，把正确答案挤出了 top-8。
            if t[0] in STOPWORDS:
                continue
            for cand in vocab.get(t[0], ()):
                # **只允许等长替换**，不允许插入/删除。
                # 中文错字绝大多数是同音替换（老汪/老王、甲壮腺/甲状腺），
                # 而"少人→商人""里多→里巷"这类插入删除型的误纠，
                # 会把本来靠 weight 兜底能命中的事件挤掉——
                # 实测放宽到"编辑距离≤1"时，留出集从 0.95 掉到 0.90。
                if len(cand) == len(t) and cand != t and _one_edit(t, cand):
                    fixed.append(cand)
        if not fixed:
            return []
        # 纠出来的词可能又互相重复，去重后走正常聚合
        return self._kw_aggregate(tuple(dict.fromkeys(fixed)), "exact", candidates, cap)

    def _kw_aggregate(
        self,
        tokens: Sequence[str],
        mode: str,
        candidates: Sequence[int] | None,
        cap: int,
    ) -> list[tuple[int, int, list[str]]]:
        """只在 kw_index 上做聚合。不碰 events 表，也不看 session。"""
        cond: list[str] = []
        params: list[Any] = []
        if mode == "exact":
            cond.append(f"token IN ({','.join('?' * len(tokens))})")
            params.extend(tokens)
        elif mode == "suffix":
            # 后缀：库里的词以查询片段结尾（陈医生 / 医生）。
            # 单字不参与——"医"会捞回"兽医/牙医/医院"，噪声比收益大。
            able = [t for t in tokens if len(t) >= 2]
            if not able:
                return []
            cond.append("(" + " OR ".join("token LIKE '%' || ?" for _ in able) + ")")
            params.extend(able)
        else:
            # 前缀范围：\uffff 当上界。UTF-8 的 memcmp 序与码点序一致，
            # 所以"以 p 开头"等价于 p <= token < p+U+10FFFF。
            # 单字片段不参与：它会捞回半库（跟 D60 同一个理由）。
            able = [t for t in tokens if len(t) >= 2]
            if not able:
                return []
            cond.append("(" + " OR ".join(
                "(token >= ? AND token < ?)" for _ in able) + ")")
            for t in able:
                params.extend([t, t + "\U0010ffff"])
        if candidates:
            cond.append(f"event_id IN ({','.join('?' * len(candidates))})")
            params.extend(candidates)

        sql = f"""
            SELECT event_id, COUNT(*) AS hit, group_concat(token, char(31)) AS toks
            FROM kw_index
            WHERE {' AND '.join(cond)}
            GROUP BY event_id
            ORDER BY hit DESC, event_id DESC
            LIMIT ?
        """
        params.append(cap)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [(int(r["event_id"]), int(r["hit"]),
                 [t for t in (r["toks"] or "").split("\x1f") if t]) for r in rows]

    def _kw_fetch(
        self,
        session: str,
        hits: dict[int, tuple[int, list[str]]],
        limit: int,
        include_historical: bool,
    ) -> list[tuple[Event, int, list[str]]]:
        """按 id 取回事件行，过滤 session / 归档 / 取代，再排序截断。

        **session 条件放在 Python 里过滤，不写进 SQL。** 写进 SQL 的话，
        查询计划器会选 `idx_events_session_tier` 按 session 扫全表
        （8000 行），而不是按主键查这 160 行——实测 6.34ms vs 0.4ms。
        它选错是因为 `id IN (...) AND session = ?` 两个条件它估计前者更贵。
        用 `INDEXED BY sqlite_autoindex_events_1` 也能逼它，但那依赖内部索引名，
        升级 SQLite 就可能失效；Python 过滤是行为确定的。
        """
        ids = list(hits)
        cond = [f"id IN ({','.join('?' * len(ids))})", "archived = 0"]
        params: list[Any] = [*ids]
        if not include_historical:
            cond.append("superseded_by IS NULL")
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(cond)}", tuple(params)).fetchall()

        out = []
        for r in rows:
            ev = Event.from_row(r)
            if ev.session != session:
                continue
            hit, toks = hits[ev.id]
            out.append((ev, hit, toks))
        out.sort(key=lambda x: (-x[1], -x[0].weight, -x[0].event_time, -x[0].id))
        return out[:limit]

    def l1_filter(
        self,
        session: str,
        *,
        entity_ids: Sequence[int] | None = None,
        include_historical: bool = False,
        time_start: int | None = None,
        time_end: int | None = None,
        exclude_kinds: Sequence[str] = (),
        cap: int = 800,
    ) -> tuple[list[int], bool]:
        """检索 L1 分类层（M9）。返回 (event_id 列表, 是否被截断)。

        三条条件，缺一不可：
          1. `superseded_by IS NULL` —— 只给"还没被取代"的。历史值靠
             `include_historical` 放开，由查询里的"以前/原来"触发，不是常开。
          2. 实体 —— 走 event_entities。`entity_ids=None` 表示"没提到任何人"，
             此时**不加实体条件**（返回全库会话），而不是返回空集：
             用户问"我最近怎么样"时没有实体，不代表没有记忆。
          3. 时间 —— 区间事件用**重叠**判断，不是包含：
             `[event_time, event_time_end)` 与 `[t_start, t_end)` 有交集即命中。
             用 `event_time >= t_start` 会让"去年冬天搬走"（区间事件）在
             查"去年"时漏掉——它的起点在 12 月，而查询起点也是 12 月，边界相等
             但语义上是同一段时间。

        `MAX(...)` 那层是为了零长区间：`event_time_end == event_time` 时
        直接比较会得到空区间，任何查询都匹配不上。
        """
        cond = ["e.session = ?", "e.archived = 0"]
        params: list[Any] = [session]

        if not include_historical:
            cond.append("e.superseded_by IS NULL")

        if exclude_kinds:
            marks = ",".join("?" * len(exclude_kinds))
            cond.append(f"e.kind NOT IN ({marks})")
            params.extend(exclude_kinds)

        if entity_ids:
            marks = ",".join("?" * len(entity_ids))
            cond.append(
                f"e.id IN (SELECT event_id FROM event_entities "
                f"WHERE entity_id IN ({marks}))")
            params.extend(int(e) for e in entity_ids)

        if time_start is not None:
            cond.append("MAX(COALESCE(e.event_time_end, e.event_time + 1), "
                        "e.event_time + 1) > ?")
            params.append(int(time_start))
        if time_end is not None:
            cond.append("e.event_time < ?")
            params.append(int(time_end))

        sql = f"""
            SELECT e.id FROM events e
            WHERE {' AND '.join(cond)}
            ORDER BY e.weight DESC, e.event_time DESC, e.id DESC
            LIMIT ?
        """
        params.append(int(cap) + 1)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        ids = [int(r["id"]) for r in rows]
        if len(ids) > cap:
            return ids[:cap], True
        return ids, False

    def events_of_entity(self, session: str, entity_id: int, limit: int = 200) -> list[int]:
        """某个实体参与过的全部事件，按权重降序。

        002 之前的写法是 `entities LIKE '%3%'`，实体 3 会命中 13/23/30。
        改成走 event_entities：多一次 JOIN，换掉一整类"记忆挂错人"的事故。
        """
        rows = self.conn.execute(
            """
            SELECT e.id FROM event_entities ee JOIN events e ON e.id = ee.event_id
            WHERE ee.entity_id = ? AND e.session = ?
              AND e.archived = 0 AND e.superseded_by IS NULL
            ORDER BY e.weight DESC, e.event_time DESC, e.id DESC
            LIMIT ?
            """,
            (entity_id, session, limit),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def link_entities(self, event_id: int, entity_ids: Iterable[int]) -> int:
        """补挂实体。已有实体要在事件落库后才知道 id 时用这条路。"""
        pairs = [(event_id, int(e)) for e in entity_ids]
        if not pairs:
            return 0
        with self.tx():
            self.conn.executemany(
                "INSERT OR IGNORE INTO event_entities(event_id, entity_id) VALUES (?,?)",
                pairs,
            )
        return len(pairs)

    def entities_of_event(self, event_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT entity_id FROM event_entities WHERE event_id = ? ORDER BY entity_id",
            (event_id,),
        ).fetchall()
        return [int(r["entity_id"]) for r in rows]

    def supersede_event(self, old_id: int, new_id: int) -> bool:
        """把旧事件标成"已被取代"（M9.2 用；M13 摘要迭代也会用）。

        不删旧事件是刻意的：删了之后"我原来住北京"就永久消失，
        而用户问"我以前住哪"时恰恰需要它。`superseded_by` 是软删除，
        检索默认看不见（L1 的 `superseded_by IS NULL`），历史查询能看见。

        自指（old==new）直接拒绝：那会让事件从默认检索里凭空消失，
        而它并没有被任何东西取代。
        """
        if int(old_id) == int(new_id):
            raise StoreError("事件不能取代自己")
        with self.tx():
            cur = self.conn.execute(
                "UPDATE events SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
                (int(new_id), int(old_id)),
            )
        return cur.rowcount > 0

    def touch_events(self, ids: Sequence[int], when: int | None = None) -> int:
        """被注入上下文后调用：recall_count += 1、last_access 刷新（M14.3）。"""
        if not ids:
            return 0
        ts = when if when is not None else _now()
        marks = ",".join("?" * len(ids))
        with self.tx():
            cur = self.conn.execute(
                f"UPDATE events SET recall_count = recall_count + 1, last_access = ? "
                f"WHERE id IN ({marks})",
                (ts, *ids),
            )
        return cur.rowcount

    def delete_event(self, event_id: int) -> bool:
        """物理删除。**只给测试和维护用**，正常业务走 archived / superseded_by。

        数据库层有触发器拦 DELETE，这里临时把开关打开。开关和删除在同一个
        事务里，所以删除失败时开关不会残留——否则库会长期处于"可以删"的状态，
        等于铁律失效而没人知道。
        """
        with self.tx():
            self.conn.execute(
                "INSERT OR REPLACE INTO maint_flags(key, value) VALUES ('allow_event_delete','1')")
            cur = self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self.conn.execute("DELETE FROM maint_flags WHERE key = 'allow_event_delete'")
        return cur.rowcount > 0

    def fts_search(self, query: str, limit: int = 20) -> list[tuple[int, float]]:
        """摘要模糊检索（trigram，仅 ≥3 字符有效）。"""
        rows = self.conn.execute(
            "SELECT rowid, bm25(events_fts) AS score FROM events_fts "
            "WHERE events_fts MATCH ? ORDER BY score LIMIT ?",
            (query, limit),
        ).fetchall()
        return [(int(r["rowid"]), float(r["score"])) for r in rows]

    # ------------------------------------------------------------ M15.3 归档
    def archive_candidates(self, *, max_weight: float = 0.05,
                           idle_days: int = 365, now: int | None = None,
                           limit: int = 0) -> list[int]:
        """够格归档的事件：cold + 权重极低 + 很久没被想起。

        三个条件缺一不可：
          - `tier = 3`（cold）：还在 warm 里的东西随时可能被问起
          - `weight < 0.05`：衰减到几乎没有召回价值
          - `last_access` 超过一年：**没被召回过的才算**。注意 `last_access`
            为 NULL 时用 `ingest_time` 兜底——从没人碰过不等于"很久没碰"，
            它得从写入那天开始算。

        只归档 `kind='event'`：fact / plan 是结构化记忆，删了没法重建。
        """
        ts = now if now is not None else _now()
        cutoff = int(ts) - int(idle_days) * 86400
        sql = ("SELECT id FROM events WHERE tier = 3 AND weight < ?"
               " AND kind = 'event' AND archived = 0"
               " AND coalesce(last_access, ingest_time) < ?"
               " ORDER BY coalesce(last_access, ingest_time), id")
        args: list[Any] = [float(max_weight), cutoff]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [int(r["id"]) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def archive_to(self, ids: Sequence[int], archive_path: str | Path, *,
                   now: int | None = None, vacuum: bool = True) -> dict[str, Any]:
        """把事件搬到归档库，再从主库删掉。

        ## 为什么是"搬家"而不是"再存一份"

        手册要求"归档后主库变小 ≥20%"。复制一份再标 `archived=1` 的话，
        主库一个字节都不会少——`raw` 还在那儿，而 raw 恰恰是体积的大头。

        ## 为什么敢物理删除

        因为**先写后删，且写完要验数**。整个动作的顺序是：

            ATTACH → INSERT INTO arch.events → 核对 count → DELETE → DETACH

        任何一步失败都抛异常，事务回滚，主库一行不少。唯一的风险窗口是
        "INSERT 成功但 DETACH 前崩溃"，那也只是归档库多一份副本，
        主库还完整（下一次归档会 INSERT OR IGNORE 跳过已存在的 id）。

        ## 和"append-only"的关系

        `delete_event` 的注释说正常业务不走物理删除。归档是例外，但它是
        **有记录、可还原的删除**：`archive_meta` 里记了每批的 id 和源库，
        `restore_from` 能把任何一条原样搬回来（含原 id）。
        区别在于"删了还在别处"，不是"删了就没了"。
        """
        ids = [int(x) for x in ids]
        if not ids:
            return {"moved": 0, "bytes_before": 0, "bytes_after": 0}
        p = Path(archive_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        before = self.db_bytes()
        alias = "arch_m15"
        self.conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(p),))
        try:
            # 归档库的表结构跟着主库走：`AS SELECT ... WHERE 0` 保留列名和
            # 顺序，丢掉约束和索引。归档库只按 id 查、只读，不需要约束；
            # 而**约束里的 `length(raw) > 0` 和不可变触发器如果不丢**，
            # restore 回去时会被自己的旧规则拦住。
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {alias}.events AS"
                f" SELECT * FROM main.events WHERE 0")
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {alias}.archive_meta ("
                f" id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,"
                f" source TEXT NOT NULL, n INTEGER NOT NULL, ids TEXT NOT NULL)")

            marks = ",".join("?" * len(ids))
            with self.tx():
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {alias}.events"
                    f" SELECT * FROM main.events WHERE id IN ({marks})", tuple(ids))
                got = [int(r[0]) for r in self.conn.execute(
                    f"SELECT id FROM {alias}.events WHERE id IN ({marks})",
                    tuple(ids)).fetchall()]
                missing = sorted(set(ids) - set(got))
                if missing:
                    # 不静默跳过：少一行就说明归档库写失败了，
                    # 这时候继续 DELETE 等于删掉唯一一份。
                    raise StoreError(f"归档校验失败，缺 {len(missing)} 条：{missing[:5]}")
                self.conn.execute(
                    f"INSERT INTO {alias}.archive_meta(ts, source, n, ids)"
                    f" VALUES (?,?,?,?)",
                    (now if now is not None else _now(), self.db_file(),
                     len(ids), json.dumps(ids)))
                self.conn.execute(
                    "INSERT OR REPLACE INTO maint_flags(key, value)"
                    " VALUES ('allow_event_delete','1')")
                self.conn.execute(
                    f"DELETE FROM events WHERE id IN ({marks})", tuple(ids))
                self.conn.execute(
                    "DELETE FROM maint_flags WHERE key = 'allow_event_delete'")
        finally:
            self.conn.execute(f"DETACH DATABASE {alias}")

        after = self.db_bytes()
        if vacuum:
            # 归档是夜间批量操作，锁几秒没人看得见；不 VACUUM 的话
            # 用户看文件属性会发现"归档了一点没变小"，然后怀疑没生效。
            try:
                self.vacuum()
            except sqlite3.Error as exc:
                log.warning("store.vacuum_failed",
                            extra={"fields": {"err": str(exc)}})
        log.info("store.archived", extra={"fields": {
            "n": len(ids), "before": before, "after": after}})
        return {"moved": len(ids), "bytes_before": before, "bytes_after": after,
                "file": str(p), "file_size": self.db_bytes(live=False)}

    def restore_from(self, ids: Sequence[int], archive_dir: str | Path) -> list[int]:
        """从归档库把事件原样搬回主库（含原 id）。返回真正恢复的 id。

        跨所有 `mem-*.db` 找，因为一条事件可能属于任何一批。
        用 `INSERT OR IGNORE`：主库已经有同 id 时跳过——那种情况只可能是
        "上次恢复过一半"，重试不该报错。
        """
        want = {int(x) for x in ids}
        if not want:
            return []
        d = Path(archive_dir)
        if not d.exists():
            return []
        got: list[int] = []
        for f in sorted(d.glob("mem-*.db")):
            alias = "arch_r"
            self.conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(f),))
            try:
                exists = self.conn.execute(
                    f"SELECT name FROM {alias}.sqlite_master"
                    f" WHERE type='table' AND name='events'").fetchone()
                if not exists:
                    continue
                marks = ",".join("?" * len(want))
                rows = self.conn.execute(
                    f"SELECT * FROM {alias}.events WHERE id IN ({marks})",
                    tuple(sorted(want))).fetchall()
                if not rows:
                    continue
                cols = list(rows[0].keys())
                collist = ",".join(cols)
                marks2 = ",".join("?" * len(cols))
                with self.tx():
                    for r in rows:
                        self.conn.execute(
                            f"INSERT OR IGNORE INTO events({collist})"
                            f" VALUES ({marks2})", tuple(r[c] for c in cols))
                        eid = int(r["id"])
                        got.append(eid)
                        # 倒排必须一起补回来，否则"恢复了但搜不到"。
                        toks = [t.lower() for t in (r["keywords"] or "").split() if t]
                        if toks:
                            self.conn.executemany(
                                "INSERT OR IGNORE INTO kw_index(token, event_id)"
                                " VALUES (?,?)", [(t, eid) for t in toks])
                    self.conn.execute(
                        f"DELETE FROM {alias}.events WHERE id IN ({marks})",
                        tuple(sorted(want)))
            finally:
                self.conn.execute(f"DETACH DATABASE {alias}")
        return got

    def search_archive(self, query: str, *, limit: int = 5,
                       archive_dir: str | Path | None = None) -> list[dict[str, Any]]:
        """在归档库里按字面找（手册 M15.3 验收 #2「归档事件仍可检索」）。

        ## 为什么不在主检索路径上

        归档库是**冷**的：一年没被碰过、权重 <0.05。每轮对话都 ATTACH 一批
        归档库去查，等于让 99% 的查询为 1% 的可能性付磁盘 IO。所以这里
        做成显式询问（`hippo recall --deep`），默认路径一个字节都不读。

        ## 为什么用 LIKE 而不是倒排

        归档库没有 kw_index（`CREATE TABLE ... AS SELECT` 会丢索引，见
        `archive_to`），而且它是"死档案"：几万行以内全表扫一遍是毫秒级，
        为此维护一套倒排不划算。真相是——**归档库的价值在于"还能找回来"，
        不在于"每次都参与排序"**。

        返回按时间倒序，每条带上 `archived_id` 和所在文件，方便接着
        `hippo restore --id`。
        """
        q = (query or "").strip()
        if not q:
            return []
        d = Path(archive_dir) if archive_dir else Path(self.db_file()).parent / "archive"
        if not d.exists():
            return []
        # 关键词占位符：用最朴素的"任一空格分词命中即算"，和归档库的无索引
        # 现实匹配。整句子串匹配在这里不可用——用户问"我搬家的事"，
        # 归档里写的是"从朝阳搬到海淀"，一个字都不重合。
        toks = [t for t in q.replace("，", " ").replace("。", " ").split() if t]
        if not toks:
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(d.glob("mem-*.db")):
            alias = "arch_q"
            try:
                self.conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(f),))
            except sqlite3.Error:
                continue
            try:
                exists = self.conn.execute(
                    f"SELECT name FROM {alias}.sqlite_master"
                    f" WHERE type='table' AND name='events'").fetchone()
                if not exists:
                    continue
                cond = " OR ".join(
                    ["raw LIKE ? OR summary LIKE ? OR keywords LIKE ?"] * len(toks))
                params: list[Any] = []
                for t in toks:
                    params += [f"%{t}%"] * 3
                rows = self.conn.execute(
                    f"SELECT id, event_time, raw, summary, keywords"
                    f" FROM {alias}.events WHERE {cond}"
                    f" ORDER BY event_time DESC LIMIT ?",
                    (*params, limit)).fetchall()
                for r in rows:
                    out.append({
                        "archived_id": int(r["id"]),
                        "event_time": int(r["event_time"] or 0),
                        "raw": r["raw"] or "",
                        "summary": r["summary"] or "",
                        "keywords": r["keywords"] or "",
                        "file": str(f),
                    })
            finally:
                self.conn.execute(f"DETACH DATABASE {alias}")
            if len(out) >= limit:
                break
        out.sort(key=lambda x: x["event_time"], reverse=True)
        return out[:limit]

    def db_file(self) -> str:
        """主库的磁盘路径。

        不存成实例属性：`Store(conn)` 的构造路径有很多条（测试、归档还原、
        迁移），任何一条忘了传路径都会变成 `str(None)`，而它只在写归档日志时
        才被用到——**错得很晚才暴露**。现查 `database_list` 不会有这个问题。
        """
        for r in self.conn.execute("PRAGMA database_list").fetchall():
            if r[1] == "main":
                return str(r[2] or "")
        return ""

    def page_stats(self) -> dict[str, int]:
        """页统计。`db_bytes` 两种口径的原始数据，也是 `stats --disk` 的唯一来源。

        为什么单独开一个方法而不是让 cli 自己查 PRAGMA：本项目的规矩是
        **SQL 只有 store.py 一个出口**（`test_no_direct_db_access_outside_dao`）。
        这条规矩在 CLI 上破过一次（`_print_disk` 直接查 PRAGMA），被门禁测试
        当场抓住。改代码不改测试——门禁存在的意义就是它会挡你。
        """
        pc = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        ps = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        fl = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        return {"pages": pc, "page_size": ps, "freelist": fl,
                "live": max(0, pc - fl) * ps, "total": pc * ps}

    def db_bytes(self, *, live: bool = True) -> int:
        """主库占用的字节数。

        `live=True` 返回**活数据**大小：(page_count - freelist_count) × page_size。
        `live=False` 返回文件大小。

        为什么默认用活数据：SQLite 删行不会缩文件，被删的页进 freelist 等着
        复用。删掉一半数据后 `os.path.getsize` 一个字节都不会少，
        "归档后主库变小 ≥20%" 这条验收会永远显示 0%——而**那不代表没删**。
        两个数都要能拿到：活数据量回答"删掉了多少"，文件大小回答
        "什么时候该 VACUUM"。
        """
        st = self.page_stats()
        # 不用 `os.path.getsize`：VACUUM 之后紧接着调它，拿到的是旧的
        # st_size（实测 432KB vs 真实 220KB），于是"归档后变小了多少"
        # 这个数会一直是 0。页数 × 页大小就是文件大小的定义
        # （SQLite 文件除 100 字节头之外全是页）。
        return st["live"] if live else st["total"]

    def vacuum(self) -> None:
        """真正把文件缩小。**不能在事务里跑**，而且会锁库几秒。

        只在归档后调用：日常每轮对话都 VACUUM 会把代理卡住，
        而归档是夜间批量操作，锁几秒没人看得见。
        """
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("VACUUM")

    def backup_to(self, target: str | Path) -> Path:
        """把整个库一致快照到 target（M28.3）。

        为什么不用 `shutil.copy`：WAL 模式下直接拷文件会拷到"半个事务"，
        备份看着能用、恢复后却缺最后几轮。`VACUUM INTO` 在一个读事务里
        生成一致快照，是 SQLite 官方的在线备份方式。

        `VACUUM INTO` 不支持参数绑定，只能拼字符串；这里转义单引号，
        防止路径里的 `'` 把语句截断（Windows 用户名里带单引号很少见，
        但备份路径最终由用户给，不赌）。
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.conn.in_transaction:
            self.conn.commit()
        quoted = str(target).replace("'", "''")
        self.conn.execute(f"VACUUM INTO '{quoted}'")
        log.info("store.backup_to", extra={"fields": {"target": str(target)}})
        return target

    # ------------------------------------------------------------------ entities
    def upsert_entity(
        self,
        *,
        session: str,
        canonical: str,
        type: str = "person",
        aliases: Sequence[str] | None = None,
        confidence: float = 0.5,
    ) -> int:
        now = _now()
        row = self.conn.execute(
            "SELECT id, aliases FROM entities WHERE session = ? AND canonical = ? AND type = ?",
            (session, canonical, type),
        ).fetchone()
        if row:
            merged = sorted(set(json.loads(row["aliases"] or "[]")) | set(aliases or []))
            self.conn.execute(
                "UPDATE entities SET aliases = ?, last_seen = ?, "
                "confidence = max(confidence, ?) WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), now, confidence, row["id"]),
            )
            return int(row["id"])
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO entities(session, canonical, aliases, type, confidence,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                (
                    session, canonical,
                    # INSERT 分支也必须去重：UPDATE 分支用了 set，这里没有，
                    # 于是"同一个别名进来两次"只在第一次写库时留下重复项。
                    json.dumps(sorted(set(aliases or [])), ensure_ascii=False),
                    type, confidence, now, now,
                ),
            )
        return int(cur.lastrowid)

    def get_entity(self, entity_id: int) -> Entity | None:
        row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return Entity.from_row(row) if row else None

    def entity_id_of(self, session: str, canonical: str, type: str | None = None) -> int | None:
        """按正名查 id。写之前用它判断"是新建还是命中已有"。

        type 为 None 时只返回未合并的那一条：同一个名字在不同 type 下
        是不同实体（"朝阳"是地名也是人名），调用方不确定类型时不该瞎猜。
        """
        sql = ("SELECT id FROM entities WHERE session = ? AND canonical = ? "
               "AND merged_into IS NULL")
        params: list[Any] = [session, canonical]
        if type is not None:
            sql += " AND type = ?"
            params.append(type)
        row = self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return int(row["id"]) if row else None

    def list_entities(self, session: str) -> list[Entity]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE session = ? AND merged_into IS NULL ORDER BY id",
            (session,),
        ).fetchall()
        return [Entity.from_row(r) for r in rows]

    def find_entity(self, session: str, name: str) -> Entity | None:
        """按名字或别名找人。`hippo traits --entity 王哥` 要能找到"老王"。"""
        from .entities import normalize_name

        n = normalize_name(name)
        if not n:
            return None
        ents = self.list_entities(session)
        for e in ents:                       # 先正名
            if normalize_name(e.canonical) == n:
                return e
        for e in ents:                       # 再别名
            if any(normalize_name(a) == n for a in e.aliases):
                return e
        return None

    def resolve_entity(
        self,
        *,
        session: str,
        name: str,
        type: str = "person",
        aliases: Sequence[str] | None = None,
        confidence: float = 0.7,
        doubt_ttl_days: int = 30,
    ) -> Any:
        """写入前解析实体（M7.1）。返回 merge / doubt / new。

        **命中就返回已有 id，不新建。** 新建永远是兜底，不是默认动作。
        """
        from .entities import AUTO_MERGE, SUSPECT, Match, decide, normalize_name

        canonical = normalize_name(name)
        if not canonical:
            raise StoreError("实体名为空")

        rows = self.conn.execute(
            "SELECT id, canonical, type, aliases FROM entities "
            "WHERE session = ? AND merged_into IS NULL",
            (session,),
        ).fetchall()
        existing = [(int(r["id"]), r["canonical"], r["type"],
                     json.loads(r["aliases"] or "[]")) for r in rows]

        mt = decide(canonical, type, existing, aliases=list(aliases or []))

        if mt.action == AUTO_MERGE and mt.target_id is not None:
            eid = self.upsert_entity(
                session=session, canonical=self.get_entity(mt.target_id).canonical,
                type=type, aliases=[canonical, *(aliases or [])], confidence=confidence,
            )
            return Match(AUTO_MERGE, eid, mt.score, mt.reason)

        eid = self.upsert_entity(
            session=session, canonical=canonical, type=type,
            aliases=list(aliases or []), confidence=confidence,
        )

        if mt.action == SUSPECT and mt.target_id is not None:
            other = self.get_entity(mt.target_id)
            if other is not None:
                q = f"「{canonical}」和「{other.canonical}」是同一个人吗？"
                # 同一个疑问只问一次。会话聊三小时，"张三/张珊"会被反复提到，
                # 每次都插一条 doubt 会让待澄清列表变成刷屏。
                dup = self.conn.execute(
                    "SELECT id FROM doubts WHERE session = ? AND question = ? AND resolved = 0",
                    (session, q),
                ).fetchone()
                if not dup:
                    self.add_doubt(
                        session=session, question=q,
                        context=f"实体归一：相似度 {mt.score:.2f}（{mt.reason}）",
                        confidence=0.3, cost=0.2, impact=0.5,
                        expire_days=doubt_ttl_days,
                    )
            return Match(SUSPECT, eid, mt.score, mt.reason)

        return Match("new", eid, mt.score, mt.reason)

    def merge_entities(self, src_id: int, dst_id: int) -> dict[str, int]:
        """把 src 并进 dst。**不可逆**，只给 `hippo merge` 与人工确认后调用。

        自动归一（resolve_entity）永远不会走到这里：它只做"命中已有"，
        不做"把两条已存在的记录缝起来"。缝错了没法拆。
        """
        if src_id == dst_id:
            raise StoreError("不能和自己合并")
        src, dst = self.get_entity(src_id), self.get_entity(dst_id)
        if src is None or dst is None:
            raise StoreError("实体不存在")
        if src.session != dst.session:
            raise StoreError("跨 session 合并会让记忆串号，拒绝")
        if src.type != dst.type:
            raise StoreError(f"类型不同不能合并：{src.type} vs {dst.type}")
        if src.merged_into:
            raise StoreError(f"#{src_id} 已并入 #{src.merged_into}")

        stats = {"traits": 0, "traits_merged": 0, "facts": 0, "events": 0}
        with self.tx():
            # 1) 事件缓存的 JSON 列先改，触发器会据此重建 event_entities
            ev_rows = self.conn.execute(
                "SELECT e.id, e.entities FROM events e JOIN event_entities ee "
                "ON ee.event_id = e.id WHERE ee.entity_id = ?", (src_id,)
            ).fetchall()
            for r in ev_rows:
                try:
                    ids = json.loads(r["entities"] or "[]")
                except ValueError:
                    continue
                new = sorted({int(x) for x in ids if int(x) != src_id} | {dst_id})
                self.conn.execute(
                    "UPDATE events SET entities = ? WHERE id = ?",
                    (json.dumps(new), r["id"]),
                )
                stats["events"] += 1
            # 2) 兜底：触发器已按新 JSON 重建，这里清掉可能残留的 src 关联
            self.conn.execute("DELETE FROM event_entities WHERE entity_id = ?", (src_id,))

            # 3) traits：同槽位（或同文案）的并证据，其余直接迁
            #
            # 判重要按 **slot** 而不是 trait 文案：007 的唯一索引是
            # `(entity_id, slot) WHERE valid_to IS NULL AND slot <> ''`。
            # 两个"老王"的槽位都是 `job`，但一个写"在字节做后端"、
            # 一个写"后端工程师（字节）"——按文案判重会漏，然后在 UPDATE
            # entity_id 时撞索引，整个合并失败。
            for t in self.list_traits(src_id, active_only=False):
                if t.valid_to is None and t.slot:
                    dup = self.conn.execute(
                        "SELECT id, trait, evidence, confidence FROM card_traits "
                        "WHERE entity_id = ? AND slot = ? AND valid_to IS NULL",
                        (dst_id, t.slot),
                    ).fetchone()
                elif t.valid_to is None:
                    dup = self.conn.execute(
                        "SELECT id, trait, evidence, confidence FROM card_traits "
                        "WHERE entity_id = ? AND trait = ? AND valid_to IS NULL",
                        (dst_id, t.trait),
                    ).fetchone()
                else:
                    dup = None

                if dup:
                    merged = sorted(set(json.loads(dup["evidence"] or "[]")) | set(t.evidence))
                    # 文案留置信度高的那条。两个实体的说法都对，
                    # 但用户只看得见一条——留证据更多、置信更高的那个更安全。
                    trait_text = (t.trait if t.confidence > float(dup["confidence"])
                                  else dup["trait"])
                    self.conn.execute(
                        "UPDATE card_traits SET trait = ?, evidence = ?, "
                        "confidence = min(0.95, ?), updated_at = ? WHERE id = ?",
                        (trait_text, json.dumps(merged),
                         max(float(dup["confidence"]), t.confidence) + 0.1,
                         _now(), dup["id"]),
                    )
                    self.conn.execute("DELETE FROM card_traits WHERE id = ?", (t.id,))
                    stats["traits_merged"] += 1
                else:
                    self.conn.execute(
                        "UPDATE card_traits SET entity_id = ? WHERE id = ?", (dst_id, t.id)
                    )
                    stats["traits"] += 1

            # 4) facts
            # 008 加的 idx_facts_single_active 约束 (session, entity_id, key)
            # WHERE valid_to IS NULL AND card='single'。直接把 src 的 facts
            # 改 entity_id 会撞它：两个"老王"各自有一条 employer active，
            # 合并后就成了同一实体的两条 active——**建索引时没想到合并这条路**。
            #
            # 处理方式与 M8 的"改口"一致：谁更新谁留下，旧的按 valid_to 收尾，
            # 不删。删了的话"我以前在腾讯"就永久消失了，而那正是历史查询要的。
            # multi 类（爱好、宠物这种清单）本来就允许多条，不参与。
            src_active = self.conn.execute(
                "SELECT id, key, card, valid_from FROM facts "
                "WHERE entity_id = ? AND valid_to IS NULL", (src_id,)).fetchall()
            for f in src_active:
                if f["card"] != "single":
                    continue
                dup = self.conn.execute(
                    "SELECT id, valid_from FROM facts WHERE entity_id = ? AND key = ? "
                    "AND card = 'single' AND valid_to IS NULL", (dst_id, f["key"])
                ).fetchone()
                if dup is None:
                    continue
                src_from = f["valid_from"] or 0
                dst_from = dup["valid_from"] or 0
                keep, drop = ((f["id"], dup["id"]) if src_from > dst_from
                              else (dup["id"], f["id"]))
                keep_from = self.conn.execute(
                    "SELECT valid_from FROM facts WHERE id = ?", (keep,)
                ).fetchone()["valid_from"]
                self.conn.execute(
                    "UPDATE facts SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                    (keep_from or _now(), drop),
                )

            cur = self.conn.execute(
                "UPDATE facts SET entity_id = ? WHERE entity_id = ?", (dst_id, src_id)
            )
            stats["facts"] = cur.rowcount

            # 5) 源实体留痕：canonical 加不可见后缀腾出唯一键，
            #    否则以后出现同一个名字时 INSERT 会撞 UNIQUE(session, canonical, type)。
            aliases = sorted(set(dst.aliases) | set(src.aliases) | {src.canonical})
            self.conn.execute(
                "UPDATE entities SET merged_into = ?, canonical = ?, aliases = ?, "
                "last_seen = ? WHERE id = ?",
                (dst_id, f"{src.canonical}\x1f{src_id}",
                 json.dumps(aliases, ensure_ascii=False), _now(), src_id),
            )
            self.conn.execute(
                "UPDATE entities SET aliases = ? WHERE id = ?",
                (json.dumps(aliases, ensure_ascii=False), dst_id),
            )
        return stats

    # ------------------------------------------------------------------ card_traits
    def add_trait(
        self,
        *,
        entity_id: int,
        trait: str,
        evidence: Sequence[int],
        status: str = "observed",
        confidence: float = 0.5,
        valid_from: int | None = None,
        slot: str = "",
    ) -> int:
        """写一条角色卡特征。**没有证据的一律拒绝**（M7.2）。

        slot 是"这条特征在回答哪个问题"。同一实体同一槽位只保留一条 active：
        用户换工作后，"在字节" 和 "在创业公司" 不能同时成立。
        slot 为空表示不占槽——那些说不清槽位的特征（"话少但心细"）
        不该互相覆盖。
        """
        if not evidence:
            raise NoEvidenceError(f"trait「{trait}」没有证据事件，拒绝落库")
        slot = (slot or "").strip().lower()
        now = _now()
        with self.tx():
            dup = self.conn.execute(
                "SELECT id, confidence, evidence FROM card_traits "
                "WHERE entity_id = ? AND trait = ? AND valid_to IS NULL",
                (entity_id, trait),
            ).fetchone()
            if dup:
                merged = sorted(set(json.loads(dup["evidence"] or "[]")) | set(evidence))
                self.conn.execute(
                    "UPDATE card_traits SET evidence = ?, confidence = min(0.95, ?), "
                    "updated_at = ? WHERE id = ?",
                    (json.dumps(merged), dup["confidence"] + 0.1, now, dup["id"]),
                )
                return int(dup["id"])
            if slot:
                # 旧条不删，只关窗。三个月后问"他以前在哪工作"还要查得到。
                self.conn.execute(
                    "UPDATE card_traits SET valid_to = ?, updated_at = ? "
                    "WHERE entity_id = ? AND slot = ? AND valid_to IS NULL",
                    (valid_from or now, now, entity_id, slot),
                )
            cur = self.conn.execute(
                "INSERT INTO card_traits(entity_id, trait, status, evidence, confidence,"
                " valid_from, updated_at, slot) VALUES (?,?,?,?,?,?,?,?)",
                (
                    entity_id, trait, status,
                    json.dumps(list(evidence)), confidence, valid_from or now, now, slot,
                ),
            )
        return int(cur.lastrowid)

    def list_traits(self, entity_id: int, *, active_only: bool = True) -> list[CardTrait]:
        sql = "SELECT * FROM card_traits WHERE entity_id = ?"
        if active_only:
            sql += " AND valid_to IS NULL"
        rows = self.conn.execute(sql + " ORDER BY id", (entity_id,)).fetchall()
        return [CardTrait.from_row(r) for r in rows]

    def close_trait(self, trait_id: int, when: int | None = None) -> bool:
        with self.tx():
            cur = self.conn.execute(
                "UPDATE card_traits SET valid_to = ?, updated_at = ? "
                "WHERE id = ? AND valid_to IS NULL",
                (when or _now(), _now(), trait_id),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------ facts
    def set_fact(
        self,
        *,
        session: str,
        key: str,
        value: str,
        entity_id: int | None = None,
        source_event: int | None = None,
        confidence: float = 0.6,
        when: int | None = None,
        label: str = "",
    ) -> "FactWrite":
        """写一条事实。返回 FactWrite（id / action / 判定理由）。

        M8 之前这个方法直接比字符串：值不同就判矛盾。于是
        `上海` / `上海市`、`字节` / `字节跳动` 每次都是一场"你搬家了？"。
        现在判定挪进 `facts.decide_fact`（纯函数，可单独测），
        这里只负责执行它给出的六种结局。
        """
        from .facts import Decision  # 避免循环导入时的类型解析开销

        table = fact_keys()
        canon = table.canonical(key)
        if canon is None:
            # 表外 key 一律不写。放行等于承认 key 可以无限长，
            # 而 key 一旦发散，"住哪"就会被拆成十个槽，每个都只有一条。
            log.info("fact.rejected_key", extra={"fields": {
                "key": str(key)[:24], "value": str(value)[:40]}})
            return FactWrite(None, "reject", Decision("reject", reason=f"key 不在词表：{key}"))

        k = table.get(canon)
        assert k is not None
        ts = when or _now()

        rows = self.conn.execute(
            "SELECT id, value FROM facts WHERE session = ? AND key = ? "
            "AND valid_to IS NULL AND entity_id IS ?",
            (session, canon, entity_id),
        ).fetchall()
        existing = [(int(r["id"]), str(r["value"])) for r in rows]

        if not label:
            label = "用户"
            if entity_id is not None:
                er = self.conn.execute(
                    "SELECT canonical FROM entities WHERE id = ?", (entity_id,)
                ).fetchone()
                if er:
                    label = str(er["canonical"])

        d = decide_fact(k, existing, value, confidence, label=label)

        with self.tx():
            if d.action == "reject":
                return FactWrite(None, "reject", d)

            if d.action == "doubt":
                # 不确定就不动 facts。问一句比悄悄改掉安全：
                # 改错了用户不会发现，问一句用户会纠正。
                self.add_doubt(session=session, question=d.question,
                               context=f"fact:{canon}", confidence=confidence)
                return FactWrite(None, "doubt", d)

            if d.action in ("same", "refine") and d.keep_id is not None:
                self.conn.execute(
                    "UPDATE facts SET value = ?, confidence = max(confidence, ?), "
                    "source_event = coalesce(?, source_event) WHERE id = ?",
                    (d.value, confidence, source_event, d.keep_id),
                )
                return FactWrite(d.keep_id, d.action, d)

            for fid in d.close_ids:
                # 旧值的开始时间可能比新值的开始还晚：新值带了"上个月"这种
                # 回溯线索，旧值没有，只能按写入时间估。这时"北京"的区间会变成
                # [11-05, 10-01) —— 结束早于开始，任何按时间点的查询都查不到它，
                # 用户问"我以前住哪"会得到"不知道"。
                #
                # 真相是：我们**知道它什么时候结束，不知道它什么时候开始**。
                # 用 0 表示"早于所有记录"，区间变成 [0, 10-01)，问去年 12 月能答对。
                self.conn.execute(
                    "UPDATE facts SET valid_from = 0, valid_to = ? "
                    "WHERE id = ? AND valid_to IS NULL AND valid_from >= ?",
                    (ts, fid, ts),
                )
                self.conn.execute(
                    "UPDATE facts SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                    (ts, fid),
                )

            # valid_from 用**事件发生时间**而不是写入时间：
            # "去年住在北京"必须记成去年的状态，否则"你什么时候搬的家"
            # 会答成"今天"——而今天只是我们抽到它的日子。
            cur = self.conn.execute(
                "INSERT INTO facts(session, entity_id, key, key_raw, value, card,"
                " valid_from, valid_to, source_event, confidence)"
                " VALUES (?,?,?,?,?,?,?,NULL,?,?)",
                (session, entity_id, canon, str(key)[:40], d.value, k.card,
                 ts, source_event, confidence),
            )
            return FactWrite(int(cur.lastrowid), d.action, d)

    def get_fact(
        self, session: str, key: str, *, entity_id: int | None = None,
        include_historical: bool = False, any_entity: bool = False,
    ) -> list[Fact]:
        """查当前（或全部）事实。

        `any_entity=True` 时不按实体过滤。默认按 entity_id 过滤，
        包括 entity_id 为 NULL 的情况——"用户本人的城市"和"老王的城市"
        是两个不同的槽，混在一起查会得到两条都 active 的矛盾结果。
        """
        canon = fact_keys().canonical(key) or key
        sql = "SELECT * FROM facts WHERE session = ? AND key = ?"
        params: list[Any] = [session, canon]
        if not any_entity:
            sql += " AND entity_id IS ?"
            params.append(entity_id)
        if not include_historical:
            sql += " AND valid_to IS NULL"
        rows = self.conn.execute(sql + " ORDER BY valid_from DESC, id DESC", tuple(params)).fetchall()
        return [Fact.from_row(r) for r in rows]

    def list_facts(self, session: str, *, include_historical: bool = True
                   ) -> list[tuple[Fact, str]]:
        """列出一个会话的全部事实，附带所属实体名。

        CLI 要显示"谁的事实"，但 Fact 只有 entity_id。让 CLI 自己去 JOIN
        会破坏"SQL 只有一个出口"——那条规矩不是洁癖，是为了让
        "哪些地方在写 facts"永远只有一个答案。
        """
        sql = ("SELECT f.*, e.canonical AS who FROM facts f "
               "LEFT JOIN entities e ON e.id = f.entity_id WHERE f.session = ?")
        if not include_historical:
            sql += " AND f.valid_to IS NULL"
        rows = self.conn.execute(sql + " ORDER BY f.key, f.valid_from DESC",
                                 (session,)).fetchall()
        return [(Fact.from_row(r), str(r["who"] or "")) for r in rows]

    def fact_as_of(
        self, session: str, key: str, ts: int, *, entity_id: int | None = None,
    ) -> Fact | None:
        """ts 那一刻，这个槽的值是什么。

        "你以前住哪"、"搬家之前公司在哪"——这类问题只能靠时间点查，
        不能靠 current。区间是左闭右开：`valid_from <= ts < valid_to`，
        因为 valid_to 恰好等于新值的 valid_from，用闭区间会让两段重叠，
        同一个时间点查出两个答案。
        """
        canon = fact_keys().canonical(key) or key
        row = self.conn.execute(
            "SELECT * FROM facts WHERE session = ? AND key = ? AND entity_id IS ? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY valid_from DESC, id DESC LIMIT 1",
            (session, canon, entity_id, ts, ts),
        ).fetchone()
        return Fact.from_row(row) if row else None

    # ------------------------------------------------------------------ doubts
    def add_doubt(
        self,
        *,
        session: str,
        question: str,
        context: str = "",
        confidence: float = 0.5,
        cost: float = 0.5,
        impact: float = 0.5,
        expire_days: int = 7,
        now: int | None = None,
    ) -> int:
        now = now if now is not None else _now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO doubts(session, question, context, confidence, cost, impact,"
                " created, expires) VALUES (?,?,?,?,?,?,?,?)",
                (session, question, context, confidence, cost, impact,
                 now, now + expire_days * 86400),
            )
        return int(cur.lastrowid)

    def active_doubts(self, session: str, limit: int = 50) -> list[Doubt]:
        rows = self.conn.execute(
            "SELECT * FROM doubts WHERE session = ? AND resolved = 0 AND expires > ? "
            "ORDER BY impact DESC, id DESC LIMIT ?",
            (session, _now(), limit),
        ).fetchall()
        return [Doubt.from_row(r) for r in rows]

    def resolve_doubt(self, doubt_id: int) -> bool:
        with self.tx():
            cur = self.conn.execute(
                "UPDATE doubts SET resolved = 1 WHERE id = ?", (doubt_id,)
            )
        return cur.rowcount > 0

    def mark_doubt_asked(self, doubt_id: int, turn: int) -> None:
        self._assert_writable()
        self.conn.execute(
            "UPDATE doubts SET asked_count = asked_count + 1, last_asked_turn = ? "
            "WHERE id = ?",
            (turn, doubt_id),
        )

    # ---- 以下四个方法服务 M18 doubt.py（全生命周期），保持 DAO 边界：
    # 存疑的策略判断放在 doubt.py，这里只做 SQL。M16 修 CLI 直接查 PRAGMA
    # 时已经确认过一次：绕过 DAO 的写法在测试里能过，在 schema 变更时必炸。
    def bump_doubt_impact(self, doubt_id: int, impact: float) -> None:
        """合并时提升已有条目的 impact（疑问被再次触发表明它更值得问）。

        只升不降：新的候选 impact 更低时不覆盖——已有条目已经等了几天，
        它的 impact 是被时间检验过的，不能被一句新抽取冲淡。
        """
        self._assert_writable()
        impact = max(0.0, min(1.0, float(impact)))
        self.conn.execute(
            "UPDATE doubts SET impact = MAX(impact, ?) WHERE id = ?",
            (impact, doubt_id),
        )

    def prune_doubts(self, session: str, keep: int = 20) -> int:
        """活跃 doubts 超 keep 时，淘汰 impact 最低的。返回淘汰条数。

        为什么删而不是 resolve：resolve 的语义是"答案有了"（M18.3 会
        据此写 fact），淘汰是"池子满了你排最后"。两者不能混。
        """
        keep = max(0, int(keep))
        with self.tx():
            cur = self.conn.execute(
                "DELETE FROM doubts WHERE session = ? AND resolved = 0 AND id IN ("
                "  SELECT id FROM doubts WHERE session = ? AND resolved = 0"
                "  ORDER BY impact ASC, id ASC LIMIT MAX(("
                "    SELECT COUNT(*) FROM doubts WHERE session = ? AND resolved = 0"
                "  ) - ?, 0))",
                (session, session, session, keep),
            )
        return cur.rowcount

    def expired_doubts(self, *, session: str = "", now_ts: int | None = None,
                       limit: int = 100) -> list[Doubt]:
        """过期且未 resolve、未转换的 doubts（M18.3 扫描用）。

        resolved 的不返回：答案已经有了，转成"没搞清楚"是撒谎。
        """
        now = now_ts if now_ts is not None else _now()
        sql = ("SELECT * FROM doubts WHERE resolved = 0 AND converted = 0 "
               "AND expires <= ?{cond} ORDER BY expires ASC LIMIT ?")
        args: list = [now]
        if session:
            sql = sql.format(cond=" AND session = ? ")
            args.append(session)
        else:
            sql = sql.format(cond="")
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [Doubt.from_row(r) for r in rows]

    def mark_doubt_converted(self, doubt_id: int) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE doubts SET converted = 1 WHERE id = ?", (doubt_id,)
            )

    def recent_resolved_doubts(self, session: str, *,
                               days: int = 30) -> list[Doubt]:
        """最近 N 天内被 resolved 的 doubts（M18.1 防重复入库用）。

        为什么 filter 也要看已关闭的：问过且已回答的问题，换了个
        问法再次出现时不该变成新疑问——答案就在 facts 里，再问就是
        装傻。只回看 N 天，太老的关闭记录没有比对价值。
        """
        now = _now()
        rows = self.conn.execute(
            "SELECT * FROM doubts WHERE session = ? AND resolved = 1 "
            "AND expires > ? ORDER BY id DESC LIMIT 50",
            (session, now - days * 86400),
        ).fetchall()
        return [Doubt.from_row(r) for r in rows]

    # ------------------------------------------------------------------ plans
    def add_plan(
        self,
        *,
        session: str,
        due_at: int,
        content: str,
        raw_hint: str | None = None,
        recur: str | None = None,
    ) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO plans(session, due_at, content, raw_hint, recur, created)"
                " VALUES (?,?,?,?,?,?)",
                (session, due_at, content, raw_hint, recur, _now()),
            )
        return int(cur.lastrowid)

    def due_plans(self, now: int | None = None) -> list[Plan]:
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE fired = 0 AND due_at <= ? ORDER BY due_at",
            (now or _now(),),
        ).fetchall()
        return [Plan.from_row(r) for r in rows]

    def recent_fired_plans(self, session: str, *, minutes: int = 10,
                           now_ts: int | None = None) -> list[Plan]:
        """刚触发过的计划（M19.3 注入窗口）。

        为什么不是查 due_plans：scheduler tick 一到点就把 fired 置 1
        （并发安全的代价），pack 再查 due_plans 永远查不到。注入窗口
        用 fired_at：触发后 10 分钟内进过的 pack 都带上 [计划] 段——
        用户不一定秒回，窗口太窄的话模型根本没机会提。
        """
        now = now_ts if now_ts is not None else _now()
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE session = ? AND fired = 1 "
            "AND fired_at >= ? AND fired_at <= ? ORDER BY due_at",
            (session, now - minutes * 60, now),
        ).fetchall()
        return [Plan.from_row(r) for r in rows]

    def mark_plan_fired(self, plan_id: int, missed: bool = False,
                        now: int | None = None) -> bool:
        now = now if now is not None else _now()
        with self.tx():
            cur = self.conn.execute(
                "UPDATE plans SET fired = 1, fired_at = ?, missed = ? "
                "WHERE id = ? AND fired = 0",
                (now, 1 if missed else 0, plan_id),
            )
        return cur.rowcount > 0

    def sweep_missed_plans(self, *, grace_seconds: int = 300,
                           now: int | None = None) -> int:
        """重启补触发标记：到点很久没人管（fired=0 且超 grace）的标 missed=1。

        **不标 fired**——晚到的提醒还是要发，只是注入时要带上
        "这条晚了"的说明。grace 默认 5 分钟：到点后 5 分钟内触发算
        正常延迟，超过说明进程死过或积压过，必须认账。
        返回标记条数。
        """
        now = now if now is not None else _now()
        with self.tx():
            cur = self.conn.execute(
                "UPDATE plans SET missed = 1 "
                "WHERE fired = 0 AND due_at < ? AND missed = 0",
                (now - grace_seconds,),
            )
        return cur.rowcount

    def advance_plan(self, plan_id: int, next_due: int) -> None:
        """周期计划触发后推下一个 due_at（M19.1.4）。

        复用同一行而不是插新行：daily 计划一年 365 条文本几乎一样的
        行没有检索价值——计划的价值在"未来什么时候做什么"，不在
        "过去触发过几次"（attempts 计数够了）。fired 拨回 0 等待
        下一次到点；fired_at 保留上次触发时间，是最近一次的账。
        """
        with self.tx():
            self.conn.execute(
                "UPDATE plans SET fired = 0, fired_at = fired_at, "
                "due_at = ?, missed = 0 WHERE id = ?",
                (next_due, plan_id),
            )

    def defer_plan(self, plan_id: int, *, minutes: int = 10,
                   max_attempts: int = 3, now: int | None = None) -> bool:
        """模型判断"此刻不适合打扰"→ 延后 minutes 再试。

        attempts ≥ max_attempts 时拒绝：第 4 次必须强制发——延后
        无限次等于这条计划永远不执行，比打扰更违背承诺。
        """
        now = now if now is not None else _now()
        with self.tx():
            cur = self.conn.execute(
                "UPDATE plans SET fired = 0, due_at = ?, "
                "attempts = attempts + 1 "
                "WHERE id = ? AND fired = 1 AND attempts < ?",
                (now + minutes * 60, plan_id, max_attempts),
            )
        return cur.rowcount > 0

    # --------------------------------------------------------------- curiosity
    def add_curiosity(self, *, session: str, question: str, gap_kind: str,
                      gap_ref: str, impact: float, urgency: float,
                      novelty: float, priority: float,
                      now: int | None = None) -> int:
        """入队一条想问的话。**入队不等于说出去**（spoken_at 仍为 NULL）。

        分开记是必须的：tick 每 60 秒跑一次，静默时段和"用户正在聊"
        都会让队列里堆着问不出去的问题；把入队当发言的话，一晚上能记
        出 60 次，额度当场失效。
        """
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO curiosity(session, question, gap_kind, gap_ref,"
                " impact, urgency, novelty, priority, created, outcome)"
                " VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                (session, question, gap_kind, gap_ref, impact, urgency,
                 novelty, priority, now if now is not None else _now()),
            )
        return int(cur.lastrowid)

    def pending_curiosity(self, session: str, *, limit: int = 5,
                          max_age: int | None = None,
                          now: int | None = None) -> list[Curiosity]:
        """队列头部。`max_age` 过滤掉放太久的——三天前想问的一句
        "你搬家了吗"，今天突然冒出来只会让人莫名其妙。"""
        sql = ("SELECT * FROM curiosity WHERE session = ? AND outcome = 'pending'")
        args: list[Any] = [session]
        if max_age is not None:
            sql += " AND created >= ?"
            args.append((now if now is not None else _now()) - max_age)
        sql += " ORDER BY priority DESC, created ASC LIMIT ?"
        args.append(limit)
        return [Curiosity.from_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def next_curiosity(self, session: str, *, max_age: int | None = None,
                       now: int | None = None) -> Curiosity | None:
        """这一轮该说的那一条：队列里优先级最高且没放坏的。"""
        rows = self.pending_curiosity(session, limit=1, max_age=max_age, now=now)
        return rows[0] if rows else None

    def mark_curiosity_spoken(self, cid: int, now: int | None = None) -> bool:
        """说出口。只从 pending 迁到 spoken——重复调用不会二次计数，
        否则额度会被重试逻辑吃掉。"""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE curiosity SET spoken_at = ?, outcome = 'spoken'"
                " WHERE id = ? AND outcome = 'pending'",
                (now if now is not None else _now(), cid),
            )
        return cur.rowcount > 0

    def mark_curiosity_answered(self, cid: int, now: int | None = None) -> bool:
        """用户接了这个话头。只从 spoken 迁——排队中的问题用户根本没看见，
        把它标成"答过了"等于自己骗自己。"""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE curiosity SET answered_at = ?, outcome = 'answered'"
                " WHERE id = ? AND outcome = 'spoken'",
                (now if now is not None else _now(), cid),
            )
        return cur.rowcount > 0

    def sweep_curiosity_ignored(self, *, window: int = 24 * 3600,
                                now: int | None = None) -> int:
        """说出去超过 window 还没人接 → ignored。返回本次标记条数。

        判定放在读的时候做（tick / 打包前），不另设定时器：状态是
        "到现在为止没人接"，它只跟时间有关，不需要事件驱动。
        """
        now = now if now is not None else _now()
        with self.tx():
            cur = self.conn.execute(
                "UPDATE curiosity SET outcome = 'ignored'"
                " WHERE outcome = 'spoken' AND spoken_at < ?",
                (now - window,),
            )
        return int(cur.rowcount)

    def gave_up_curiosity(self, session: str, *, streak: int = 2,
                          within: int = 3 * 86400,
                          now: int | None = None) -> bool:
        """最近 streak 次都说了没人接 → 先别说了。

        只数"已经得出结论"的（answered / ignored），排队中的不算：
        拿 pending 去判断等于惩罚"还没找到机会开口"。

        `within` 是**恢复通道**：没有它，两次冷遇之后就永远闭嘴了——
        而它一旦不说话，也就不可能再被回应，冷遇计数永远下不来。
        三天后再试一次，是"读空气"和"别把自己憋死"的折中。
        """
        now = now if now is not None else _now()
        rows = self.conn.execute(
            "SELECT outcome FROM curiosity WHERE session = ?"
            " AND outcome IN ('answered', 'ignored') AND spoken_at >= ?"
            " ORDER BY spoken_at DESC LIMIT ?", (session, now - within, streak),
        ).fetchall()
        if len(rows) < streak:
            return False
        return all(str(r["outcome"]) == "ignored" for r in rows)

    def count_curiosity_spoken(self, *, session: str = "", since: int = 0,
                               until: int | None = None) -> int:
        sql = "SELECT count(*) FROM curiosity WHERE spoken_at IS NOT NULL AND spoken_at >= ?"
        args: list[Any] = [since]
        if session:
            sql += " AND session = ?"
            args.append(session)
        if until is not None:
            sql += " AND spoken_at < ?"
            args.append(until)
        return int(self.conn.execute(sql, args).fetchone()[0])

    def fact_active_counts(self, session: str) -> dict[str, int]:
        """每个 key 当前 active 的条数（M26 矛盾率用）。

        放在 DAO 而不是让评测直接 `conn.execute`：`test_m1_3_dao` 会扫
        tools/ 目录，而这条 SQL 是"业务口径"（active = valid_to IS NULL），
        散在评测脚本里迟早和 facts.decide_fact 的口径漂移。
        """
        rows = self.conn.execute(
            "SELECT key, count(*) AS n FROM facts"
            " WHERE session = ? AND valid_to IS NULL GROUP BY key",
            (session,)).fetchall()
        return {str(r["key"]): int(r["n"]) for r in rows}

    def count_curiosity(self) -> int:
        return int(self.conn.execute(
            "SELECT count(*) FROM curiosity").fetchone()[0])

    def curiosity_rows(self, session: str = "", *, limit: int = 50,
                       since: int = 0) -> list[Curiosity]:
        sql = "SELECT * FROM curiosity WHERE created >= ?"
        args: list[Any] = [since]
        if session:
            sql += " AND session = ?"
            args.append(session)
        sql += " ORDER BY coalesce(spoken_at, created) DESC LIMIT ?"
        args.append(limit)
        return [Curiosity.from_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def recent_curiosity_refs(self, session: str, *, since: int,
                              limit: int = 200) -> list[tuple[str, str, int]]:
        """(gap_ref, question, created) —— 去重与新颖度都要看历史。

        按 gap_ref 去重只挡得住"同一个缺口"，挡不住"同一件事换个说法"，
        所以调用方还要拿 question 做相似度比对。
        """
        rows = self.conn.execute(
            "SELECT gap_ref, question, created FROM curiosity"
            " WHERE session = ? AND created >= ? ORDER BY created DESC LIMIT ?",
            (session, since, limit),
        ).fetchall()
        return [(str(r["gap_ref"]), str(r["question"]), int(r["created"]))
                for r in rows]

    def curiosity_gaps(self, session: str, *, now: int | None = None,
                       stale_days: int = 30, trait_days: int = 30,
                       fact_days: int = 30,
                       weak_confidence: float = 0.5) -> dict[str, list[dict[str, Any]]]:
        """四类记忆缺口，四条 SQL 一次查完。零 token。

        为什么缺口要由 SQL 算而不是让模型"想想自己还缺什么"：
        模型不知道自己不知道什么——它只会顺着上一句话编。真正的缺口
        是结构性的：有一行过期没解、有个实体除了名字什么都没有、
        有一条事实失效了却没有新值。这些只有库知道。
        """
        now = now if now is not None else _now()
        out: dict[str, list[dict[str, Any]]] = {}

        # ① 过期未解：问过没答案、也没人接话的疑问
        out["doubt"] = [dict(r) for r in self.conn.execute(
            "SELECT id, question, impact, expires, asked_count FROM doubts"
            " WHERE session = ? AND resolved = 0 AND expires <= ?"
            " ORDER BY impact DESC, expires ASC LIMIT 50",
            (session, now)).fetchall()]

        # ② 钉住的事件里提到、但角色卡上一个字都没有的人
        out["entity"] = [dict(r) for r in self.conn.execute(
            "SELECT ent.id AS id, ent.canonical AS canonical,"
            " ent.last_seen AS last_seen, count(ev.id) AS mentions,"
            " max(ev.event_time) AS last_event"
            " FROM events ev"
            " JOIN event_entities ee ON ee.event_id = ev.id"
            " JOIN entities ent ON ent.id = ee.entity_id"
            " WHERE ev.session = ? AND ev.pinned = 1 AND ev.archived = 0"
            "   AND ent.merged_into IS NULL AND ent.type = 'person'"
            "   AND NOT EXISTS (SELECT 1 FROM card_traits t"
            "                   WHERE t.entity_id = ent.id AND t.valid_to IS NULL)"
            " GROUP BY ent.id ORDER BY mentions DESC LIMIT 50",
            (session,)).fetchall()]

        # ③ 半信半疑的特征：置信度低 + 很久没被新证据更新
        out["trait"] = [dict(r) for r in self.conn.execute(
            "SELECT t.id AS id, t.trait AS trait, t.confidence AS confidence,"
            " coalesce(t.updated_at, t.valid_from, 0) AS ts,"
            " ent.canonical AS canonical"
            " FROM card_traits t JOIN entities ent ON ent.id = t.entity_id"
            " WHERE ent.session = ? AND ent.merged_into IS NULL"
            "   AND t.valid_to IS NULL AND t.confidence < ?"
            "   AND coalesce(t.updated_at, t.valid_from, 0) <= ?"
            " ORDER BY t.confidence ASC LIMIT 50",
            (session, weak_confidence, now - trait_days * 86400)).fetchall()]

        # ④ 失效了却没有新值：老地址、老公司、老手机号——这类"空着"
        #    比"记错了"更危险，因为下游会拿旧值当现值用
        out["fact"] = [dict(r) for r in self.conn.execute(
            "SELECT f.id AS id, f.key AS key, f.value AS value,"
            " f.valid_to AS valid_to, coalesce(ent.canonical, '') AS label"
            " FROM facts f LEFT JOIN entities ent ON ent.id = f.entity_id"
            " WHERE f.session = ? AND f.valid_to IS NOT NULL"
            "   AND f.valid_to BETWEEN ? AND ?"
            "   AND NOT EXISTS (SELECT 1 FROM facts g"
            "                   WHERE g.session = f.session AND g.key = f.key"
            "                     AND g.entity_id IS f.entity_id"
            "                     AND g.valid_to IS NULL)"
            " ORDER BY f.valid_to DESC LIMIT 50",
            (session, now - fact_days * 86400, now)).fetchall()]

        return out


    def get_affect(self, session: str) -> Affect | None:
        row = self.conn.execute(
            "SELECT * FROM affect WHERE session = ?", (session,)
        ).fetchone()
        return Affect.from_row(row) if row else None

    def upsert_affect(self, session: str, *, updated: int | None = None,
                      **values: float) -> Affect:
        """updated 默认墙钟，但 persist 必须显式传对话时刻（ingest）——
        回填三个月前的历史对话时，情绪的时间轴要跟着对话走，不能跟着
        "现在"走（M17 时间基准的同一课）。"""
        stamp = updated if updated is not None else _now()
        exist = self.get_affect(session)
        if exist is None:
            data = {k: values.get(k, getattr(Affect(session=session, updated=0), k))
                    for k in ("joy", "sadness", "anger", "fear", "disgust", "surprise")}
            with self.tx():
                self.conn.execute(
                    "INSERT INTO affect(session, joy, sadness, anger, fear, disgust,"
                    " surprise, updated) VALUES (?,?,?,?,?,?,?,?)",
                    (session, data["joy"], data["sadness"], data["anger"],
                     data["fear"], data["disgust"], data["surprise"], stamp),
                )
        else:
            sets = ", ".join(f"{k} = ?" for k in values)
            if sets:
                self.conn.execute(
                    f"UPDATE affect SET {sets}, updated = ? WHERE session = ?",
                    (*values.values(), stamp, session),
                )
        return self.get_affect(session)  # type: ignore[return-value]

    # ------------------------------------------------------------------ ledger
    def add_ledger(self, *, session: str, action: str, predicted: float = 0.0,
                   action_id: str | None = None, kind: str = "",
                   ts: int | None = None) -> str:
        """登记一条自主动作的预期（M23.1）。

        `ts` 可传：回填历史对话时"动作发生在哪一刻"要用对话时刻，
        用墙钟会把整条账簿的时间轴平移到今天，而 3 天过期线是按这里算的。
        """
        aid = action_id or uuid.uuid4().hex
        with self.tx():
            self.conn.execute(
                "INSERT INTO ledger(action_id, session, action, action_kind, predicted, ts)"
                " VALUES (?,?,?,?,?,?)",
                (aid, session, action, kind or "", predicted, ts or _now()),
            )
        return aid

    # ------------------------------------------------------------------ 会话状态
    def get_state(self, session: str, key: str, default: str = "") -> str:
        """会话级 KV。跨轮有效，跨会话无效。

        读不到返回 default 而不是抛：状态是"锦上添花"的东西，
        丢了最多是这一轮多检索一次，不该让整轮对话失败。
        """
        row = self.conn.execute(
            "SELECT value FROM session_state WHERE session = ? AND key = ?",
            (session, key),
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, session: str, key: str, value: str) -> None:
        self._assert_writable()
        self.conn.execute(
            "INSERT INTO session_state(session, key, value, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(session, key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (session, key, value, _now()),
        )

    def open_ledger(self, session: str, within_seconds: int = 3 * 86400,
                    now: int | None = None) -> list[LedgerEntry]:
        """待结算的动作，只取窗口内的。窗口外的不返回——它们该被
        `expire_ledger` 标成 -1，而不是混进候选里让结算去猜。"""
        rows = self.conn.execute(
            "SELECT * FROM ledger WHERE session = ? AND settled = 0 AND ts >= ? "
            "ORDER BY ts DESC",
            (session, (now or _now()) - within_seconds),
        ).fetchall()
        return [LedgerEntry.from_row(r) for r in rows]

    def get_ledger(self, action_id: str) -> LedgerEntry | None:
        row = self.conn.execute(
            "SELECT * FROM ledger WHERE action_id = ?", (action_id,)
        ).fetchone()
        return LedgerEntry.from_row(row) if row else None

    def settle_ledger(self, action_id: str, actual: float,
                      now: int | None = None) -> float:
        row = self.conn.execute(
            "SELECT predicted, settled FROM ledger WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"未知 action_id: {action_id}")
        if int(row["settled"]) != 0:
            # 已结算/已作废的不许二次结算：同一个 action 被骂两次会算两遍 RPE，
            # 懊悔记忆也跟着重复生成。"只结算 1 条"是手册 M23.2 的硬要求。
            raise StoreError(f"action 已结算: {action_id}")
        rpe = actual - float(row["predicted"])
        with self.tx():
            self.conn.execute(
                "UPDATE ledger SET actual = ?, rpe = ?, settled = 1, settled_at = ?"
                " WHERE action_id = ?",
                (actual, rpe, now or _now(), action_id),
            )
        return rpe

    def expire_ledger(self, session: str, *, within_seconds: int = 3 * 86400,
                      now: int | None = None) -> list[str]:
        """超过窗口还没等到反馈的，标成 settled=-1（过期作废，不猜）。

        为什么"不猜"：把三天前的动作按今天的情绪结算，等于给自己编一段
        从来没发生过的教训。账簿宁可留空账，也不能记假账。
        返回被作废的 action_id，调用方用不着，测试要用。
        """
        cutoff = (now or _now()) - within_seconds
        rows = self.conn.execute(
            "SELECT action_id FROM ledger WHERE session = ? AND settled = 0 AND ts < ?",
            (session, cutoff),
        ).fetchall()
        ids = [r["action_id"] for r in rows]
        if ids:
            with self.tx():
                self.conn.executemany(
                    "UPDATE ledger SET settled = -1, settled_at = ? WHERE action_id = ?",
                    [((now or _now()), aid) for aid in ids],
                )
        return ids

    def ledger_stats(self, session: str = "", settled_only: bool = True) -> dict[str, Any]:
        """账簿体检：笔数 / 平均 RPE / 懊悔数。**只给 CLI 和测试看，不进 prompt。**"""
        cond = "WHERE session = ?" if session else "WHERE 1=1"
        args: list[Any] = [session] if session else []
        if settled_only:
            cond += " AND settled = 1"
        row = self.conn.execute(
            f"SELECT count(*) AS n, avg(rpe) AS avg_rpe,"
            f" sum(CASE WHEN rpe <= -0.5 THEN 1 ELSE 0 END) AS regrets"
            f" FROM ledger {cond}", args).fetchone()
        open_n = self.conn.execute(
            "SELECT count(*) AS n FROM ledger WHERE settled = 0"
            + (" AND session = ?" if session else ""),
            ([session] if session else [])).fetchone()["n"]
        return {
            "settled": int(row["n"] or 0),
            "open": int(open_n or 0),
            "avg_rpe": float(row["avg_rpe"] or 0.0),
            "regrets": int(row["regrets"] or 0),
        }

    def regrets(self, session: str, *, keyword: str = "", limit: int = 5) -> list[Event]:
        """懊悔记忆，按动作类型定向取（M23.3）。

        `keyword` 是动作类型的中文词（"改文件"/"发消息"），懊悔记忆的
        keywords 里必带它——手册 M23.3 第 3 条。这里不走 L2 检索：
        L2 回答的是"这句话让我想起什么"，懊悔要回答的是"我要做这类
        动作了，以前栽过吗"——问题不同，用关键词打分去近似只会把
        不相干的懊悔捞上来。
        """
        sql = ("SELECT * FROM events WHERE session = ? AND kind = 'regret'"
               " AND archived = 0")
        args: list[Any] = [session]
        if keyword:
            sql += " AND keywords LIKE ?"
            args.append(f"%{keyword}%")
        sql += " ORDER BY weight DESC, event_time DESC LIMIT ?"
        args.append(limit)
        return [Event.from_row(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ turns
    def add_turn(self, *, session: str, role: str, content: str,
                 partial: int = 0, ts: int | None = None) -> int:
        ts = ts or _now()
        with self.tx():
            row = self.conn.execute(
                "SELECT coalesce(max(idx), -1) + 1 AS n FROM turns WHERE session = ?",
                (session,),
            ).fetchone()
            idx = int(row["n"])
            self.conn.execute(
                "INSERT INTO turns(session, idx, role, content, ts, partial)"
                " VALUES (?,?,?,?,?,?)",
                (session, idx, role, content, ts, partial),
            )
        return idx

    def add_turns(self, session: str, items: Sequence[tuple[str, str, int]],
                  ts: int | None = None) -> list[int]:
        """一次事务写多条 turn，idx 连续。

        items: [(role, content, partial), ...]

        为什么必须批量：一轮 = user + assistant 两条。分两次调 add_turn，
        中间若有并发请求插进来，idx 就会交叉（u2 出现在 a1 之前），
        切片时窗口边界会错位。一个事务里取 max+1 再连续插入，是唯一稳妥的写法。
        """
        if not items:
            return []
        ts = ts or _now()
        with self.tx():
            row = self.conn.execute(
                "SELECT coalesce(max(idx), -1) + 1 AS n FROM turns WHERE session = ?",
                (session,),
            ).fetchone()
            start = int(row["n"])
            idxs = list(range(start, start + len(items)))
            self.conn.executemany(
                "INSERT INTO turns(session, idx, role, content, ts, partial)"
                " VALUES (?,?,?,?,?,?)",
                [(session, i, role, content, ts, partial)
                 for i, (role, content, partial) in zip(idxs, items)],
            )
        return idxs

    def get_turns(self, session: str, limit: int = 100) -> list[Turn]:
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE session = ? ORDER BY idx DESC LIMIT ?",
            (session, limit),
        ).fetchall()
        return [Turn.from_row(r) for r in reversed(rows)]

    def last_user_turn(self, session: str) -> int | None:
        """用户最后一次说话的墙钟时刻。好奇发言前要用它判断
        "现在是不是正在聊"——用户刚说完话，AI 在回复里顺手问就行，
        不需要在后台另开一句。"""
        row = self.conn.execute(
            "SELECT max(ts) AS ts FROM turns WHERE session = ? AND role = 'user'",
            (session,)).fetchone()
        ts = row["ts"] if row else None
        return int(ts) if ts is not None else None

    def pending_turns(self, session: str, limit: int = 500) -> list[Turn]:
        """尚未被抽取窗口消费的 turn，按时间正序。

        正序很重要：切片是从旧到新切的，倒序会让窗口边界落在错误的位置。
        """
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE session = ? AND extracted_at IS NULL"
            " ORDER BY idx ASC LIMIT ?",
            (session, limit),
        ).fetchall()
        return [Turn.from_row(r) for r in rows]

    def mark_turns_extracted(self, session: str, upto_idx: int) -> int:
        with self.tx():
            cur = self.conn.execute(
                "UPDATE turns SET extracted_at = ? WHERE session = ? "
                "AND idx <= ? AND extracted_at IS NULL",
                (_now(), session, upto_idx),
            )
        return cur.rowcount

    def list_sessions(self) -> list[str]:
        """turns 里出现过的所有 session。CLI 的 `extract` 不带 --session 时用它。"""
        rows = self.conn.execute(
            "SELECT DISTINCT session FROM turns ORDER BY session").fetchall()
        return [str(r[0]) for r in rows]

    def active_sessions(self, *, since: int, limit: int = 20) -> list[str]:
        """最近还在说话的会话。好奇只去"有人的房间"里准备问题：
        给一年前的会话攒一堆问题，纯属浪费队列。"""
        rows = self.conn.execute(
            "SELECT session, max(ts) AS ts FROM turns GROUP BY session"
            " HAVING max(ts) >= ? ORDER BY ts DESC LIMIT ?",
            (since, limit)).fetchall()
        return [str(r["session"]) for r in rows]

    def affect_sessions(self) -> list[str]:
        """有情绪行的会话。和 list_sessions（turns 里的）取并集才是全集：
        对话可能被清过，情绪还在；也可能刚建还没写过情绪。"""
        rows = self.conn.execute(
            "SELECT DISTINCT session FROM affect").fetchall()
        return [str(r[0]) for r in rows]

    def event_sessions(self) -> list[str]:
        """events 里出现过数据的 session。

        **和 `list_sessions()` 不是一回事**：那个查的是 `turns`（还没抽取的原文），
        巩固要的是 `events`（已经抽出来的记忆）。用错的话，一个"turn 已抽完
        但 turns 行被清理过"的库会显示"没有会话"，而里面明明有一万条记忆。
        """
        rows = self.conn.execute(
            "SELECT DISTINCT session FROM events ORDER BY session").fetchall()
        return [str(r[0]) for r in rows]

    def pending_turn_count(self, session: str) -> int:
        """未抽取的 turn 条数。

        代理每轮结账后都要问一次"攒够没有"。用 count(*) 而不是把
        500 条 content 全读出来再 len()——后者每轮多搬几十 KB 垃圾。
        """
        row = self.conn.execute(
            "SELECT count(*) FROM turns WHERE session = ? AND extracted_at IS NULL",
            (session,),
        ).fetchone()
        return int(row[0])

    def active_facts_by_value(self, session: str, value: str) -> list[tuple[str, str]]:
        """按"值"反查当前有效的事实（被动纠正用：用户说"不是上海，是北京"）。

        只返回 (key, value)，不返回整行——调用方只需要知道改哪个槽。
        """
        rows = self.conn.execute(
            "SELECT key, value FROM facts WHERE session = ? AND valid_to IS NULL "
            "AND trim(value) = ?", (session, value.strip()),
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def last_turn(self, session: str):
        """同一会话最后一条 turn（role, content）；没有就 None。"""
        row = self.conn.execute(
            "SELECT role, content FROM turns WHERE session = ? ORDER BY idx DESC LIMIT 1",
            (session,),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def extracted_turn_count(self, session: str) -> int:
        """这个会话已经抽过的 turn 条数（0 = 还没抽过）。"""
        row = self.conn.execute(
            "SELECT count(*) FROM turns WHERE session = ? AND extracted_at IS NOT NULL",
            (session,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ extract_runs
    _RUN_COLS = (
        "status", "n_events", "n_facts", "n_traits", "n_plans", "n_doubts",
        "verbatim_fail", "attempts", "prompt_tokens", "completion_tokens",
        # 004：缓存命中与思考 token。没有这两列，D13 的"省 36%"永远只是推断。
        "cached_tokens", "reasoning_tokens",
        "cost", "latency_ms", "error",
    )

    def record_run(
        self,
        *,
        session: str,
        start_idx: int,
        end_idx: int,
        status: str,
        **fields: Any,
    ) -> int:
        """登记一次抽取。同窗口重跑覆盖上一条（UNIQUE 约束）。

        这里用 INSERT ... ON CONFLICT 而不是先查再写：
        查+写在并发下会两个都查不到、两个都插。虽然抽取是单线程的，
        但"以后改成并发"这件事迟早会发生。
        """
        data: dict[str, Any] = {}
        fields = {**fields, "status": status}
        for c in self._RUN_COLS:
            v = fields.get(c)
            if v is None:
                v = 0.0 if c == "cost" else (0 if c != "error" else None)
            if c == "attempts" and not v:
                v = 1
            data[c] = v
        cols = ["session", "start_idx", "end_idx", *self._RUN_COLS, "created"]
        vals = [session, start_idx, end_idx, *[data[c] for c in self._RUN_COLS], _now()]
        updates = ", ".join(f"{c} = excluded.{c}" for c in ("status", *self._RUN_COLS, "created"))
        with self.tx():
            cur = self.conn.execute(
                f"INSERT INTO extract_runs({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))}) "
                f"ON CONFLICT(session, start_idx, end_idx) DO UPDATE SET {updates}",
                tuple(vals),
            )
        return int(cur.lastrowid or 0)

    def run_stats(self, session: str | None = None, since: int | None = None) -> dict[str, Any]:
        """抽取成功率与花费。R-5.2 的数字直接从这里出。"""
        cond = ["created >= ?"]
        params: list[Any] = [since or 0]
        if session:
            cond.append("session = ?")
            params.append(session)
        rows = self.conn.execute(
            f"SELECT status, count(*) AS n, sum(n_events) AS ev,"
            f" sum(verbatim_fail) AS vf, sum(cost) AS cost,"
            f" sum(prompt_tokens) AS pt, sum(cached_tokens) AS ct,"
            f" sum(reasoning_tokens) AS rt"
            f" FROM extract_runs WHERE {' AND '.join(cond)} GROUP BY status",
            tuple(params),
        ).fetchall()
        out: dict[str, Any] = {r["status"]: int(r["n"]) for r in rows}
        total = sum(out.values())
        ok = out.get("ok", 0)
        # 分母只算"真的调过模型"的窗口。skipped 是被预筛挡掉的，
        # 算进分母会让成功率看起来像 50%，而其实一次都没失败。
        attempted = ok + out.get("degraded", 0) + out.get("error", 0)
        out["total"] = total
        out["attempted"] = attempted
        out["success_rate"] = round(ok / attempted, 4) if attempted else 0.0
        out["events"] = sum(int(r["ev"] or 0) for r in rows)
        out["verbatim_fail"] = sum(int(r["vf"] or 0) for r in rows)
        out["cost"] = round(sum(float(r["cost"] or 0) for r in rows), 6)
        out["prompt_tokens"] = sum(int(r["pt"] or 0) for r in rows)
        out["cached_tokens"] = sum(int(r["ct"] or 0) for r in rows)
        out["reasoning_tokens"] = sum(int(r["rt"] or 0) for r in rows)
        # 命中率只在"真调过模型"的窗口上有意义
        out["cache_hit_rate"] = (
            round(out["cached_tokens"] / out["prompt_tokens"], 4)
            if out["prompt_tokens"] else 0.0)
        return out

    # ------------------------------------------------------------------ metrics
    def add_metric(self, metric: Metric | None = None, **kwargs: Any) -> int:
        data = (metric.model_dump() if metric else {}) | kwargs
        data.setdefault("ts", _now())
        cols = [c for c in (
            "ts", "session", "req_id", "t0", "t1", "t2", "t3",
            "prompt_tokens", "completion_tokens", "cached_tokens", "cost",
        ) if c in data]
        with self.tx():
            cur = self.conn.execute(
                f"INSERT INTO metrics({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(data[c] for c in cols),
            )
        return int(cur.lastrowid)

    def token_sum(self, since: int | None = None,
                  until: int | None = None, *,
                  session: str | None = None) -> dict[str, float]:
        """统计窗口内的真实用量（M27.3 日预算按这个数）。

        `until` 是必须的，不是为对称好看：熔断测试要用固定时刻回放，
        只给 since 会把"今天晚些时候"的行也算进来，于是降级判定在测试里
        永远成立、在线上却提前一天触发。上半开区间 [since, until)。
        """
        row = self.conn.execute(
            "SELECT coalesce(sum(prompt_tokens),0) AS p,"
            " coalesce(sum(completion_tokens),0) AS c,"
            " coalesce(sum(cached_tokens),0) AS k,"
            " coalesce(sum(cost),0) AS cost, count(*) AS n"
            " FROM metrics WHERE ts >= ? AND ts < ?"
            + (" AND session = ?" if session else ""),
            (since or 0, until or 2 ** 62, *([session] if session else [])),
        ).fetchone()
        return {
            "prompt_tokens": int(row["p"]), "completion_tokens": int(row["c"]),
            "cached_tokens": int(row["k"]), "cost": float(row["cost"]),
            "calls": int(row["n"]),
        }

    def extract_token_sum(self, since: int | None = None,
                          until: int | None = None, *,
                          session: str | None = None) -> dict[str, float]:
        """抽取调用的用量。

        为什么要单独一条：`metrics` 只记**对话**请求（proxy 那条链路），
        抽取是后台任务，它的 token 只落在 `extract_runs`。只读 metrics 的话，
        F 段（每 5 轮一次全窗口抽取）的钱完全看不见——重度用户每天 100 轮
        就是 20 次抽取，忽略它等于漏掉一半成本。
        """
        row = self.conn.execute(
            "SELECT coalesce(sum(prompt_tokens),0) AS p,"
            " coalesce(sum(completion_tokens),0) AS c,"
            " coalesce(sum(cached_tokens),0) AS k,"
            " coalesce(sum(cost),0) AS cost, count(*) AS n"
            " FROM extract_runs WHERE created >= ? AND created < ?"
            " AND status IN ('ok', 'degraded', 'error')"
            + (" AND session = ?" if session else ""),
            (since or 0, until or 2 ** 62, *([session] if session else [])),
        ).fetchone()
        return {
            "prompt_tokens": int(row["p"]), "completion_tokens": int(row["c"]),
            "cached_tokens": int(row["k"]), "cost": float(row["cost"]),
            "calls": int(row["n"]),
        }

    # ------------------------------------------------------------------ stats
    def ping(self) -> bool:
        """健康检查。

        放在 DAO 里而不是路由里，是为了守住"SQL 只在 core"这条线——
        一旦开了口子，半年后 adapters 里会长出二十条散装 SQL。
        """
        self.conn.execute("SELECT 1").fetchone()
        return True

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in ("events", "entities", "card_traits", "facts", "doubts",
                  "plans", "affect", "ledger", "turns", "metrics", "kw_index",
                  "event_entities", "extract_runs"):
            if table_exists(self.conn, t):
                out[t] = int(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        return out

    def board_snapshot(self, *, limit: int = 120) -> dict[str, Any]:
        """DSH 记忆看板的数据快照（只读，M30）。

        三层映射照 dsh-plugin-xujie-memory 0.8 的语义：
          语义 semantic  = 事实 + 角色特征 + 实体
          情节 episodic  = 事件（摘要 + 原文预览 + 关键词）
          工作 working   = 最近用户消息 + 未解决疑问
        节点与边都来自真实库；前端只负责画。
        """
        counts = self.counts()
        tiers = {"hot": 0, "warm": 0, "cold": 0}
        try:
            for k, n in self.tier_distribution().items():
                tiers[{1: "hot", 2: "warm", 3: "cold"}.get(int(k), "warm")] = int(n)
        except Exception:  # noqa: BLE001
            pass

        events = [dict(r) for r in self.conn.execute(
            "SELECT id, session, event_time, summary, substr(raw, 1, 240) AS raw_preview,"
            " keywords, tier, weight, pinned FROM events WHERE archived = 0"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        facts = [dict(r) for r in self.conn.execute(
            "SELECT id, key, value, session, entity_id, confidence FROM facts"
            " WHERE valid_to IS NULL ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        entities = [dict(r) for r in self.conn.execute(
            "SELECT id, canonical, type, confidence, aliases FROM entities"
            " WHERE merged_into IS NULL ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()]
        traits = [dict(r) for r in self.conn.execute(
            "SELECT id, entity_id, trait, evidence, confidence FROM card_traits"
            " WHERE valid_to IS NULL ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        turns = [dict(r) for r in self.conn.execute(
            "SELECT session, idx, role, substr(content, 1, 160) AS content, ts FROM turns"
            " WHERE role = 'user' ORDER BY ts DESC, idx DESC LIMIT 20").fetchall()]
        doubts = [dict(r) for r in self.conn.execute(
            "SELECT id, session, question, impact, cost FROM doubts"
            " WHERE resolved = 0 AND converted = 0 ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        links: list[dict[str, Any]] = []
        if table_exists(self.conn, "event_entities"):
            links = [dict(r) for r in self.conn.execute(
                "SELECT event_id, entity_id FROM event_entities").fetchall()]

        nodes: list[dict[str, Any]] = []
        index: dict[str, int] = {}

        def add(node: dict[str, Any]) -> None:
            if node["id"] in index:
                return
            index[node["id"]] = len(nodes)
            nodes.append(node)

        for e in events:
            # 海马体自己的分层：重点标注层（pinned）与遗忘权重池（其余事件）。
            add({"id": f"event:{e['id']}",
                 "layer": "pinned" if e["pinned"] else "forgotten", "kind": "event",
                 "content": e["summary"] or e["raw_preview"] or "",
                 "priority": float(e["weight"] or 0.0), "ts": e["event_time"],
                 "session": e["session"], "keywords": e["keywords"] or "",
                 "tier": e["tier"], "raw": e["raw_preview"], "pinned": e["pinned"]})
        for f in facts:
            add({"id": f"fact:{f['key']}", "layer": "persistent", "kind": "fact",
                 "content": f"{f['key']} = {f['value']}",
                 "priority": float(f["confidence"] or 0.5) * 2.0,
                 "session": f["session"], "entity_id": f["entity_id"]})
        for t in traits:
            evidence = (t["evidence"] or "").strip()
            add({"id": f"trait:{t['id']}", "layer": "persistent", "kind": "trait",
                 "content": (f"{t['trait']}：{evidence}" if evidence else str(t["trait"])),
                 "priority": float(t["confidence"] or 0.5) * 2.0,
                 "entity_id": t["entity_id"]})
        for en in entities:
            add({"id": f"entity:{en['id']}", "layer": "persistent", "kind": "entity",
                 "content": en["canonical"],
                 "priority": float(en["confidence"] or 0.5) * 2.0,
                 "type": en["type"], "aliases": en["aliases"]})
        for d in doubts:
            # 存疑池是外挂，不进三层；前端单独一个视图展示。
            add({"id": f"doubt:{d['id']}", "layer": "doubt", "kind": "doubt",
                 "content": d["question"], "priority": float(d["impact"] or 0.5) * 2.0,
                 "session": d["session"]})

        edges: list[dict[str, str]] = []

        def link(a: str, b: str) -> None:
            if a in index and b in index and a != b:
                edges.append({"source": a, "target": b})

        by_session: dict[str, list[str]] = {}
        for e in events:
            by_session.setdefault(e["session"] or "", []).append(f"event:{e['id']}")
        for ids in by_session.values():
            for i in range(1, min(len(ids), 8)):
                link(ids[i - 1], ids[i])
        for l in links:
            link(f"event:{l['event_id']}", f"entity:{l['entity_id']}")
        for f in facts:
            if f.get("entity_id"):
                link(f"fact:{f['key']}", f"entity:{f['entity_id']}")
        for t in traits:
            if t.get("entity_id"):
                link(f"trait:{t['id']}", f"entity:{t['entity_id']}")

        return {"counts": counts, "tiers": tiers, "nodes": nodes, "edges": edges,
                "events": events, "facts": facts, "entities": entities,
                "turns": turns, "doubts": doubts}

    def giveup_stats(self, *, session: str | None = None,
                     top: int = 10) -> list[dict[str, Any]]:
        """按 (level, reason) 聚合弃权记录，次数降序。M22.3 的能力地图。

        reason 存在 summary 的"原因："之后；level 是专门列（013 迁移）。
        为什么不在 Python 里聚合：事件可能有几万条，SQL 的 GROUP BY
        在索引外也只要一次扫描，而全量拉进内存是几百毫秒起。
        """
        cond = " AND session = ?" if session else ""
        args: list = [session] if session else []
        args.append(top)
        rows = self.conn.execute(
            f"""
            SELECT giveup_level AS level,
                   trim(substr(summary, instr(summary, '原因：') + 3)) AS reason,
                   count(*) AS n
            FROM events
            WHERE kind = 'giveup' AND archived = 0{cond}
            GROUP BY giveup_level, reason
            ORDER BY n DESC, reason
            LIMIT ?
            """,
            args,
        ).fetchall()
        return [{"level": r["level"] or "?", "reason": r["reason"] or "",
                 "n": int(r["n"])} for r in rows]

    def giveup_warn_once(self, session: str, reason: str, n: int,
                         threshold: int = 5) -> bool:
        """同一 reason 攒够 threshold 次 → 提醒一次（M22.3 验收 #3）。

        和 pin_warn_once 同构：判据是"这条 reason 是否提醒过"，不是
        一个全局布尔——不同 reason 各自算，同一个 reason 清理后
        再攒到 5 次是"新的一次反复"，该再说一声……但清理无法发生
        （历史不会消失），所以同一个 reason 一辈子只提一次。
        """
        if n < threshold:
            return False
        marked = self.get_state(session, "giveup_warned", "")
        done = set(x for x in marked.split("\x1f") if x)
        if reason in done:
            return False
        done.add(reason)
        with self.tx():
            self.set_state(session, "giveup_warned", "\x1f".join(sorted(done)))
        return True

    def tier_distribution(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT tier, count(*) AS n FROM events GROUP BY tier"
        ).fetchall()
        return {int(r["tier"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ 权重重算（M14.2）
    def decay_rows(self, *, session: str | None = None,
                   after: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """重算权重需要的最小字段集。

        **刻意不返回 Event**。10 万条 Event 要过 10 万次 pydantic 校验，
        光构造就要几秒，而重算只需要 6 个字段。这条查询走
        `idx_events_session_tier` 的覆盖不够（要回表），但比构造对象便宜得多。

        `archived=1` 的跳过：归档就是"不再参与召回"，权重对它没意义。
        """
        sql = ("SELECT id, session, w0, tier, weight, pinned, recall_count,"
               " last_access, ingest_time FROM events WHERE archived = 0")
        args: list[Any] = []
        if session:
            sql += " AND session = ?"
            args.append(session)
        if after:
            sql += " AND id > ?"
            args.append(int(after))
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def set_weights(self, rows: Sequence[tuple[int, float, int]],
                    *, chunk: int = 2000) -> int:
        """批量写回 (id, weight, tier)。

        用 executemany 而不是逐条 UPDATE：10 万条逐条提交在 SQLite 上要
        十几秒（每条一次隐式事务），executemany 在同一个事务里是 0.2 秒级。

        分批提交是为了"百万级不把读挡住"——一个大事务持锁太久，
        代理那边的每次检索都会被卡住。
        """
        if not rows:
            return 0
        n = 0
        with self.tx():
            for i in range(0, len(rows), chunk):
                part = rows[i:i + chunk]
                self.conn.executemany(
                    "UPDATE events SET weight = ?, tier = ? WHERE id = ?",
                    [(float(w), int(t), int(i2)) for i2, w, t in part],
                )
                n += len(part)
        return n

    # ------------------------------------------------------------ M15 巩固
    def consolidate_rows(self, session: str, *, gap_days: int = 30,
                         limit: int = 0) -> list[dict[str, Any]]:
        """巩固的输入：可能重复的事件。

        只要 `kind='event'`——fact / regret / plan 这些是**不同类型**的记忆，
        两条 plan 就算措辞一样也不能合并（一个是"周三交报告"，一个是
        "周五复查"，共享"要去"不代表是同一件事）。
        手册 §M15.1 明确排除，这里落在 SQL 上，比在业务层判断更可靠。

        `superseded_by IS NULL` 必须带：已经并进别人的事件不能再当主记录，
        否则会出现 A→B 且 B→C 的链，最后谁取代谁说不清。
        """
        sql = ("SELECT id, session, event_time, ingest_time, keywords, summary, raw,"
               " weight, w0, recall_count, tier, pinned"
               " FROM events WHERE session = ? AND kind = 'event'"
               "   AND archived = 0 AND superseded_by IS NULL")
        args: list[Any] = [session]
        sql += " ORDER BY event_time, id"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def merge_into(self, master_id: int, member_ids: Sequence[int], *,
                   keywords: str | None = None, summary: str | None = None,
                   recall_count: int | None = None) -> int:
        """把 member_ids 并进 master_id（M15.2）。返回实际并掉的条数。

        三个字段各自可选，是因为**摘要重生成可能失败**。失败时不能连带
        把 supersede 也回滚——那样这一轮就白跑了；但也不能假装摘要改过了。
        所以拆开：supersede 先做（结构变更，必须成），摘要后做（内容变更，
        失败就保留主记录原摘要，关键词仍取并集保证检索不丢）。

        `keywords` 传的是**并集**：被并掉的事件不再出现在检索结果里，
        它们的词如果不同步到主记录，那些说法就再也搜不到了——
        这是合并最隐蔽的损失（原文还在，但找不到）。
        """
        members = [int(x) for x in member_ids if int(x) != int(master_id)]
        if not members:
            return 0
        with self.tx():
            marks = ",".join("?" * len(members))
            cur = self.conn.execute(
                f"UPDATE events SET superseded_by = ?"
                f" WHERE id IN ({marks}) AND superseded_by IS NULL",
                (int(master_id), *members),
            )
            n = cur.rowcount

            sets: list[str] = []
            vals: list[Any] = []
            if recall_count is not None:
                sets.append("recall_count = ?")
                vals.append(int(recall_count))
            if summary is not None:
                sets.append("summary = ?")
                vals.append(summary)
            if keywords is not None:
                sets.append("keywords = ?")
                vals.append(keywords)
            if sets:
                self.conn.execute(
                    f"UPDATE events SET {', '.join(sets)} WHERE id = ?",
                    (*vals, int(master_id)),
                )
            if keywords is not None:
                tokens = [t.lower() for t in keywords.split() if t]
                self.conn.execute(
                    "DELETE FROM kw_index WHERE event_id = ?", (int(master_id),))
                if tokens:
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)",
                        [(t, int(master_id)) for t in tokens],
                    )
            # 被并掉的事件从倒排里摘掉。它们的词已经进了主记录的并集，
            # **一条检索能力都不会丢**；留着只会让每轮检索多捞一批注定被
            # `superseded_by IS NULL` 过滤掉的 id——倒排越长这个浪费越大。
            # 撤销时靠 events.keywords 重建（见 unmerge）。
            self.conn.execute(
                f"DELETE FROM kw_index WHERE event_id IN ({marks})", tuple(members))
        return n

    def log_consolidation(self, *, session: str, master_id: int,
                          member_ids: Sequence[int], strong: bool, score: float,
                          summary_old: str = "", summary_new: str = "",
                          tokens: int = 0, cost: float = 0.0,
                          ts: int | None = None) -> int:
        """记一条巩固日志。**每次合并都必须写**，否则撤销无从谈起。"""
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO consolidation_log(ts, session, master_id, member_ids,"
                " n_merged, strong, score, summary_old, summary_new, tokens, cost)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else _now(), session, int(master_id),
                 json.dumps([int(x) for x in member_ids], ensure_ascii=False),
                 len(member_ids), 1 if strong else 0, float(score),
                 summary_old, summary_new, int(tokens), float(cost)),
            )
        return int(cur.lastrowid)

    def unmerge(self, master_id: int) -> list[int]:
        """撤销某个主记录上的合并（弱判据合并专用）。返回被放回的 id。

        **只放 superseded_by，不回滚摘要和关键词**。理由：摘要可能已经
        被后续的抽取改过，回滚会覆盖掉更新内容；关键词取并集是无损的，
        留着反而让主记录更好检索。撤销要解决的是"这几条不该合"，
        不是"把库恢复到合并前那一秒"。
        """
        rows = self.conn.execute(
            "SELECT id, member_ids FROM consolidation_log"
            " WHERE master_id = ? AND undone_at IS NULL", (int(master_id),)
        ).fetchall()
        freed: list[int] = []
        now = _now()
        with self.tx():
            for r in rows:
                ids = [int(x) for x in json.loads(r["member_ids"] or "[]")]
                if not ids:
                    continue
                marks = ",".join("?" * len(ids))
                self.conn.execute(
                    f"UPDATE events SET superseded_by = NULL"
                    f" WHERE id IN ({marks}) AND superseded_by = ?",
                    (*ids, int(master_id)),
                )
                # 倒排条目在合并时被摘掉了，这里按 events.keywords 重建。
                # 不重建的话，撤销之后这些事件"在库里但搜不到"——
                # 比不撤销还糟：用户以为恢复了，实际还是查不出来。
                for i in ids:
                    row = self.conn.execute(
                        "SELECT keywords FROM events WHERE id = ?", (i,)).fetchone()
                    toks = [t.lower() for t in (row["keywords"] or "").split() if t]
                    if toks:
                        self.conn.executemany(
                            "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)",
                            [(t, i) for t in toks],
                        )
                freed.extend(ids)
            self.conn.execute(
                "UPDATE consolidation_log SET undone_at = ? WHERE master_id = ?"
                " AND undone_at IS NULL", (now, int(master_id)),
            )
        return freed

    def consolidation_stats(self, session: str = "") -> dict[str, Any]:
        cond, args = "", []
        if session:
            cond = " WHERE session = ?"
            args.append(session)
        row = self.conn.execute(
            "SELECT count(*) AS batches, coalesce(sum(n_merged), 0) AS merged,"
            " coalesce(sum(CASE WHEN strong = 1 THEN n_merged ELSE 0 END), 0) AS strong,"
            " coalesce(sum(CASE WHEN strong = 0 THEN n_merged ELSE 0 END), 0) AS weak,"
            " coalesce(sum(CASE WHEN undone_at IS NOT NULL THEN n_merged ELSE 0 END), 0)"
            "   AS undone"
            f" FROM consolidation_log{cond}", tuple(args),
        ).fetchone()
        return {k: int(row[k] or 0) for k in
                ("batches", "merged", "strong", "weak", "undone")}
