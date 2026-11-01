"""准入等级与熔断。

两件事，一个文件：

1. **晋级**：哪些句式可以不经用户点头就落库。
   默认只放开三类"记账三原语"——有数量、无第三方、无歧义的
   `incur / repay / waive`。其余全部 assisted。
   依据是 G0 矩阵：`数量×口语` 100%、`数量×省略` 92%、`数量×指代` 91%，
   而 `归属` 只有 60%——所以"带第三方"的一律不放开。

2. **熔断**：放开的格子一旦连续被纠正，自动收回。
   这是"敢开"的前提——没有熔断的 auto 是赌博。

    closed ──失败×3──► open ──确认×5──► closed
      ▲                  │
      └──────────────────┘

**为什么不用"连续正确 N 次就放开"**：没有线上抽检就攒不出"正确"的证据，
而"正确"是可以自证的（提交成功≠对）。但**"被纠正"是不可辩驳的证据**——
用户专门回来说"不是这个"，那是系统错了。所以晋级靠人工判断（白名单），
熔断靠用户行为（自动）。

这条取舍写在这里，是因为它决定了：**系统会自己变紧，不会自己变松。**
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .engine import Engine, canonical_json


# 默认放开的句式。加一个进来要能回答两个问题：
#   1. 这句话的语义有几种可能？（只有一种才能放开）
#   2. 错了会怎样？（会改变余额，但改错能立刻看出来）
DEFAULT_AUTO = frozenset({
    "incur:plain",     # 我欠你三袋
    "repay:plain",     # 还你两袋
    "waive:plain",     # 免一袋
})

# 永远不放开的。即使有人往 DEFAULT_AUTO 里加，这几个也挡在前面。
NEVER_AUTO = frozenset({
    "correct:revision",    # 改历史
    "retract:revision",    # 撤历史
    "set_total:revision",  # 改写总额
})


class AdmissionController:
    """按 (关系, 句式) 决定准入等级，并维护熔断状态。"""

    FAIL_THRESHOLD = 3     # 连续被纠正几次 → 熔断
    RELEASE_AFTER = 5      # 熔断后连续确认几次 → 恢复

    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------ 分类
    @staticmethod
    def pattern_of(op: str, has_qty: bool, signals: dict[str, Any],
                   modality: str = "asserted") -> str:
        """这句话属于哪一类风险。**顺序就是优先级。**

        先看"会不会改历史"（最危险），再看"施事/语气/性质"，
        最后才看"有没有数量"。反过来的话，「替老张赊了三袋」
        会因为"有数量"被归到 plain 里——而它恰恰是最容易记错人的那一类。
        """
        if op in ("correct", "retract", "set_total"):
            return f"{op}:revision"
        # 请求不是事实。「给我免三碗」是"我想要"，不是"已经免了"——
        # 放它自动提交，等于替对方答应了一件事。
        if modality in ("request", "planned"):
            return f"{op}:request"
        if signals.get("third_party"):
            return f"{op}:third_party"
        if signals.get("settled_report") or signals.get("balance_check"):
            return f"{op}:check"
        if signals.get("seeking_agreement"):
            return f"{op}:check"
        if signals.get("describe_only"):
            return f"{op}:describe"
        if op == "promise":
            return "promise:plain"      # 不改余额，本来就 record_only
        if not has_qty:
            return f"{op}:noqty"        # 没数量，多半要问
        return f"{op}:plain"

    def _default_level(self, pattern: str) -> str:
        if pattern in NEVER_AUTO:
            return "assisted"
        return "auto" if pattern in DEFAULT_AUTO else "assisted"

    # ------------------------------------------------------------ 查询
    def state_of(self, relation: str, pattern: str) -> dict[str, Any]:
        row = self.engine._conn().execute(
            "SELECT * FROM admission_state WHERE relation=? AND pattern=?",
            (relation, pattern),
        ).fetchone()
        if row:
            return dict(row)
        return {
            "relation": relation, "pattern": pattern,
            "level": self._default_level(pattern),
            "failures": 0, "successes": 0, "opened_at": None,
        }

    def level(self, relation: str, pattern: str, spec_mode: str,
              escalate: bool = True) -> str:
        """最终等级。**规格是上限，不是下限。**

        spec 说 shadow，就永远 shadow——句式再稳也不能越过关系的授权级别。
        spec 说 assisted，白名单里的句式可以升到 auto；
        白名单外的停留在 assisted。
        """
        if spec_mode == "shadow":
            return "shadow"

        # 熔断优先级最高：格子被关掉就是关掉，规格说 auto 也不给开。
        # 这是"熔断"这个词的全部意义——**它必须能压过配置**，
        # 否则一次配置错误就再也收不回来。
        if self.state_of(relation, pattern).get("opened_at"):
            return "assisted"

        if spec_mode == "auto":
            return "auto"

        # 规格是 assisted：只有白名单里的句式能升。
        # escalate=False 用于回放/测试——那两种场景要的是"规则原样"，
        # 不是"当前部署的准入配置"。
        if not escalate:
            return "assisted"
        return "auto" if pattern in DEFAULT_AUTO else "assisted"

    # ------------------------------------------------------------ 熔断
    def record_failure(
        self, scope_id: str, relation: str, pattern: str, reason: str
    ) -> bool:
        """记一次"被纠正"。返回 True 表示**这一次把格子关掉了**。"""
        st = self.state_of(relation, pattern)
        failures = int(st.get("failures") or 0) + 1
        now = self.engine.now()
        opened = st.get("opened_at")

        if failures >= self.FAIL_THRESHOLD and not opened:
            opened = now
            self._upsert(relation, pattern, "assisted", failures, 0, opened)
            self._log(scope_id, relation, pattern, "open",
                      f"连续 {failures} 次被纠正：{reason}")
            return True

        self._upsert(relation, pattern, st.get("level") or "assisted",
                     failures, int(st.get("successes") or 0), opened)
        self._log(scope_id, relation, pattern, "failure", reason)
        return False

    def record_success(self, relation: str, pattern: str) -> bool:
        """记一次"用户确认了"。只在熔断后计数，用于恢复。返回 True 表示恢复了。"""
        st = self.state_of(relation, pattern)
        if not st.get("opened_at"):
            return False                       # 没熔断就不用记
        successes = int(st.get("successes") or 0) + 1
        if successes >= self.RELEASE_AFTER:
            self._upsert(relation, pattern, self._default_level(pattern), 0, 0, None)
            self._log("", relation, pattern, "close",
                      f"连续 {successes} 次确认，恢复")
            return True
        self._upsert(relation, pattern, "assisted", int(st.get("failures") or 0),
                     successes, st["opened_at"])
        return False

    # ------------------------------------------------------------ 存储
    def _upsert(self, relation: str, pattern: str, level: str,
                failures: int, successes: int, opened_at: str | None) -> None:
        # 走 engine._tx()：它可重入（SAVEPOINT），所以在 _apply_revision
        # 的事务里调用也不会炸。**不能自己 commit**——那会把外层事务提前提交。
        with self.engine._tx() as conn:
            conn.execute(
            "INSERT INTO admission_state(relation, pattern, level, failures,"
            " successes, opened_at, updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(relation, pattern) DO UPDATE SET"
            " level=excluded.level, failures=excluded.failures,"
            " successes=excluded.successes, opened_at=excluded.opened_at,"
            " updated_at=excluded.updated_at",
                (relation, pattern, level, failures, successes, opened_at,
                 self.engine.now()),
            )

    def _log(self, scope_id: str, relation: str, pattern: str,
             action: str, reason: str) -> None:
        with self.engine._tx() as conn:
            conn.execute(
            "INSERT INTO breaker_log(log_id, scope_id, relation, pattern,"
            " action, reason, at) VALUES (?,?,?,?,?,?,?)",
                (f"bl_{abs(hash((relation, pattern, action, self.engine.now()))) % 10**12}",
                 scope_id or "-", relation, pattern, action, reason, self.engine.now()),
            )

    # ------------------------------------------------------------ 读
    def snapshot(self) -> list[dict[str, Any]]:
        rows = self.engine._conn().execute(
            "SELECT * FROM admission_state ORDER BY relation, pattern"
        ).fetchall()
        return [dict(r) for r in rows]

    def log(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.engine._conn().execute(
            "SELECT * FROM breaker_log ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
