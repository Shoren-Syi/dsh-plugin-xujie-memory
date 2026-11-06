"""命令行入口：init / doctor / stats / remember / recall / serve。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Any
from pathlib import Path

from . import __version__, log
from .config import default_config_text, load_config
from .core import backup as backup_mod
from .core import migrate as mig
from .core.db import connect, supports_trigram
from .core.store import Store

PERSONA_TEMPLATE = """# 我是谁

（在这里写 AI 的人设。这段会**每轮**都进上下文，请控制在 500 token 以内。）

## 行为准则

- 不确定的事，问一次就好，问完不再纠结。
- 不知道就说不知道。人也会忘事，这不是缺陷。
- 做不到的事可以直说，不要硬编。
"""


# ---------------------------------------------------------------- 工具函数
def _config_for(args: argparse.Namespace):
    """--dir 指定的目录里如果有 config.yaml，就用它。

    不这么做的话 `hippo init --dir /tmp/x` 建好的配置，
    `hippo serve --dir /tmp/x` 会视而不见，去读 ~/.hippo/config.yaml ——
    用户改了半天配置不生效，还找不到原因。
    """
    explicit = getattr(args, "config", None)
    if explicit:
        return load_config(explicit)
    d = getattr(args, "dir", None)
    if d:
        candidate = Path(d).expanduser() / "config.yaml"
        if candidate.exists():
            return load_config(candidate)
    return load_config()


def _db_path(args: argparse.Namespace) -> Path:
    if getattr(args, "dir", None):
        return Path(args.dir).expanduser() / "mem.db"
    try:
        return _config_for(args).db_file
    except Exception:
        return Path.home() / ".hippo" / "mem.db"


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir).expanduser() if args.dir else Path.home() / ".hippo"
    target.mkdir(parents=True, exist_ok=True)

    cfg_file = target / "config.yaml"
    if not cfg_file.exists():
        text = default_config_text().replace("~/.hippo/mem.db", str(target / "mem.db"))
        cfg_file.write_text(text, encoding="utf-8")
        print(f"  配置  {cfg_file}")
    else:
        print(f"  配置  {cfg_file} (已存在，跳过)")

    persona = target / "persona.md"
    if not persona.exists():
        persona.write_text(PERSONA_TEMPLATE, encoding="utf-8")
        print(f"  人设  {persona}")

    db = target / "mem.db"
    conn = connect(db)
    try:
        applied = mig.migrate(conn)
        ver = mig.current_version(conn)
    finally:
        conn.close()
    if applied:
        print(f"  数据库 {db}  (应用迁移 {applied} → v{ver})")
    else:
        print(f"  数据库 {db}  (已是最新 v{ver})")

    if args.force_rebuild and db.exists():
        pass
    print("\n完成。下一步：hippo serve --port 8787")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python ≥ 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))

    sv = sqlite3.sqlite_version_info
    checks.append(("SQLite ≥ 3.34", sv >= (3, 34), sqlite3.sqlite_version))

    try:
        fts = supports_trigram()
    except Exception:  # noqa: BLE001
        fts = False
    checks.append(("FTS5 trigram 可用", fts, "ok" if fts else "不可用，需重编 SQLite"))

    db = _db_path(args)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db)
        ver = mig.current_version(conn)
        conn.close()
        checks.append(("数据库可读写", True, f"{db} v{ver}"))
    except Exception as e:  # noqa: BLE001
        checks.append(("数据库可读写", False, str(e)))

    try:
        import fastapi
        import httpx
        import uvicorn

        checks.append(("代理依赖", True,
                       f"fastapi {fastapi.__version__} / uvicorn {uvicorn.__version__} / "
                       f"httpx {httpx.__version__}"))
    except ImportError as e:  # noqa: BLE001
        checks.append(("代理依赖", False,
                       f"缺 {e.name}：pip install 'hippocampus-deepseek[server]'"))

    try:
        _config_for(args)
        checks.append(("配置合法", True, "ok"))
    except Exception as e:  # noqa: BLE001
        checks.append(("配置合法", False, str(e).splitlines()[0]))

    # 词表坏了不会报错，只会静默地什么都不记——那是最难查的故障
    try:
        from .core.prescreen import load_rules

        cfg = _config_for(args)
        rules = load_rules(cfg.extract.lexicon_path or None)
        total = len(rules.plan) + len(rules.preference) + len(rules.fact)
        checks.append(("预筛词表", total >= 20, f"{total} 条规则 / {rules.source}"))
    except Exception as e:  # noqa: BLE001
        checks.append(("预筛词表", False, str(e).splitlines()[0]))

    # prompt 模板漏打包的话，抽取会静默退化成兜底简版：
    # 不报错、不崩，只是抽得差。这是最难查的一类故障，必须在自检里拦住。
    try:
        from .core.schema import PROMPT_TEMPLATE, load_template

        tpl = load_template()
        is_full = tpl != PROMPT_TEMPLATE
        checks.append(("抽取 prompt", is_full and "json" in tpl.lower(),
                       f"{len(tpl)} 字符" + ("" if is_full else "（用的是兜底简版！）")))
    except Exception as e:  # noqa: BLE001
        checks.append(("抽取 prompt", False, str(e).splitlines()[0]))

    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        print(f"  {'[OK]' if ok else '[FAIL]'}  {name.ljust(width)}  {detail}")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 1


def cmd_recompute(args: argparse.Namespace) -> int:
    """每日重算权重与 tier（M14.2）。

    `--explain` 单独给一条事件拆公式：调参时最常问的是"为什么它掉下去了"，
    没有这个就得写脚本 import compute_weight。
    """
    from .core.dynamics import recompute
    from .core.weight import explain

    cfg = _config_for(args)
    store = Store.open(_db_path(args), cfg)
    try:
        if args.explain:
            ev = store.get_event(int(args.explain))
            if ev is None:
                print(f"没有 id={args.explain} 的事件")
                return 2
            d = explain(ev, None, cfg=cfg.weight)
            print(f"#{ev.id}  {ev.summary or ev.raw[:40]}")
            print(f"  w0            {d['w0']:.4f}   写入时定的，之后不再变")
            print(f"  距上次想起     {d['age_days']:.1f} 天")
            print(f"  基准半衰期     {d['base_half_life']:.0f} 天（按 w0 分档）")
            print(f"  召回 {d['recall']} 次 → 半衰期 {d['half_life']:.0f} 天")
            print(f"  衰减系数      {d['decay']:.4f}")
            print(f"  → weight      {d['weight']:.4f}   tier={d['tier']}")
            return 0

        rep = recompute(store, cfg=cfg.weight, dry_run=args.dry_run,
                        session=args.session or None, limit=args.limit)
        tag = "（试算，未写库）" if rep.dry_run else ""
        print(f"重算完成{tag}：{rep.line()}")
        print(f"  变动 {rep.changed} 条　weight {rep.w_min:.3f}/{rep.w_avg:.3f}/{rep.w_max:.3f}"
              f"（min/avg/max）")
        print(f"  迁移 {rep.matrix()}")
        if rep.pinned_bad:
            print(f"  [!] pinned 却不是 hot 的 {rep.pinned_bad} 条（应为 0）")
        return 0
    finally:
        store.close()


class _Summary:
    """把 `Encoder.chat` 包成同步回调，**复用同一个事件循环**。

    每簇各 `asyncio.run()` 会重复建 loop，上千簇时开销可观，而且
    httpx 的连接池会跟着重建——巩固是夜间批量跑，白白多付 TLS 握手。
    """

    def __init__(self, cfg: Any, key: str):
        import asyncio
        import httpx

        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout,
                                  write=30.0, pool=10.0),
        )
        from .core.encode import Encoder
        self.encoder = Encoder(cfg, self.client, key)

    def __call__(self, messages: list[dict[str, str]]):
        return self.loop.run_until_complete(self.encoder.chat(messages))

    def close(self) -> None:
        try:
            self.loop.run_until_complete(self.client.aclose())
        finally:
            self.loop.close()


def cmd_consolidate(args: argparse.Namespace) -> int:
    """夜间巩固（M15）：合并重复、归档冷记忆。

    `--no-summary` 不是"降级"，是**默认行为的另一条正常路径**：
    关键词并集已经保证检索不丢，重生成摘要只是让它更好读。
    没有 API Key 时自动走这条路，不打一个"fmt 警告"然后失败。
    """
    from .core.consolidate import dup_rate, run

    cfg = _config_for(args)
    store = Store.open(_db_path(args), cfg)
    summ = None
    try:
        sessions = ([args.session] if args.session else store.event_sessions())
        if not sessions:
            print("库里还没有会话")
            return 0

        key = "" if args.no_summary else cfg.resolve_api_key(None)
        if key:
            summ = _Summary(cfg, key)
        elif not args.no_summary:
            print("（没有 API Key，只做结构合并，不重生成摘要）")

        total_merged = 0
        for s in sessions:
            before = dup_rate(store, s)
            rep = run(store, s, dry_run=args.dry_run, summarizer=summ,
                      pricing=cfg.pricing.model_dump())
            after = dup_rate(store, s)
            tag = "（试算，未写库）" if args.dry_run else ""
            print(f"[{s}]{tag} {rep.line()}")
            print(f"    可合并比例 {before:.1%} → {after:.1%}"
                  f"　摘要 {rep.summary_ok} 成 / {rep.summary_fail} 败"
                  f"　花费 ¥{rep.cost:.4f}")
            total_merged += rep.merged

        st = store.consolidation_stats()
        if st["batches"]:
            print(f"\n累计：{st['batches']} 批，合并 {st['merged']} 条"
                  f"（强 {st['strong']} / 弱 {st['weak']}）"
                  + (f"，已撤销 {st['undone']} 条" if st["undone"] else ""))
        return 0
    finally:
        if summ is not None:
            summ.close()
        store.close()


def cmd_unmerge(args: argparse.Namespace) -> int:
    """撤销一次合并。弱判据合并专用（strong=0 的那批）。

    强判据合并也允许撤销——拒绝它没有理由，只是正常不该用到。
    真正该拒绝的是"撤销一条没被合并过的记录"，那说明用户搞错了 id。
    """
    cfg = _config_for(args)
    store = Store.open(_db_path(args), cfg)
    try:
        freed = store.unmerge(int(args.id))
        if not freed:
            print(f"#{args.id} 上没有可撤销的合并（可能 id 不对，或已经撤过）")
            return 2
        print(f"已放回 {len(freed)} 条：{freed}")
        print("注意：摘要与关键词不回滚——并集是无损的，摘要是后续可能已被改写。")
        return 0
    finally:
        store.close()


def cmd_pin(args: argparse.Namespace) -> int:
    """钉住一条记忆：永不衰减、永远进 C 段。

    为什么要有手动入口：自动识别只认祈使句（"记住…"），而用户的真实意图
    经常藏在别处——"这件事你千万别忘了"命中，"我刚才说的都挺重要的"
    不命中。手动的 `--event` 是兜底，也是自动识别误判时的修正手段。

    `--off` 不把 tier 改回 warm，交给下一次 recompute：手改等于替公式做决定，
    而且改错方向（该冷的不冷）比不改更糟。
    """
    from .core.drill import format_event
    from .core.turns import estimate_tokens

    store = Store.open(_db_path(args), _config_for(args))
    try:
        if args.event:
            if store.get_event(args.event) is None:
                print(f"没有找到事件 {args.event}")
                return 1
            store.set_pinned(args.event, not args.off)
            e = store.get_event(args.event)
            state = "已撤销钉住" if args.off else "已钉住"
            print(f"{state} #{args.event}：{e.raw[:40] if e else ''}")
            if not args.off and e is not None:
                print("它现在会以 hot 档注入（关键词 + 摘要 + 原文）。")
            return 0

        items = store.list_pinned(args.session)
        if not items:
            print("还没有钉住任何记忆。用 `hippo pin --event N` 钉一条。")
            return 0
        print(f"钉住 {len(items)} 条（永不衰减，每轮都进 C 段）：")
        for e in items:
            print(f"  #{e.id:<4} {e.raw[:50]}")
        used = sum(estimate_tokens(format_event(e)) for e in items)
        print(f"\n合计约 {used} token（C 段预算 200）"
              f"，超了会被裁。清理用 `hippo pin --event N --off`。")
        return 0
    finally:
        store.close()


def cmd_curiosity(args: argparse.Namespace) -> int:
    """好奇队列：它想问什么、今天问过几次、问完有没有人接。

    `--list` 走的是**和线上完全同一条** build_queue：CLI 里另写一套
    "仅供参考"的排序，看的时候是一个样，跑起来是另一个样。
    """
    from .core.curiosity import Curiosity, build_queue, format_queue, format_stats

    cfg = _config_for(args)
    store = Store.open(_db_path(args), cfg)
    try:
        sessions = ([args.session] if args.session
                    else store.list_sessions() or ["main"])

        if args.list:
            for s in sessions:
                q = build_queue(store, s, cfg=cfg,
                                limit=getattr(cfg.curiosity, "queue_size", 5))
                print(f"=== {s} ===")
                print(format_queue(q))
            return 0

        if args.tick:
            from .core.curiosity import Curiosity as _C
            eng = _C(store, cfg)
            n = eng.tick()
            print(f"准备 {n} 条（入队不算发言，额度在说出口时才扣）")
            return 0

        eng = Curiosity(store, cfg)
        for s in sessions:
            st = eng.stats(s)
            print(f"=== {s} ===")
            print(format_stats(st, store.curiosity_rows(
                s, limit=5, since=eng.quota.day_bounds()[0])))
        return 0
    finally:
        store.close()


def cmd_archive(args: argparse.Namespace) -> int:
    """归档冷记忆（M15.3）。**物理搬走，不是复制一份再标记**。

    为什么要搬：手册要求"主库体积降 ≥20%"。复制 + `archived=1` 的话，
    `raw` 还在主库，一个字节都不会少，而 raw 正是体积的大头。
    """
    from datetime import datetime as _dt

    cfg = _config_for(args)
    db = _db_path(args)
    store = Store.open(db, cfg)
    try:
        adir = (Path(args.archive_dir).expanduser() if args.archive_dir
                else db.parent / "archive")
        now = int(_dt.now().timestamp())
        ids = store.archive_candidates(
            max_weight=args.max_weight, idle_days=args.idle_days,
            now=now, limit=args.limit)
        if not ids:
            print(f"没有够格归档的事件（条件：cold · weight<{args.max_weight}"
                  f" · {args.idle_days} 天没被想起）")
            return 0
        before = store.db_bytes(live=False)
        if args.dry_run:
            print(f"[试算] 够格 {len(ids)} 条，预计主库 "
                  f"{before / 1024:.0f}KB → 约 {before * 0.5 / 1024:.0f}KB")
            return 0

        target = adir / f"mem-{_dt.now().year}.db"
        r = store.archive_to(ids, target, now=now, vacuum=not args.no_vacuum)
        drop = (1 - r["file_size"] / before) if before else 0.0
        print(f"归档 {r['moved']} 条 → {target}")
        print(f"  主库 {before / 1024:.0f}KB → {r['file_size'] / 1024:.0f}KB"
              f"（降 {drop:.0%}）"
              + ("　[跳过 VACUUM，文件未收缩]" if args.no_vacuum else ""))
        print("  还原：hippo restore --id <事件id>"
              + (f" --archive-dir {adir}" if args.archive_dir else ""))
        return 0
    finally:
        store.close()


def cmd_backup(args: argparse.Namespace) -> int:
    """生成一份一致快照到 <库目录>/backups/，保留最近 keep 份（M28.3）。"""
    db = _db_path(args)
    bdir = Path(args.backups_dir).expanduser() if args.backups_dir else None
    try:
        target = backup_mod.backup(db, keep=args.keep, backups_dir=bdir)
    except FileNotFoundError as exc:
        print(f"{exc}\n先运行：hippo init", file=sys.stderr)
        return 1
    files = backup_mod.list_backups(target.parent)
    size = target.stat().st_size
    print(f"备份完成 → {target}")
    print(f"  {size / 1024:.0f}KB　现有备份 {len(files)} 份"
          f"（保留最近 {args.keep} 份）")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """恢复整库（--latest / --backup）或从归档库拉回单条（--id，原 id 不变）。"""
    db = _db_path(args)

    if args.latest or args.backup:
        if args.id:
            print("--id 与 --latest/--backup 互斥：前者拉单条事件，"
                  "后者还原整个数据库。", file=sys.stderr)
            return 2
        bdir = (Path(args.backups_dir).expanduser() if args.backups_dir
                else backup_mod.backups_dir_for(db))
        src = Path(args.backup).expanduser() if args.backup else backup_mod.latest_backup(bdir)
        if src is None:
            print(f"没有找到任何备份（找过 {bdir}）。先运行：hippo backup",
                  file=sys.stderr)
            return 2
        try:
            backup_mod.restore(db, src)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"已从备份恢复 → {db}")
        print(f"  来源 {src}")
        print(f"  旧库改名 {db.name}.pre-restore-*.bak 留作后悔药")
        print("  下一步：hippo doctor")
        return 0

    if not args.id:
        print("需要 --id（归档库拉单条）或 --latest / --backup（还原整库）。",
              file=sys.stderr)
        return 2

    cfg = _config_for(args)
    store = Store.open(db, cfg)
    try:
        adir = (Path(args.archive_dir).expanduser() if args.archive_dir
                else db.parent / "archive")
        got = store.restore_from([args.id], adir)
        if not got:
            print(f"归档库里没有 #{args.id}（找过 {adir}）")
            return 2
        ev = store.get_event(got[0])
        print(f"已恢复 #{got[0]}：{(ev.raw if ev else '')[:40]}")
        return 0
    finally:
        store.close()


def cmd_stats(args: argparse.Namespace) -> int:
    store = Store.open(_db_path(args), _config_for(args))
    cfg_stats = _config_for(args)
    try:
        # 降级是"看起来正常、其实在省电"的状态，必须主动报出来。
        # 只在超限时打一行，不超限不占版面；`--budget` 看详情。
        try:
            from .core.budget import check as budget_check
            st = budget_check(store, cfg_stats)
            if st.degraded:
                print(f"预算降级中：今日 {st.used:,} / {st.limit:,} token"
                      f"（{st.pct:.0f}%），停止抽取、记忆暂停。"
                      f"次日 00:00 自动恢复，或 `hippo budget reset`。\n")
            if getattr(args, "budget", False):
                from .core.budget import format_status
                print("预算")
                for line in format_status(st).splitlines():
                    print(f"  {line}")
                print()
        except Exception as exc:  # noqa: BLE001
            print(f"预算状态读取失败：{exc}")
        counts = store.counts()
        print("表行数")
        for k, v in counts.items():
            print(f"  {k:<12} {v:>8}")
        dist = store.tier_distribution()
        if dist:
            total = sum(dist.values())
            print("\n分层分布")
            for tier in (1, 2, 3):
                n = dist.get(tier, 0)
                pct = n / total * 100 if total else 0
                label = {1: "hot", 2: "warm", 3: "cold"}[tier]
                print(f"  {label:<6} {n:>8}  {pct:5.1f}%")
        try:
            runs = store.run_stats()
            if runs.get("total"):
                print("\n抽取")
                print(f"  窗口      {runs['total']}")
                print(f"  成功率    {runs['success_rate'] * 100:.1f}%"
                      f"  (ok {runs.get('ok', 0)} / degraded {runs.get('degraded', 0)}"
                      f" / skipped {runs.get('skipped', 0)})")
                print(f"  原文丢弃  {runs['verbatim_fail']} 条（raw 锚不上）")
                print(f"  缓存命中  {runs.get('cache_hit_rate', 0) * 100:.1f}%"
                      f"  ({runs.get('cached_tokens', 0)}/{runs.get('prompt_tokens', 0)} token)")
                print(f"  累计花费  ¥{runs['cost']:.4f}")

            # 摘要质量标记：截断和"修不好"分开报，因为它们指向不同的下一步
            flags = store.summary_flag_totals()
            if flags["n"]:
                print(f"\n摘要  {flags['n']} 条")
                print(f"  被截断    {flags['truncated']} 条（内容丢了半句）")
                print(f"  已知不合格 {flags['flagged']} 条（长度/实体/人称没过，重写一次仍不行）")
        except Exception as exc:  # noqa: BLE001
            # ★ 不能静默 pass。之前这里吞掉了 "no such column: cached_tokens"，
            #   表现是"抽取这一段凭空消失"，看着像"这个库没抽过"，
            #   而其实是有 3 条记录的。报表少一段，比报表报错危险得多。
            print(f"\n抽取统计读取失败：{exc}")
            print("  提示：库可能是旧版本，运行 `hippo migrate` 后再试")

        db = _db_path(args)
        if db.exists():
            print(f"\n库文件 {db}  {db.stat().st_size / 1024:.0f} KB")
        if getattr(args, "giveup", False):
            from .core.giveup import format_stats
            rows = store.giveup_stats(
                session=getattr(args, "session", None) or None)
            print("\n能力边界（弃权记录）")
            for line in format_stats(rows).splitlines():
                print(f"  {line}")
        if getattr(args, "heartbeat", False):
            print("\n心跳")
            raw = store.get_state("_sys", "hb:last_tick", "0")
            try:
                last = int(raw)
            except ValueError:
                last = 0
            if last:
                age = max(0, int(time.time()) - last)
                print(f"  最近一次   {time.strftime('%H:%M:%S', time.localtime(last))}"
                      f"（{age} 秒前）")
            else:
                print("  最近一次   从未（服务没起来过，或库是新的）")
            cfg = _config_for(args)
            from .core.heartbeat import Escalator
            esc = Escalator(store, cfg=cfg)
            print(f"  间隔       {cfg.heartbeat.interval_seconds}s")
            print(f"  今日调用   {esc.used()} / {cfg.heartbeat.max_llm_calls_per_day}")
            print(f"  各步       mood {cfg.heartbeat.mood_seconds}s"
                  f" / tier {cfg.heartbeat.tier_seconds}s"
                  f" / consolidate {cfg.heartbeat.consolidate_hour:02d}:00")
        if getattr(args, "ledger", False):
            from .core.ledger import format_stats as format_ledger
            session = getattr(args, "session", None) or ""
            print("\n奖励账簿（自主动作的预期 vs 结果）")
            text = format_ledger(store.ledger_stats(session),
                                 store.regrets(session, limit=5) if session else [])
            for line in text.splitlines():
                print(f"  {line}")
        if getattr(args, "disk", False):
            _print_disk(store, db)
    finally:
        store.close()
    return 0


def _print_disk(store, db: Path) -> None:
    """磁盘占用明细（`hippo stats --disk`）。

    三个数缺一不可，因为它们回答三个不同的问题：

        活数据  删掉了多少（归档/清理的效果）
        文件    现在占多少（要不要 VACUUM）
        freelist 白占多少（VACUUM 能收回多少）

    只报文件大小会得出"归档没生效"的结论——SQLite 删行不缩文件，被删的页
    进 freelist 等着复用。只报活数据会得出"VACUUM 没用"的结论。
    """
    st = store.page_stats()
    print(f"  页 {st['pages']} × {st['page_size']}B"
          f"   活数据 {st['live'] / 1024:.0f} KB"
          f"   空闲页 {st['freelist']}"
          f"（{st['freelist'] * st['page_size'] / 1024:.0f} KB 可回收）")
    adir = db.parent / "archive"
    files = sorted(adir.glob("mem-*.db")) if adir.exists() else []
    if not files:
        print("  归档库 无")
        return
    tot = sum(f.stat().st_size for f in files)
    print(f"  归档库 {len(files)} 个文件  {tot / 1024:.0f} KB"
          f"   主库 {st['total'] / 1024:.0f} KB")
    for f in files:
        print(f"    {f.name:<24} {f.stat().st_size / 1024:>7.0f} KB")


def cmd_budget(args: argparse.Namespace) -> int:
    """M27.3：看预算 / 手动恢复。

    `hippo budget reset` 只写一个时间戳，不删账。删 metrics 会让
    "这个月到底花了多少"永久失真，而重置要的只是"从此刻起重新算"。
    """
    from .core.budget import check as budget_check
    from .core.budget import format_status, reset as budget_reset

    store = Store.open(_db_path(args), _config_for(args))
    try:
        if args.action == "reset":
            ts = budget_reset(store)
            print(f"已重置：从 {time.strftime('%m-%d %H:%M:%S', time.localtime(ts))} 起重新计数"
                  f"（历史用量仍留在 metrics 里）")
        print(format_status(budget_check(store, _config_for(args))))
    finally:
        store.close()
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    store = Store.open(_db_path(args), _config_for(args))
    try:
        text = " ".join(args.text)
        tokens = args.keywords.split(",") if args.keywords else []
        eid = store.add_event(
            session=args.session, raw=text,
            summary=args.summary or text[:60],
            keywords=tokens or text.split()[:6],
            kind=args.kind, pinned=1 if args.pin else 0,
        )
        print(f"已写入事件 #{eid}")
    finally:
        store.close()
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    """一句话为什么被放行 / 挡掉。

    调词表时最常问的两个问题是"这条怎么没抽"和"这条凭什么抽"。
    没有这个命令，每次都得写一段 python 脚本去 import prescreen。
    """
    from .core.prescreen import EntityIndex, explain, load_rules

    text = " ".join(args.text)
    try:
        cfg = _config_for(args)
        rules = load_rules(cfg.extract.lexicon_path or None)
    except Exception:  # noqa: BLE001
        rules = load_rules()

    idx = None
    db = _db_path(args)
    if Path(db).exists():
        try:
            with Store.open(db) as store:
                idx = EntityIndex.from_store(store, args.session)
        except Exception:  # noqa: BLE001
            idx = None

    n = len(idx) if idx else 0
    print(explain(text, entities=idx, prev_assistant=args.prev, rules=rules))
    print(f"  文本长度 {len(text)}　实体词表 {n} 条　规则文件 {rules.source}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """调试检索。

    四档，从粗到细：
      --explain  只解释"这句话被读成了什么样"（谁 / 什么时候 / 要不要历史）
      --l1       跑 L1 分类层，看砍到多少条、花多久
      默认       跑 L1 → L2，看最终会进 prompt 的那 8 条
      --raw      再跑 L3，把这些条目真正要注入的文本打出来（含原话）
    """
    from .core.drill import SKIP_TEXT, format_event
    from .core.drill import drill
    from .core.rank import explain as l2_explain
    from .core.rank import l2_search
    from .core.retrieve import explain, l1_candidates, l1_facts, parse_query

    try:
        cfg = _config_for(args)
    except Exception as exc:  # noqa: BLE001
        # 配置坏了不该让 `recall` 整个不能用：它正是排查配置问题时要用的命令。
        # 降级成 None = 用代码里的默认值，并显式告诉用户"这不是你的配置"。
        print(f"配置读不出来（{exc}），这次用默认值跑。")
        cfg = None
    store = Store.open(_db_path(args), cfg)
    try:
        query = " ".join(args.query)
        q = parse_query(query, args.session, store)

        def _deep() -> None:
            """归档库查询。只在 --deep 时跑——它要 ATTACH 一遍所有归档文件。

            输出带 id 和文件名，因为下一步动作一定是 `hippo restore --id`。
            """
            if not args.deep:
                return
            hits = store.search_archive(query, limit=5)
            if not hits:
                print("--- 归档库 --- 没命中（一年前的冷记忆里也没有）")
                return
            print(f"--- 归档库 --- 命中 {len(hits)} 条（不参与正常召回，"
                  f"要拉回来用 `hippo restore --id`）")
            for h in hits:
                day = time.strftime("%Y-%m-%d", time.localtime(h["event_time"]))
                print(f"  [{h['archived_id']}] {day} {h['raw'][:60]}")

        if args.explain:
            print(explain(q))

        if args.explain and not args.l1:
            return 0

        if args.l1:
            res = l1_candidates(store, args.session, q)
            print(explain(q, res))
            facts = l1_facts(store, args.session, q)
            if facts:
                print("facts：" + "  ".join(f"{f.key}={f.value}" for f in facts))
            _deep()
            return 0

        res = l2_search(store, args.session, query, query=q, limit=args.limit)
        out = drill(store, res, text=query) if args.raw else None
        print(l2_explain(res, drilled=out))
        if out is not None:
            if not out.drilled:
                why = out.skipped[0][1] if out.skipped else "empty"
                print(f"（原文层未触发：{SKIP_TEXT.get(why, why)}）")
            # 注入预览：这就是 M13 会拼进 prompt 的东西。调试时唯一可信的
            # "模型到底看到了什么"，比看数据库里的字段可靠。
            print("--- 注入预览 ---")
            for it in res.items:
                print(format_event(it.event, it.depth, body=it.raw_text or None))
        _deep()
        return 0
    finally:
        store.close()


def cmd_traits(args: argparse.Namespace) -> int:
    """看某个人的角色卡，或列出全部实体。

    这张卡是 AI 眼里的"你朋友"。用户问"它凭什么觉得我讨厌猫"时，
    唯一能回答的就是这个命令：每一行后面挂着证据事件 id。
    """
    from .core.card import render_card

    store = Store.open(_db_path(args), _config_for(args))
    try:
        if args.entity:
            e = store.find_entity(args.session, args.entity)
            if e is None:
                print(f"没找到「{args.entity}」。试试 `hippo traits` 看全部。")
                return 1
            traits = store.list_traits(e.id, active_only=False)
            print(render_card(e, traits))
        else:
            ents = store.list_entities(args.session)
            if not ents:
                print("还没有任何实体。")
                return 0
            for e in ents:
                n = len(store.list_traits(e.id))
                print(f"#{e.id:<3} {e.canonical:<10} {e.type:<7} traits={n:<3} conf={e.confidence:.2f}")
    finally:
        store.close()
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """手动把两个实体并成一个。合并不可逆，所以必须显式给 id。"""
    store = Store.open(_db_path(args), _config_for(args))
    try:
        stats = store.merge_entities(args.src, args.dst)
        print(f"已把 #{args.src} 并入 #{args.dst}：{stats}")
    except Exception as exc:  # noqa: BLE001
        print(f"合并失败：{exc}")
        return 1
    finally:
        store.close()
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    """看当前事实，或某个时间点的事实。

    `--at` 是这一节的重点：不给人翻历史的命令，
    "旧值留着"就只是个数据库特性，用户永远用不上。
    """
    from datetime import datetime

    from .core.timeparse import parse_when

    store = Store.open(_db_path(args), _config_for(args))
    try:
        when = None
        if args.at:
            p = parse_when(args.at)
            if p is None:
                print(f"看不懂「{args.at}」。试试 `--at 2026-03-01`。")
                return 1
            when = p.ts

        if when is not None:
            key = args.key
            if not key:
                print("按时间查需要给 key，例如 `hippo facts city --at 去年冬天`。")
                return 1
            f = store.fact_as_of(args.session, key, when)
            if f is None:
                print(f"{datetime.fromtimestamp(when):%Y-%m-%d} 时没有 {key} 的记录。")
                return 0
            print(f"{datetime.fromtimestamp(when):%Y-%m-%d}  "
                  f"{f.key} = {f.value}"
                  f"（记录于 {datetime.fromtimestamp(f.valid_from):%Y-%m-%d}）")
            return 0

        rows = store.list_facts(args.session)
        if not rows:
            print("还没有任何事实。")
            return 0
        for f, who in rows:
            flag = "" if f.valid_to is None else "  (历史)"
            print(f"{f.key:<12} {f.value:<22} {who or '用户':<8}{flag}")
    finally:
        store.close()
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """手动跑一次抽取。

    为什么必须有这个命令：
      1. 代理里的抽取是"攒够 5 轮才抽"，聊 3 轮就关掉的那 3 轮会留在 pending。
         `hippo extract --force` 是手动补的唯一入口（M21 会做成自动心跳）。
      2. 调 prompt 时，你需要"只抽这一个窗口、只花这一次钱"的能力，
         而不是去跟代理聊天。
      3. `--dry-run` 能在不花钱的前提下看清"到底有多少东西等着抽"。
    """
    import asyncio

    cfg = _config_for(args)
    db = _db_path(args)
    if not db.exists():
        print(f"数据库不存在：{db}", file=sys.stderr)
        return 1

    sessions = [args.session] if args.session else _all_sessions(db)
    if not sessions:
        print("库里没有任何对话。先聊两句，或 hippo remember 一条。")
        return 0

    store = Store.open(db)
    try:
        if args.dry_run:
            total = 0
            for s in sessions:
                n = store.pending_turn_count(s)
                total += n
                print(f"  {s:<24} {n:>4} 条未抽取 turn")
            print(f"\n合计 {total} 条。加 --force 立刻抽，不加则等攒够 "
                  f"{cfg.extract.every_n_turns} 轮。")
            return 0

        key = cfg.resolve_api_key(None)
        if not key:
            print("缺少 API Key：设 DEEPSEEK_API_KEY 或写进 config.deepseek.api_key",
                  file=sys.stderr)
            return 1

        rep_all = asyncio.run(_extract_sessions(cfg, store, key, sessions, args.force))
    finally:
        store.close()

    for rep in rep_all:
        print(f"\n[{rep.session}]")
        for r in rep.results:
            print("  " + r.line())
        if rep.results:
            print(f"  小计：事件 {rep.events} 条，花费 ¥{rep.cost:.4f}")
    return 0


def _all_sessions(db: Path) -> list[str]:
    with Store.open(db) as store:
        return store.list_sessions()


async def _extract_sessions(cfg, store, key: str, sessions: list[str], force: bool):
    import httpx

    from .core.encode import Encoder
    from .core.extractor import Extractor

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout,
                              write=30.0, pool=10.0),
    ) as client:
        ex = Extractor(store, cfg, Encoder(cfg, client, key))
        out = []
        for s in sessions:
            out.append(await ex.run(s, force=force))
        return out


def cmd_serve(args: argparse.Namespace) -> int:
    """起代理。这一步之后，客户端只需要改 base_url。"""
    import uvicorn

    from .adapters.proxy import create_app

    cfg = _config_for(args)
    if args.dir:
        cfg.db_path = str(Path(args.dir).expanduser() / "mem.db")
    port = args.port

    db = Path(cfg.db_path).expanduser()
    if not db.exists():
        print(f"数据库不存在：{db}\n先运行：hippo init{' --dir ' + args.dir if args.dir else ''}",
              file=sys.stderr)
        return 1

    base = f"http://{args.host}:{port}"
    log.setup(log_dir=db.parent / "logs")
    try:
        made = backup_mod.ensure_daily(db)
        if made:
            print(f"  备份      {made}（今日首份，自动）")
    except Exception as exc:  # noqa: BLE001
        # 备份失败不该拦住对话服务：记忆是锦上添花，不是路由器。
        print(f"  备份      今日自动备份失败：{exc}", file=sys.stderr)
    print(f"海马体代理已就绪 → {base}")
    print(f"  数据库    {db}")
    print(f"  日志      {log.current_log_file(db.parent / 'logs')}")
    print(f"  上游      {cfg.deepseek.base_url}")
    print(f"  会话隔离  请求头 {cfg.session.header}: <名字>（不传则按首条消息自动归并）")
    print()
    print("首次使用：把客户端（SillyTavern / OpenAI SDK / 任何 OpenAI 兼容客户端）的")
    print(f"  base_url 改成 \"{base}/v1\" 就可以了。")
    print()
    # 输出被重定向到文件/管道时 Python 默认块缓冲，uvicorn 一启动就再也不
    # 刷缓冲 —— 用户在日志里根本看不到这段引导。显式 flush。
    sys.stdout.flush()

    uvicorn.run(create_app(cfg), host=args.host, port=port,
                log_level=args.log_level, access_log=False)
    return 0


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hippo", description="海马体插件 · DeepSeek 记忆中间件")
    p.add_argument("--version", action="version", version=f"hippocampus-deepseek {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dir", help="工作目录（默认 ~/.hippo）")
        sp.add_argument("--config", help="配置文件路径")

    pi = sub.add_parser("init", help="初始化配置、人设与数据库")
    common(pi)
    pi.add_argument("--force-rebuild", action="store_true", help="忽略已有数据（危险）")
    pi.set_defaults(func=cmd_init)

    pd = sub.add_parser("doctor", help="环境自检")
    common(pd)
    pd.set_defaults(func=cmd_doctor)

    pg = sub.add_parser("recompute", help="重算权重与分层（M14）")
    common(pg)
    pg.add_argument("--dry-run", action="store_true", help="只算不写")
    pg.add_argument("--session", default="", help="只重算这个会话")
    pg.add_argument("--limit", type=int, default=0)
    pg.add_argument("--explain", type=int, default=0, metavar="ID",
                    help="拆开一条事件的权重公式")
    pg.set_defaults(func=cmd_recompute)

    pc = sub.add_parser("consolidate", help="夜间巩固：合并重复 + 归档（M15）")
    common(pc)
    pc.add_argument("--session", default="", help="只处理这个会话")
    pc.add_argument("--dry-run", action="store_true", help="只算不写")
    pc.add_argument("--no-summary", action="store_true",
                    help="不重生成摘要，只做结构合并")
    pc.set_defaults(func=cmd_consolidate)

    pu = sub.add_parser("unmerge", help="撤销一次合并（M15.2）")
    common(pu)
    pu.add_argument("--id", type=int, required=True, help="主记录 id")
    pu.set_defaults(func=cmd_unmerge)

    pp = sub.add_parser("pin", help="钉住 / 撤销钉住（M16.2）")
    common(pp)
    pp.add_argument("--event", type=int, default=0, help="事件 id")
    pp.add_argument("--off", action="store_true", help="撤销钉住")
    pp.add_argument("--list", action="store_true", help="列出已钉住的")
    pp.add_argument("--session", default="main")
    pp.set_defaults(func=cmd_pin)

    pc = sub.add_parser("curiosity", help="好奇队列：看它想问什么（M25）")
    common(pc)
    pc.add_argument("--list", action="store_true", help="列出当前队列")
    pc.add_argument("--tick", action="store_true", help="手动跑一轮准备（调试用）")
    pc.add_argument("--session", default="", help="只看某个会话")
    pc.set_defaults(func=cmd_curiosity)

    pa = sub.add_parser("archive", help="归档冷记忆（M15.3）")
    common(pa)
    pa.add_argument("--dry-run", action="store_true", help="只算不搬")
    pa.add_argument("--max-weight", type=float, default=0.05)
    pa.add_argument("--idle-days", type=int, default=365)
    pa.add_argument("--limit", type=int, default=0)
    pa.add_argument("--archive-dir", default="", help="归档库目录（默认 <db>/archive）")
    pa.add_argument("--no-vacuum", action="store_true",
                    help="不 VACUUM（文件不会缩小，但省几秒）")
    pa.set_defaults(func=cmd_archive)

    pr2 = sub.add_parser("restore", help="恢复：整库从备份，或从归档库拉单条")
    common(pr2)
    grp = pr2.add_mutually_exclusive_group()
    grp.add_argument("--id", type=int, help="事件 id（从归档库拉回，M15.3）")
    grp.add_argument("--latest", action="store_true",
                     help="用最近一份整库备份恢复（先停服务）")
    grp.add_argument("--backup", default="",
                     help="用指定备份文件恢复整库")
    pr2.add_argument("--archive-dir", default="")
    pr2.add_argument("--backups-dir", default="")
    pr2.set_defaults(func=cmd_restore)

    pbu = sub.add_parser("backup", help="备份数据库（VACUUM INTO，保留最近 7 份）")
    common(pbu)
    pbu.add_argument("--keep", type=int, default=backup_mod.DEFAULT_KEEP,
                     help="保留最近几份（默认 7）")
    pbu.add_argument("--backups-dir", default="",
                     help="备份目录（默认 <库目录>/backups）")
    pbu.set_defaults(func=cmd_backup)

    ps = sub.add_parser("stats", help="统计")
    common(ps)
    ps.add_argument("--disk", action="store_true",
                    help="磁盘占用明细（活数据/空闲页/归档库）")
    ps.add_argument("--giveup", action="store_true",
                    help="能力边界：按 level/原因 聚合弃权记录（M22.3）")
    ps.add_argument("--ledger", action="store_true",
                    help="奖励账簿：待结算/已结算/RPE/懊悔记忆（M23）")
    ps.add_argument("--session", help="只统计某个会话（配合 --giveup / --ledger）")
    ps.add_argument("--heartbeat", action="store_true",
                    help="心跳状态：最近一次 tick / 今日升级调用数（M24）")
    ps.add_argument("--budget", action="store_true",
                    help="日预算明细：已用 / 限额 / 是否降级（M27.3）")
    ps.set_defaults(func=cmd_stats)

    pb = sub.add_parser("budget", help="日预算：看状态 / 手动恢复（M27.3）")
    common(pb)
    pb.add_argument("action", nargs="?", default="status",
                    choices=["status", "reset"], help="默认 status")
    pb.set_defaults(func=cmd_budget)

    pr = sub.add_parser("remember", help="手动写入一条记忆")
    common(pr)
    pr.add_argument("text", nargs="+")
    pr.add_argument("--summary")
    pr.add_argument("--keywords", help="逗号分隔")
    pr.add_argument("--session", default="main")
    pr.add_argument("--kind", default="event")
    pr.add_argument("--pin", action="store_true")
    pr.set_defaults(func=cmd_remember)

    pc = sub.add_parser("recall", help="调试检索")
    common(pc)
    pc.add_argument("query", nargs="+")
    pc.add_argument("--session", default="main")
    pc.add_argument("--limit", type=int, default=10)
    pc.add_argument("--l1", action="store_true", help="只跑分类层（谁/何时/要历史）")
    pc.add_argument("--explain", action="store_true", help="解释这句话被读成了什么")
    pc.add_argument("--raw", action="store_true", help="连原文层一起跑，打印注入文本")
    pc.add_argument("--deep", action="store_true",
                    help="额外查归档库（一年前的冷记忆，不进正常检索）")
    pc.set_defaults(func=cmd_recall)

    pw = sub.add_parser("why", help="看一句话为什么被预筛放行/挡掉")
    common(pw)
    pw.add_argument("text", nargs="+")
    pw.add_argument("--session", default="main")
    pw.add_argument("--prev", default="", help="上一轮 AI 的回复，用于 answer_to_question")
    pw.set_defaults(func=cmd_why)

    pe = sub.add_parser("extract", help="手动跑一次抽取（补尾巴 / 调 prompt）")
    common(pe)
    pe.add_argument("--session", help="只处理这个会话；不传则处理全部")
    pe.add_argument("--force", action="store_true",
                    help="不足 every_n_turns 也抽（补聊到一半的尾巴）")
    pe.add_argument("--dry-run", action="store_true", help="只看有多少待抽，不调模型")
    pe.set_defaults(func=cmd_extract)

    pt = sub.add_parser("traits", help="看角色卡 / 列出实体")
    common(pt)
    pt.add_argument("--entity", help="实体名（可以是别名）")
    pt.add_argument("--session", default="main")
    pt.set_defaults(func=cmd_traits)

    pf = sub.add_parser("facts", help="看当前事实 / 某个时间点的事实")
    common(pf)
    pf.add_argument("key", nargs="?", help="事实名（city / employer …）")
    pf.add_argument("--at", help="时间点，如 2026-03-01 或 去年冬天")
    pf.add_argument("--session", default="main")
    pf.set_defaults(func=cmd_facts)

    pm = sub.add_parser("merge", help="把两个实体并成一个（不可逆）")
    common(pm)
    pm.add_argument("src", type=int, help="被并掉的实体 id")
    pm.add_argument("dst", type=int, help="保留的实体 id")
    pm.set_defaults(func=cmd_merge)

    pv = sub.add_parser("serve", help="启动代理（改一行 base_url 就能用）")
    common(pv)
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8787)
    pv.add_argument("--log-level", default="info")
    pv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
