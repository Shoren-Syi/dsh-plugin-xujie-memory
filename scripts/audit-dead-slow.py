# -*- coding: utf-8 -*-
"""死功能 / 低效审计。

1. 配置项：定义了但代码里没人读的字段（死旋钮）
2. 路由：内核暴露但宿主/面板从不调用的接口
3. 效率：一次 pack 打多少条 SQL、重复查询
"""
from __future__ import annotations

import ast
import io
import re
import sys
from pathlib import Path

ROOT = Path(r"C:\h\hippo")
SRC = ROOT / "hippo"
HOST = ROOT / "lib"


def config_fields() -> list[tuple[str, str]]:
    """从 config.py 提取所有字段名（含类名）。"""
    tree = ast.parse((SRC / "config.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Cfg"):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    out.append((node.name.replace("Cfg", ""), item.target.id))
    return out


def all_py_text() -> str:
    parts = []
    for p in list(SRC.rglob("*.py")) + list(HOST.rglob("*.js")) + [ROOT / "lib" / "client.js"]:
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


def dead_config(fields, text: str) -> list[str]:
    dead = []
    for section, name in fields:
        # 字段名作为属性出现（cfg.x.name / .name）才算被使用
        hits = len(re.findall(rf"\b{re.escape(name)}\b", text))
        if hits == 0:
            dead.append(f"{section}.{name}")
    return dead


def routes() -> list[str]:
    text = (SRC / "dsh_kernel.py").read_text(encoding="utf-8")
    return re.findall(r'@app\.(?:get|post|delete)\("([^"]+)"', text)


def unused_routes(route_list, host_text: str, board_text: str) -> list[str]:
    unused = []
    for r in route_list:
        key = r.split("?")[0]
        used = key in host_text or key in board_text
        if not used:
            unused.append(r)
    return unused


def query_count() -> dict:
    """统计一次 pack 的 SQL 条数（按语句类型聚合）。"""
    import collections

    sys.path.insert(0, str(ROOT))
    from hippo.config import Config
    from hippo.core.pack import pack
    from hippo.core.store import Store

    db = Path(r"C:\h\stress\audit.db")
    if db.exists():
        db.unlink()
    cfg = Config()
    cfg.db_path = str(db)
    with Store.open(db, cfg) as st:
        st.add_turns("s", [
            ("user", "我在杭州做医疗项目，下个月要辞职", 0),
            ("assistant", "记下了", 0),
            ("user", "顺便把 lib/index.js 的 fetch 加上重试", 0),
            ("assistant", "好", 0),
            ("user", "我妈下个月十号过生日", 0),
        ], ts=1_760_000_000)
        st.add_event(session="s", raw="我在杭州做医疗项目，下个月要辞职",
                     summary="用户下个月辞职，去杭州做医疗项目", keywords=["杭州", "医疗", "辞职"],
                     event_time=1_760_000_000, importance=5)
        st.add_event(session="s", raw="顺便把 lib/index.js 的 fetch 加上重试",
                     summary="用户给 lib/index.js 的 fetch 加了重试", keywords=["fetch", "index.js"],
                     event_time=1_760_000_100, importance=3)
        counter = collections.Counter()
        # sqlite3.Connection 的属性是只读的，用官方 trace 回调统计
        st.conn.set_trace_callback(lambda sql: counter.update(
            [" ".join(str(sql).split())[:40]]))
        for _ in range(3):
            pack(st, "s", "我们下一步做什么", cfg=cfg, use_cache=False)
        st.conn.set_trace_callback(None)
    total = sum(counter.values())
    top = counter.most_common(12)
    return {"total_sql": total, "per_pack": round(total / 3, 1), "top": top}


def main() -> int:
    fields = config_fields()
    text = all_py_text()
    report = {
        "config_fields": len(fields),
        "dead_config": dead_config(fields, text),
        "routes": routes(),
    }
    board = (SRC / "data" / "board.html").read_text(encoding="utf-8")
    host = "\n".join(p.read_text(encoding="utf-8") for p in HOST.rglob("*.js"))
    report["unused_routes"] = unused_routes(report["routes"], host, board)
    report["queries"] = query_count()
    print(json_dump(report))
    (ROOT / "reports" / "R-52-dead-and-slow.md").write_text(
        format_report(report), encoding="utf-8")
    return 0


def json_dump(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1)[:4000]


def format_report(r) -> str:
    lines = ["# R-52 · 死功能与低效审计", "",
             f"- 配置字段总数：{r['config_fields']}",
             f"- 没人读的配置：{len(r['dead_config'])} 个 → {r['dead_config']}",
             f"- 内核路由：{len(r['routes'])} 个",
             f"- 宿主/面板从不调用：{r['unused_routes']}",
             f"- 一次 pack 的 SQL：约 {r['queries']['per_pack']} 条", "",
             "## 最频繁的 SQL", ""]
    for sql, n in r["queries"]["top"]:
        lines.append(f"- {n}× `{sql}`")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
