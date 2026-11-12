# -*- coding: utf-8 -*-
"""M67 验收：代码窗口识别 + 工程记忆提示词。

成本纪律：判断是纯本地正则，不调模型；提示词只在"像代码"的窗口追加。
"""
from __future__ import annotations

from hippo.config import Config
from hippo.core.encode import Encoder
from hippo.core.extractor import coding_window
from hippo.core.turns import Window


def _w(text: str) -> Window:
    turn = type("T", (), {"role": "user", "content": text, "idx": 0, "partial": 0})()
    return Window(session="s", idxs=[0], token_est=0, has_pending=False, turns=[turn])


def test_coding_window_detects_code():
    assert coding_window(_w("```python\nimport os\nprint(os.getcwd())\n```"))
    assert coding_window(_w("Traceback (most recent call last):\n  File \"a.py\", line 3\nValueError: bad"))
    assert coding_window(_w("把 lib/index.js 里的 fetchJson 改成带重试，npm run test 挂了"))


def test_coding_window_ignores_chat():
    assert not coding_window(_w("今天好累啊，晚上想吃火锅"))
    assert not coding_window(_w("早安"))          # 太短
    assert not coding_window(_w("你觉得这部电影怎么样？我还没看呢"))


def test_encoder_appends_coding_addendum_only_when_coding(tmp_path):
    cfg = Config()
    enc = Encoder(cfg, None, "sk-test")
    plain = enc.build_messages(_w("今天吃什么"))[0]["content"]
    code = enc.build_messages(_w("改一下 lib/index.js"), coding=True)[0]["content"]
    assert "软件工程任务" in code
    assert "软件工程任务" not in plain
    assert len(code) > len(plain)
