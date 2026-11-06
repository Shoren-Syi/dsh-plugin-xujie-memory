"""生成验收报告骨架：make verify M=1.1

跑对应里程碑的测试，把真实输出贴进报告，其余栏目留空待填。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.verify")
    ap.add_argument("--milestone", required=True, help="如 1.1")
    ap.add_argument("--open", action="store_true", help="生成后打印路径")
    args = ap.parse_args(argv)

    tag = args.milestone
    module = "m" + tag.replace(".", "_")
    files = sorted((ROOT / "tests").glob(f"test_{module}_*.py"))
    if not files:
        print(f"找不到 tests/test_{module}_*.py", file=sys.stderr)
        return 2

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *[str(f) for f in files], "-v", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = (proc.stdout + proc.stderr).strip()
    tail = "\n".join(output.splitlines()[-4:])
    passed = re.search(r"(\d+) passed", output)
    failed = re.search(r"(\d+) failed", output)

    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"R-{tag}.md"
    if path.exists():
        # 绝不覆盖已填写的报告：人工写的内容比脚手架值钱
        path = REPORTS / f"R-{tag}.new.md"
        print(f"⚠️  R-{tag}.md 已存在，改写到 {path.name}（请手工合并）", file=sys.stderr)
    path.write_text(
        f"""# R-{tag}

| 项 | 内容 |
|---|---|
| 里程碑 | M{tag.split('.')[0]} |
| 子任务 | M{tag} |
| 日期 | {dt.date.today():%Y-%m-%d} |
| 执行人 | （填写） |
| commit | （填写） |
| 环境 | Python {sys.version.split()[0]} |

## 1. 交付物

| 文件 | 说明 | 行数 |
|---|---|---|
| （填写） | | |

## 2. 验收项

| # | 要求 | 实测 | 结论 |
|---|---|---|---|
| 1 | （从手册抄） | （填跑出来的数） | |

## 3. 测试记录

```
{output}
```

## 4. 关键数据

| 指标 | 值 |
|---|---|
| 通过 / 失败 | {passed.group(1) if passed else 0} / {failed.group(1) if failed else 0} |

## 5. 遗留问题

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| — | 无 | — | — |

## 6. 降级说明

无。

## 7. Gate 结论

**（填写）**
""",
        encoding="utf-8",
    )
    print(tail)
    print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
