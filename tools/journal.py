"""生成施工记录条目骨架：`make journal M=3.1`。

存在的唯一理由是**降低摩擦**。写记录最大的敌人不是懒，
是"面对空白文件不知道从哪下笔"。给一个填格子，人就会填。

设计上刻意不做：
  - 不自动填入测试结果（那会把"记录"变成"抄写"，抄写没有判断）
  - 不写"完成情况"这种可以全填 ✅ 的字段（验收报告已经有了）
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "施工记录.md"
MARKER = "<!-- APPEND_MARKER -->"

TEMPLATE = """## {stamp}　M{mid} {title}

### 开工判断

动手前我认为的难点是：**（一句话）**。
（收工时回来对一下，这里最容易暴露判断偏差。）

### 决策

| 编号 | 选择 | 否掉了什么 | 依据 |
|---|---|---|---|
|  |  |  |  |

### 施工过程

{stamp}　（动作 + 命令 + 输出）

### 事故

（撞了什么、怎么定位、怎么修。没有就写「无」，但连续三个里程碑都是「无」，
说明在糊弄。）

### 收工状态

```
$
```

---

"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="journal")
    p.add_argument("milestone", help="里程碑号，如 3.1")
    p.add_argument("--title", default="", help="标题，默认留空自己填")
    p.add_argument("--file", default=str(JOURNAL))
    args = p.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    if MARKER not in text:
        print(f"{path} 里没有 {MARKER}，无法定位插入点", file=sys.stderr)
        return 1

    now = datetime.now()
    heading = f"## {now:%Y-%m-%d %H:%M}　M{args.milestone}"
    if heading in text:
        print(f"已存在同名条目：{heading}", file=sys.stderr)
        return 1

    block = TEMPLATE.format(stamp=f"{now:%Y-%m-%d %H:%M}",
                            mid=args.milestone, title=args.title)
    path.write_text(text.replace(MARKER, block + MARKER), encoding="utf-8")
    print(f"已插入 {path}：{heading}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
