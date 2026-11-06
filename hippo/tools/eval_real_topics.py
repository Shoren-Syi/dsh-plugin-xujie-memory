"""多话题真实评测（M29）：真实 key 跑 10 个话题，检查记忆命中率。

用法（先起内核，带真实 key）：

    python -m hippo.tools.eval_real_topics http://127.0.0.1:8792

每个话题写 4 轮（2 轮用户 + 2 轮助手），强制抽取一次，再用两个问题
走 /assemble，检查注入的记忆文本里是否含预期关键词。
"""

from __future__ import annotations

import sys
import time

import httpx

TOPICS = [
    {
        "name": "健康",
        "rounds": [
            ("我对花生过敏，上次吃了花生酱整个人起疹子。", "记住了，花生过敏要当心。"),
            ("我还有慢性胃炎，医生让我少吃辣。", "好，辛辣的我会提醒你注意。"),
        ],
        "queries": [
            {"q": "我饮食上要注意什么？", "expect": ["花生", "过敏"]},
            {"q": "我胃怎么样来着？", "expect": ["胃炎"]},
        ],
    },
    {
        "name": "工作",
        "rounds": [
            ("我在杭州做后端开发，主要写 Python。", "杭州后端，Python，记下了。"),
            ("我们老板姓陈，最近想让我转做 AI 产品。", "陈老板想让你转 AI 产品，明白。"),
        ],
        "queries": [
            {"q": "我是做什么工作的？", "expect": ["后端"]},
            {"q": "我老板姓什么？", "expect": ["陈"]},
        ],
    },
    {
        "name": "家庭",
        "rounds": [
            ("我妹妹在上海读研究生，学的是法律。", "妹妹在上海读法律研究生，记住了。"),
            ("我妈腰一直不好，阴天就疼。", "你妈妈腰不好，阴天疼，我会记着。"),
        ],
        "queries": [
            {"q": "我妹妹在哪读书？", "expect": ["上海"]},
            {"q": "我妈身体怎么样？", "expect": ["腰"]},
        ],
    },
    {
        "name": "偏好",
        "rounds": [
            ("我不吃香菜的，闻到味道都不行。", "好的，香菜绝对不放。"),
            ("我每天早上都要一杯拿铁，不加糖。", "拿铁不加糖，记下了。"),
        ],
        "queries": [
            {"q": "我有什么忌口？", "expect": ["香菜"]},
            {"q": "我早上喝什么？", "expect": ["拿铁"]},
        ],
    },
    {
        "name": "旅行",
        "rounds": [
            ("下个月我打算去成都玩四天。", "成都四天，记下了。"),
            ("我预算大概三千块，想爬青城山。", "三千预算爬青城山，明白。"),
        ],
        "queries": [
            {"q": "我下个月要去哪？", "expect": ["成都"]},
            {"q": "我旅行预算多少？", "expect": ["三千"]},
        ],
    },
    {
        "name": "技术",
        "rounds": [
            ("我电脑是 Windows，装了 WSL2 在用。", "Windows + WSL2，记下了。"),
            ("最近在学 Rust，感觉所有权挺难的。", "Rust 所有权确实绕，我记着你在学。"),
        ],
        "queries": [
            {"q": "我用什么系统？", "expect": ["WSL"]},
            {"q": "我最近在学什么？", "expect": ["Rust"]},
        ],
    },
    {
        "name": "宠物",
        "rounds": [
            ("我养了一只橘猫，叫豆豆。", "豆豆，好名字，记下了。"),
            ("豆豆吃的是渴望猫粮，不能换别的。", "豆豆只吃渴望，明白。"),
        ],
        "queries": [
            {"q": "我的猫叫什么？", "expect": ["豆豆"]},
            {"q": "豆豆吃什么猫粮？", "expect": ["渴望"]},
        ],
    },
    {
        "name": "财务",
        "rounds": [
            ("我每个月房贷要还八千。", "月供八千，记下了。"),
            ("我最近在攒钱换车，不碰股票。", "攒钱换车、不碰股票，明白。"),
        ],
        "queries": [
            {"q": "我每月还多少房贷？", "expect": ["八千", "8000"]},
            {"q": "我对股票什么态度？", "expect": ["不碰", "股票"]},
        ],
    },
    {
        "name": "学习",
        "rounds": [
            ("我在学日语，目标是明年考过 N2。", "日语 N2，记下了。"),
            ("我每天背五十个单词，用 Anki。", "每天五十词，Anki，明白。"),
        ],
        "queries": [
            {"q": "我在学什么语言？", "expect": ["日语"]},
            {"q": "我每天背多少单词？", "expect": ["五十", "50"]},
        ],
    },
    {
        "name": "感情",
        "rounds": [
            ("我女朋友叫小雨，我们在一起三年了。", "小雨，三年，记下了。"),
            ("我们打算明年结婚。", "明年结婚，恭喜，我记着。"),
        ],
        "queries": [
            {"q": "我女朋友叫什么？", "expect": ["小雨"]},
            {"q": "我们在一起多久了？", "expect": ["三年"]},
        ],
    },
]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8792"
    hits = 0
    total = 0
    failures: list[str] = []
    started = time.time()

    with httpx.Client(timeout=90.0) as client:
        for i, topic in enumerate(TOPICS):
            sid = f"topic-{i}"
            for user_text, assistant_text in topic["rounds"]:
                client.post(f"{base}/event", json={"session": sid, "role": "user", "text": user_text}).raise_for_status()
                client.post(f"{base}/event", json={"session": sid, "role": "assistant", "text": assistant_text}).raise_for_status()
            client.post(f"{base}/extract", params={"session": sid, "force": "true"}).raise_for_status()
            time.sleep(4.0)  # 等抽取任务
            for q in topic["queries"]:
                res = client.post(f"{base}/assemble", json={"session": sid, "text": q["q"]}).json()
                text = res.get("text", "")
                ok = any(k in text for k in q["expect"])
                hits += 1 if ok else 0
                total += 1
                if not ok:
                    failures.append(f"{topic['name']} / {q['q']}（期望 {q['expect']}）")
                print(f"[{'HIT ' if ok else 'MISS'}] {topic['name']:<4} {q['q']}")

    ratio = hits / total if total else 0.0
    print()
    print(f"HIT {hits}/{total} = {ratio:.0%}  用时 {time.time() - started:.0f}s")
    if failures:
        print("未命中：")
        for f in failures:
            print("  -", f)
    return 0 if ratio >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
