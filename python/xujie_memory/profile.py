"""人物画像：为交流对象建一张会变的卡。

画像回答的是「这个人是谁」，不是「账上还有多少」。
两件事放在同一个库里，但**不是同一层**：

    events / states  事实。账上有几袋米，是可核对的。
    person_tags      印象。他说话爱带「哈」，是我的看法。

方案第四章给了三条硬约束，这个模块就是它们的落地：

    1. 标签是印象，不是事实
    2. 来源必须分开记，推断永不自动升级为事实
    3. 账号不等于人物，可疑期间新标签先暂存

---

## 为什么标签要有 source

「用户说他不吃辣」和「模型觉得他可能不吃辣」是两件事。
前者可以拿去点菜，后者只能拿去试探。混在一起记，
模型猜错一次就会被当成事实用一辈子——而它从来没被确认过。

所以 `source` 是这一层最重要的字段：

    confirmed  用户明说的          → 可以当事实用，可以授权
    observed   系统看到的（他自己重复了三次） → 可以当事实用，不做授权依据
    inferred   模型推的            → 只能影响语气与追问，**不能授权**

## 为什么标签要有 confidence 和 seen_count 两个数

3.3 那句话：**反复提及的谎言可能很容易想起，但仍不可信。**

`seen_count` 是「多常用」，`confidence` 是「多可信」。
把它们合成一个数，就再也分不开了——而常见的情形恰恰是
「他说了很多次，但我不信」。所以两个字段，各记各的。

## 为什么可疑期间要暂存

「账号被同事拿去用」是真实场景。如果新观察直接写进主画像，
一次借用就会永久污染对这个人的认知（他从此"爱说脏话"）。
暂存区的意思是：**先看见，不采信**。等判断清楚了再合。

## 为什么不自动判「换人」

方案 4.5 写得很清楚：轻量模型输出线索，主 AI 自己判断。
这一层只负责**把线索摆出来**（alert），不做身份结论。
表达异常只是线索，不是换人的证明——人也会因为心情、场合、玩笑而变。
"""

from __future__ import annotations

import json
import re

from .engine import canonical_json, digest
from .detect import address_claim as _address_claim

# 标签来源。顺序即可信度顺序：confirmed > observed > inferred。
SOURCE_CONFIRMED = "confirmed"
SOURCE_OBSERVED = "observed"
SOURCE_INFERRED = "inferred"
SOURCES = (SOURCE_CONFIRMED, SOURCE_OBSERVED, SOURCE_INFERRED)

# 来源决定可信度的上限。推断再确定也上不去——
# 因为它没有被任何人确认过。
_CONFIDENCE_CAP = {
    SOURCE_CONFIRMED: 1.0,
    SOURCE_OBSERVED: 0.8,
    # 0.5 不是"一半可信"，是"还没有证据"。留在这个数上，
    # 是为了让下游能一眼看出这条是猜的。
    SOURCE_INFERRED: 0.5,
}

# 画像状态
PROFILE_ACTIVE = "active"
PROFILE_SUSPECT = "suspect"

# 标签状态
TAG_ACTIVE = "active"
TAG_STAGED = "staged"          # 画像可疑期间的新标签，先不进主卡
TAG_CORRECTED = "corrected"    # 被用户纠正
TAG_EXPIRED = "expired"        # 失效（长期没再出现）

# 标签类别。粗到能解释，细到能区分用途。
TAG_KINDS = ("address", "preference", "habit", "style", "topic", "relation")


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _override_key(tag: dict) -> str:
    return "slot:" + tag["slot"] if tag.get("slot") else "value:" + tag["value"]


class ProfileStore:
    """人物画像的读写。不改记忆，不改账，只管"我对这个人的看法"。"""

    def __init__(self, engine):
        self.engine = engine

    # ---------------------------------------------------------- 画像
    def ensure(self, scope_id: str, account_id: str,
               display_name: str | None = None) -> dict:
        """建卡或取卡。首次交互时建一张空的。

        账号是账号，人物是人物（4.3）。这里用 (scope, account) 定位，
        因为初期一个账号绑一张主卡——真的发现换人了，
        也是把这**张**卡标成 suspect，不是新建一张卡把人劈成两半。
        """
        row = self.get(scope_id, account_id)
        if row:
            return row
        profile_id = "pf_" + digest("profile", scope_id, account_id)[:20]
        now = self.engine.now()
        self.engine._conn().execute(
            "INSERT OR IGNORE INTO person_profiles"
            "(profile_id, scope_id, account_id, display_name, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (profile_id, scope_id, account_id, display_name, PROFILE_ACTIVE,
             now, now),
        )
        return self.get(scope_id, account_id) or {}

    def get(self, scope_id: str, account_id: str) -> dict | None:
        row = self.engine._conn().execute(
            "SELECT * FROM person_profiles WHERE scope_id=? AND account_id=?",
            (scope_id, account_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def profiles(self, scope_id: str) -> list[dict]:
        rows = self.engine._conn().execute(
            "SELECT * FROM person_profiles WHERE scope_id=? ORDER BY created_at",
            (scope_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------------------------------------------------------- 标签
    def tag(
        self,
        scope_id: str,
        account_id: str,
        kind: str,
        value: str,
        *,
        source: str = SOURCE_INFERRED,
        confidence: float | None = None,
        evidence_trace_id: str | None = None,
    ) -> dict:
        """提一个标签。

        三条规则，都写在这儿而不是调用方那里：

        1. **来源决定上限**。推断给的 confidence 再高也压到 0.5。
        2. **画像可疑时进暂存**。一次借用不该永久污染认知。
        3. **重复的标签不新增，只计数**。「他第三次说自己不吃辣」
           是一条标签被看见三次，不是三条标签。
        """
        if source not in SOURCES:
            raise ValueError(f"未知来源: {source}")
        if kind not in TAG_KINDS:
            raise ValueError(f"未知标签类别: {kind}")
        if not value:
            raise ValueError("标签值不能为空")

        profile = self.ensure(scope_id, account_id)
        cap = _CONFIDENCE_CAP[source]
        conf = cap if confidence is None else min(float(confidence), cap)

        # 已经有一条一模一样的生效标签 → 加次数，不新增。
        # 只认 active：被纠正过的旧值再出现，是**新的一次**，该另起一条。
        same = self.engine._conn().execute(
            "SELECT * FROM person_tags WHERE profile_id=? AND kind=? AND value=?"
            " AND status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            (profile["profile_id"], kind, value, TAG_ACTIVE, TAG_STAGED),
        ).fetchone()
        if same:
            # 来源可以升（观察到 → 用户确认了），不能降。
            new_source = source
            if _rank(same["source"]) > _rank(source):
                new_source = same["source"]
            new_conf = max(float(same["confidence"]), conf)
            self.engine._conn().execute(
                "UPDATE person_tags SET seen_count=seen_count+1, source=?,"
                " confidence=?, updated_at=? WHERE tag_id=?",
                (new_source, min(new_conf, _CONFIDENCE_CAP[new_source]),
                 self.engine.now(), same["tag_id"]),
            )
            return self._tag_by_id(same["tag_id"]) or {}

        status = TAG_STAGED if profile["status"] == PROFILE_SUSPECT else TAG_ACTIVE
        tag_id = "tg_" + digest("tag", profile["profile_id"], kind, value,
                                self.engine.now(), str(_next_seq(self)))[:20]
        now = self.engine.now()
        self.engine._conn().execute(
            "INSERT INTO person_tags(tag_id, profile_id, scope_id, kind, value,"
            " source, confidence, seen_count, status, evidence_trace_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tag_id, profile["profile_id"], scope_id, kind, value, source,
             conf, 1, status, evidence_trace_id, now, now),
        )
        return self._tag_by_id(tag_id) or {}

    def confirm(self, tag_id: str, *,
                evidence_trace_id: str | None = None) -> dict | None:
        """用户确认了一条推断。这是推断**唯一**的升级路径（4.2）。

        不能自动升级的理由：推断是模型的看法，确认是人的表态。
        两者之间少的那一步是「问过」——没有被问过的东西，
        不该因为"看起来对"就变成事实。
        """
        row = self._tag_by_id(tag_id)
        if row is None:
            return None
        self.engine._conn().execute(
            "UPDATE person_tags SET source=?, confidence=?, status=?,"
            " evidence_trace_id=COALESCE(?, evidence_trace_id), updated_at=?"
            " WHERE tag_id=?",
            (SOURCE_CONFIRMED, 1.0, TAG_ACTIVE, evidence_trace_id,
             self.engine.now(), tag_id),
        )
        return self._tag_by_id(tag_id)

    def correct(self, tag_id: str, value: str, *,
                evidence_trace_id: str | None = None) -> dict | None:
        """用户纠正了一条标签。

        旧的不删，标成 corrected，新的指向旧的（`supersedes`）。
        理由和记忆层一样：**"我当时以为他是什么样"本身是有价值的历史**。
        直接覆盖就再也答不了"这个印象是怎么变过来的"。
        """
        old = self._tag_by_id(tag_id)
        if old is None:
            return None
        if old["value"] == value:
            # 同一个值，只是再确认了一次，不算纠正。
            return self.confirm(tag_id, evidence_trace_id=evidence_trace_id)

        now = self.engine.now()
        self.engine._conn().execute(
            "UPDATE person_tags SET status=?, updated_at=? WHERE tag_id=?",
            (TAG_CORRECTED, now, tag_id),
        )
        new_tag = self.tag(
            old["scope_id"], self._account_of(old["profile_id"]),
            old["kind"], value,
            source=SOURCE_CONFIRMED, confidence=1.0,
            evidence_trace_id=evidence_trace_id,
        )
        self.engine._conn().execute(
            "UPDATE person_tags SET supersedes=? WHERE tag_id=?",
            (tag_id, new_tag["tag_id"]),
        )
        return self._tag_by_id(new_tag["tag_id"])

    def expire(self, tag_id: str) -> None:
        """让一条标签失效。不删——失效也是历史。"""
        self.engine._conn().execute(
            "UPDATE person_tags SET status=?, updated_at=? WHERE tag_id=?",
            (TAG_EXPIRED, self.engine.now(), tag_id),
        )

    def tags(self, scope_id: str, account_id: str, *,
             kind: str | None = None,
             status: str | list[str] = TAG_ACTIVE,
             limit: int | None = None) -> list[dict]:
        profile = self.get(scope_id, account_id)
        if profile is None:
            return []
        statuses = [status] if isinstance(status, str) else list(status)
        marks = ",".join("?" * len(statuses))
        sql = (f"SELECT * FROM person_tags WHERE profile_id=? AND status IN ({marks})")
        args: list = [profile["profile_id"], *statuses]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        # 可信度高的在前，同等可信度按被看见的次数排——这就是"印象的强度"。
        sql += " ORDER BY confidence DESC, seen_count DESC, created_at DESC"
        rows = [_row_to_dict(r) for r in self.engine._conn().execute(sql, args).fetchall()]
        if TAG_ACTIVE in statuses:
            overrides = self.engine._conn().execute(
                "SELECT kind,target_key,value FROM person_tag_overrides WHERE profile_id=?",
                (profile["profile_id"],),
            ).fetchall()
            hidden = {(r["kind"], r["target_key"]) for r in overrides if r["value"] is None}
            rows = [r for r in rows if (r["kind"], _override_key(r)) not in hidden]
            manual = [{"tag_id": r["target_key"], "profile_id": profile["profile_id"],
                       "scope_id": scope_id, "kind": r["kind"], "value": r["value"],
                       "source": SOURCE_CONFIRMED, "confidence": 1.0, "status": TAG_ACTIVE}
                      for r in overrides if r["value"] is not None and r["target_key"].startswith("manual:")
                      and (kind is None or r["kind"] == kind)]
            rows = manual + rows
        return rows[:int(limit)] if limit is not None else rows

    def _tag_by_id(self, tag_id: str) -> dict | None:
        row = self.engine._conn().execute(
            "SELECT * FROM person_tags WHERE tag_id=?", (tag_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def _account_of(self, profile_id: str) -> str:
        row = self.engine._conn().execute(
            "SELECT account_id FROM person_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        return row["account_id"] if row else ""

    # ---------------------------------------------------------- 一致性观察
    def watch(self, scope_id: str, account_id: str, text: str, *,
              trace_id: str | None = None) -> dict | None:
        """看一眼这句话跟画像对不对得上。**只出线索，不下结论。**

        方案 4.4 / 4.5 的分工：便宜的这一层负责「发现不像」，
        「是不是换了人」由主 AI 判断。所以这里返回的永远是 alert，
        不是 verdict。

        只认两种**确定性**线索，因为它们的误报率最低：

            1. 自称换了称呼      「叫我老板」——画像里的称呼是"小鲸"
            2. 明说不是本人      「我是他同事」——这句本身就是线索

        为什么不看语气、句长、用词：
        那些是**风格**线索，同一个人心情变了就会变。
        拿它们开 alert，会把"今天不高兴"判成"换人了"——
        而误判换人的代价是关系被凭空重置。
        """
        profile = self.get(scope_id, account_id)
        if profile is None:
            # 4.1：初次交互就该建一张空卡。不建的话，
            # 「我是他同事」这种第一句就出现的线索会直接漏掉——
            # 恰恰是这种话最该被听见。
            profile = self.ensure(scope_id, account_id)
        if profile["status"] == PROFILE_SUSPECT:
            # 已经在可疑状态就不再重复开 alert——一条线索说一次就够。
            return None

        signals: dict = {}

        claimed = _address_claim(text)
        if claimed:
            known = [t["value"] for t in self.tags(scope_id, account_id,
                                                   kind="address")]
            if known and claimed not in known:
                signals["address"] = {"said": claimed, "known": known}

        if _denies_being_self(text):
            signals["identity"] = {"said": text[:40]}

        if not signals:
            return None

        kind = "address_shift" if "address" in signals else "explicit_claim"
        return self.suspect(scope_id, account_id, kind=kind,
                            signals=signals, evidence_trace_id=trace_id)

    # ---------------------------------------------------------- 从记忆派生
    def derive_from_memory(self, scope_id: str, account_id: str,
                           state_keys: list[str] | None = None) -> list[dict]:
        """把**已经提交的事实**翻译成画像标签。

        为什么从 `states` 派生而不是在 ingest 里顺手写一份：
        事实层是 events/states，画像只是对它的**看法**。
        在写路径里再抄一份，就有了两个真相源——
        用户改口之后，账改了、画像没改，从此两个人格（红线一）。

        v0.52 改成增量：
          - `state_keys=None`：全量对账（进程里每个 scope 第一次、以及定期兜底）。
          - `state_keys=[...]`：只看这一轮动过的状态；空列表直接返回。
        旧实现每轮全量、逐条 3 句 SQL、各自落盘，还被调了两次——
        会话攒到 1500 条时单轮 1.5–2 秒，超过宿主超时后记忆静默失效。
        另外旧实现每轮给所有标签 seen_count+1，计数失去意义；现在只有
        新值才写。

        同一槽位（同一偏好对象 / 称呼）出现新值时，旧标签标成 corrected——
        卡片上不会同时出现「不吃辣」和「能吃辣」。
        """
        if state_keys is not None and not state_keys:
            return []
        profile = self.ensure(scope_id, account_id)
        conn = self.engine._conn()
        if state_keys is None:
            rows = conn.execute(
                "SELECT state_key, status, value_json FROM states"
                " WHERE scope_id=? AND status='known'",
                (scope_id,),
            ).fetchall()
        else:
            marks = ",".join("?" * len(state_keys))
            rows = conn.execute(
                "SELECT state_key, status, value_json FROM states"
                f" WHERE scope_id=? AND state_key IN ({marks})",
                (scope_id, *state_keys),
            ).fetchall()

        desired: dict[tuple[str, str], str | None] = {}   # (kind, slot) -> label
        for row in rows:
            parts = row["state_key"].split("|")
            if len(parts) < 4:
                continue
            subject, relation, obj = parts[1], parts[2], parts[3]
            if subject != account_id:
                continue
            value = _load_value(row["value_json"]) if row["status"] == "known" else None
            if relation == "nickname":
                desired[("address", "-")] = str(value) if value else None
            elif relation == "preference":
                slot = obj or "-"
                if value:
                    if str(value) == "过敏" and obj and obj != "-":
                        label = f"对{obj}过敏"
                    else:
                        label = f"{value}{obj}" if obj and obj != "-" else str(value)
                    desired[("preference", slot)] = label
                else:
                    desired[("preference", slot)] = None   # 撤回了
        if not desired and state_keys is not None:
            return []

        base_sql = ("SELECT tag_id, kind, value, slot FROM person_tags"
                    " WHERE profile_id=? AND source=? AND status IN (?, ?)"
                    " AND kind IN ('address', 'preference')")
        base_args = [profile["profile_id"], SOURCE_CONFIRMED, TAG_ACTIVE, TAG_STAGED]
        if state_keys is None:
            existing = conn.execute(base_sql, base_args).fetchall()
        else:
            # 增量：只取这几个槽位的标签，外加同值的老标签（没有 slot 列的）。
            slots = sorted({slot for _kind, slot in desired})
            labels = sorted({v for v in desired.values() if v}) or [""]
            existing = conn.execute(
                base_sql
                + f" AND (slot IN ({','.join('?' * len(slots))})"
                + f" OR (slot IS NULL AND value IN ({','.join('?' * len(labels))})))",
                (*base_args, *slots, *labels),
            ).fetchall()

        stale: list[str] = []
        have: set[tuple[str, str]] = set()
        wanted_labels = {(k[0], v) for k, v in desired.items() if v}
        for r in existing:
            key = (r["kind"], r["slot"]) if r["slot"] else None
            if key is not None and key in desired:
                if desired[key] == r["value"]:
                    have.add((r["kind"], r["value"]))
                else:
                    stale.append(r["tag_id"])
            elif key is None and state_keys is None:
                # 老库里没有 slot 的标签：全量对账时按值比对。
                if (r["kind"], r["value"]) in wanted_labels:
                    have.add((r["kind"], r["value"]))
                else:
                    stale.append(r["tag_id"])
            elif key is None and (r["kind"], r["value"]) in wanted_labels:
                have.add((r["kind"], r["value"]))
            elif key is not None and state_keys is None and key not in desired:
                # 全量对账：状态已经不在了（被撤回 / 不再 known）。
                stale.append(r["tag_id"])

        new_items = [(kind, slot, label) for (kind, slot), label in desired.items()
                     if label and (kind, label) not in have]
        if not new_items and not stale:
            return []

        got: list[dict] = []
        with self.engine._tx():
            now = self.engine.now()
            for tid in stale:
                conn.execute(
                    "UPDATE person_tags SET status=?, updated_at=? WHERE tag_id=?",
                    (TAG_CORRECTED, now, tid),
                )
            for kind, slot, label in new_items:
                tag = self.tag(scope_id, account_id, kind, label,
                               source=SOURCE_CONFIRMED, confidence=1.0)
                if tag.get("tag_id"):
                    conn.execute("UPDATE person_tags SET slot=? WHERE tag_id=?",
                                 (slot, tag["tag_id"]))
                got.append(tag)
        return got

    # ---------------------------------------------------------- 给主 AI 的卡
    def card(self, scope_id: str, account_id: str, *,
             limit: int = 8) -> dict:
        """渲染成一张卡。**这是画像唯一的对外出口。**

        每条标签都带 `source`，因为下游必须知道哪些能当事实用：
        点菜可以用 confirmed 的"不吃辣"，不能用 inferred 的。
        """
        profile = self.get(scope_id, account_id)
        if profile is None:
            return {"profile": None, "tags": [], "alerts": [], "suspect": False}

        tags = self.tags(scope_id, account_id, limit=limit)
        return {
            "profile": profile,
            "tags": tags,
            "alerts": self.open_alerts(scope_id, account_id),
            "suspect": profile["status"] == PROFILE_SUSPECT,
        }

    def render(self, scope_id: str, account_id: str, *, limit: int = 8) -> str:
        """人话版。给主 AI 塞进提示词用。

        刻意把 source 写在每条后面：让模型自己看见
        「这条是猜的」。不写的话，它会把推断当成事实用。
        """
        got = self.card(scope_id, account_id, limit=limit)
        if not got["profile"]:
            return "（还不认识这个人）"
        lines = []
        if got["suspect"]:
            lines.append("注意：这个账号当前有换人线索，别把他当同一个人。")
        for t in got["tags"]:
            mark = {"confirmed": "他说过", "observed": "见过",
                    "inferred": "我猜"}[t["source"]]
            lines.append(f"- {mark}：{_kind_name(t['kind'])}{t['value']}")
        return "\n".join(lines) if lines else "（还不了解这个人）"

    # ---------------------------------------------------------- 异常
    def suspect(self, scope_id: str, account_id: str, *,
                kind: str, signals: dict,
                evidence_trace_id: str | None = None) -> dict:
        """把画像标成可疑，并开一条 alert。

        **不判身份，只摆线索**（4.5）。谁来判断？主 AI。
        这一层只做两件事：记下线索、把新标签拦进暂存区。
        """
        profile = self.ensure(scope_id, account_id)
        now = self.engine.now()
        self.engine._conn().execute(
            "UPDATE person_profiles SET status=?, updated_at=? WHERE profile_id=?",
            (PROFILE_SUSPECT, now, profile["profile_id"]),
        )
        alert_id = "al_" + digest("alert", profile["profile_id"], kind,
                                  now, str(_next_seq(self)))[:20]
        self.engine._conn().execute(
            "INSERT INTO profile_alerts(alert_id, scope_id, profile_id, kind,"
            " signals_json, status, evidence_trace_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (alert_id, scope_id, profile["profile_id"], kind,
             canonical_json(signals), "open", evidence_trace_id, now),
        )
        return self._alert_by_id(alert_id) or {}

    def open_alerts(self, scope_id: str, account_id: str) -> list[dict]:
        profile = self.get(scope_id, account_id)
        if profile is None:
            return []
        rows = self.engine._conn().execute(
            "SELECT * FROM profile_alerts WHERE profile_id=? AND status='open'"
            " ORDER BY created_at DESC",
            (profile["profile_id"],),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def evaluate(self, alert_id: str, verdict: str, reasons: list[str],
                 actor_id: str) -> dict:
        """主 AI 给出结论。三种：同一个人 / 不是 / 判断不了。

        `unknown` 是**合法答案**，而且很常见——
        「这件事我先不定，但我会注意」比硬判一个强。
        """
        if verdict not in ("same_person", "different_person", "unknown"):
            raise ValueError(f"未知结论: {verdict}")
        now = self.engine.now()
        eval_id = "ev_pf_" + digest("eval", alert_id, verdict, now)[:16]
        self.engine._conn().execute(
            "INSERT INTO profile_evaluations(evaluation_id, alert_id, verdict,"
            " reasons_json, actor_id, created_at) VALUES (?,?,?,?,?,?)",
            (eval_id, alert_id, verdict, canonical_json(reasons), actor_id, now),
        )
        self.engine._conn().execute(
            "UPDATE profile_alerts SET status=?, resolved_at=? WHERE alert_id=?",
            ("escalated" if verdict == "different_person" else "dismissed",
             now, alert_id),
        )
        if verdict == "same_person":
            self._promote_staged(alert_id)
        return {"evaluation_id": eval_id, "verdict": verdict}

    def restore(self, scope_id: str, account_id: str) -> None:
        """确认是同一个人 → 画像回到正常，暂存区里的标签合进来。"""
        profile = self.get(scope_id, account_id)
        if profile is None:
            return
        self.engine._conn().execute(
            "UPDATE person_profiles SET status=?, updated_at=? WHERE profile_id=?",
            (PROFILE_ACTIVE, self.engine.now(), profile["profile_id"]),
        )
        self.engine._conn().execute(
            "UPDATE person_tags SET status=?, updated_at=?"
            " WHERE profile_id=? AND status=?",
            (TAG_ACTIVE, self.engine.now(), profile["profile_id"], TAG_STAGED),
        )

    def _promote_staged(self, alert_id: str) -> None:
        row = self._alert_by_id(alert_id)
        if row is None:
            return
        profile = self.engine._conn().execute(
            "SELECT * FROM person_profiles WHERE profile_id=?",
            (row["profile_id"],),
        ).fetchone()
        if profile is None:
            return
        self.restore(profile["scope_id"], profile["account_id"])

    def _alert_by_id(self, alert_id: str) -> dict | None:
        row = self.engine._conn().execute(
            "SELECT * FROM profile_alerts WHERE alert_id=?", (alert_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    # ---------------------------------------------------------- 授权
    @staticmethod
    def can_authorize(tag: dict) -> bool:
        """这条标签能不能作为**授权**依据（第十一章）。

        只有 confirmed 能。理由：
        「他说话爱带哈」这种观察，可以让我聊天时放松点；
        但它不能用来判定"这个人可以看私人记忆"。
        **语气可以随便调，权限不行。**
        """
        return tag.get("source") == SOURCE_CONFIRMED


def _rank(source: str) -> int:
    """来源的强度顺序。数字越大越可信。"""
    return {SOURCE_CONFIRMED: 2, SOURCE_OBSERVED: 1, SOURCE_INFERRED: 0}.get(source, 0)


def _load_value(raw: str | None):
    """states.value_json 是一个 JSON 标量。读不出来就当没有。"""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


# 明说不是本人。这几句本身就是线索，不需要别的证据。
_NOT_SELF = (
    "我是他同事", "我是她同事", "我是他朋友", "我是他哥", "我是他弟",
    "我不是本人", "他不在", "她不在", "我替他", "我帮她", "借用一下",
    "这不是我的号", "不是我的账号",
)


def _denies_being_self(text: str) -> bool:
    return any(p in text for p in _NOT_SELF)


_KIND_NAMES = {
    "address": "称呼是",
    "preference": "偏好",
    "habit": "习惯",
    "style": "说话方式",
    "topic": "常聊",
    "relation": "关系",
}


def _kind_name(kind: str) -> str:
    return _KIND_NAMES.get(kind, kind)


def _next_seq(store: "ProfileStore") -> int:
    """画像层也需要一个进程内序号：同一秒内可能连提两个同类标签，
    只靠时间戳做 id 会撞。"""
    store._seq = getattr(store, "_seq", 0) + 1
    return store._seq


def parse_signals(raw: str | None) -> dict:
    """alert 里的 signals_json 读回来。"""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
