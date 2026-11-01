"""Windows 控制台的编码兜底。

演示文本里有 `−`（U+2212）、`⚠`、`✓`、`█` 这类字符。中文 Windows 的控制台
默认是 GBK（cp936），编不出来的字符会让 `print` 抛 `UnicodeEncodeError`——
而且通常发生在演示中段，看起来像逻辑坏了，其实是输出编码。

这里只放宽**错误处理**（`errors="replace"`），不改编码：

    cmd / PowerShell（cp936）  中文照常显示，个别符号退化成 `?`
    已是 UTF-8 的终端            不受影响（Linux、CI、chcp 65001）

**不要把它做成 import 副作用**：库被谁 import 都可能，改全局 stdout 是调用方的事。
"""

from __future__ import annotations

import sys

_UTF8 = {"utf-8", "utf8", "utf-16", "utf-16-le", "utf-16-be", "utf-32"}


def make_console_safe() -> bool:
    """让 stdout/stderr 遇到编不出的字符时退化，而不是抛异常。

    返回是否真的动了标准流（已是 UTF-8 时返回 False）。
    """
    changed = False
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding in _UTF8:
            continue
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            # 被别的框架接管过的流可能不允许重新配置——那就保持原样，
            # 宁可少一层保护，也不要在这里把演示弄挂。
            continue
        changed = True
    return changed
