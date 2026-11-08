# -*- coding: utf-8 -*-
"""版本一致性：包版本、内核版本、README 安装命令必须对得上。

起因：包已经到 1.1.5，但内核 `__version__` 还写着 1.0.0，健康检查返回 1.0.0，
用户装完看到"版本 1.0.0"会以为装错了。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import hippo

ROOT = Path(__file__).resolve().parent.parent


def test_package_and_kernel_version_match():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == hippo.__version__, f"包 {pkg['version']} != 内核 {hippo.__version__}"


def test_readme_install_command_matches_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    version = hippo.__version__
    assert f"dsh-plugin-xujie-memory@{version}" in readme, "README 安装命令版本没跟上"
    assert re.search(r"## 实测效果", readme), "README 缺实测数据"


def test_release_and_changelog_mention_current_version():
    version = hippo.__version__
    release = (ROOT / "RELEASE.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert version in release, "RELEASE.md 没写当前版本"
    assert f"## {version}" in changelog, "CHANGELOG.md 缺当前版本小节"


def test_readme_title_matches_version():
    version = hippo.__version__
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"# 叙界记忆 {version}" in readme, "README 标题版本没跟上"


def test_no_stale_current_version_claims():
    """当前面向用户的文档里不该再出现旧版本号（历史记录文件除外）。"""
    version = hippo.__version__
    for name in ("README.md", "RELEASE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for stale in ("1.0.0", "1.1.5", "1.1.6", "0.2.5"):
            assert stale not in text, f"{name} 里还留着旧版本 {stale}"
    assert version  # 单纯防变量被误删
