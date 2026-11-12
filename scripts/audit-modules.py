# -*- coding: utf-8 -*-
"""模块级可达性审计：哪些 core 模块在运行时没被接线。

判定：从运行时入口（dsh_kernel / pack / extractor / persist / cli）出发做引用图，
只通过 tests/scripts 引用的模块 = 运行时死功能。
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\h\hippo")
CORE = ROOT / "hippo" / "core"
ENTRIES = ["dsh_kernel.py", "cli.py", "core/pack.py", "core/extractor.py", "core/persist.py"]


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def imports_of(path: Path, own: str) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    package = own.split(".")
    # 相对导入 level=1 表示当前包，level=2 表示上一级
    base = ".".join(package[:-1])
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = base.split(".")
                keep = len(parts) - (node.level - 1)
                prefix = ".".join(parts[:keep])
                mod = f"{prefix}.{node.module}" if node.module else prefix
            else:
                mod = node.module or ""
            if mod:
                found.add(mod)
                for alias in node.names:      # from .core import qq 也要算到 qq
                    found.add(f"{mod}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def main() -> int:
    files = {module_name(p): p for p in ROOT.rglob("*.py")
             if "hippo" in p.parts and "tests" not in p.parts}
    graph = {name: imports_of(path, name) for name, path in files.items()}

    # 从入口 BFS
    reachable: set[str] = set()
    queue = [m for m in graph if any(m.endswith(e.replace("/", ".").replace(".py", "")) for e in ENTRIES)]
    while queue:
        cur = queue.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        for dep in graph.get(cur, ()):
            for name in graph:
                if dep == name or dep.startswith(name + "."):
                    if name not in reachable:
                        queue.append(name)

    core_modules = {n for n in graph if n.startswith("hippo.core.")}
    dead = sorted(m.replace("hippo.core.", "") for m in core_modules - reachable if m.count(".") == 2)

    # 这些模块是否被 tests / scripts 用到（说明是"仅测试用"）
    tests_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                           for p in (ROOT / "tests").rglob("*.py"))
    scripts_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                             for p in (ROOT / "scripts").rglob("*.py"))
    rows = []
    for mod in dead:
        used_tests = mod in tests_text
        used_scripts = mod in scripts_text
        rows.append((mod, used_tests, used_scripts))

    print(f"core 模块 {len(core_modules)} 个，运行时不可达 {len(rows)} 个")
    for mod, t, s in rows:
        flag = "仅测试/脚本用" if (t or s) else "完全没接线"
        print(f"  {mod:20} {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
