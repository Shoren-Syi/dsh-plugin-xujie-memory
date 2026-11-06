"""真机抽取评测（M5.5）：真 key、真模型、真库，零 mock。

回答 R-5-Gate §4 里挂着的四个"未验证"：

    U-1 schema 合法率      —— parse 成功 / 总调用
    U-2 raw 锚定通过率     —— 锚上的 events / 总 events   ← 最致命的一项
    U-3 真实成本           —— 每窗口 / 每轮
    U-4 缓存命中率         —— 命中前缀 token / 总 prompt token

语料按"最容易抽错"的类型挑，不是按"好抽"挑：
    日常叙事 / 偏好 / 情绪 / 事实归属 / 计划时间 / 寒暄（该被预筛挡）
    / 指代模糊 / 更正 / 多人物 / 长文本

用法：
    python3 -m tools.live_extract --limit 10          # 跑 10 个窗口
    python3 -m tools.live_extract --limit 10 --model mimo-v2.6-pro
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.extractor import Extractor
from hippo.core.migrate import migrate
from hippo.core.schema import anchor_raw
from hippo.core.store import Store
from hippo.core.turns import record_pair

OUT = Path("reports/data/live_extract.json")

# 每个元素 = 一个窗口（5 轮 user+assistant）。窗口之间不共享上下文，
# 各自独立打分，避免一个窗口的错污染另一个。
CORPUS: list[tuple[str, list[tuple[str, str]]]] = [
    ("narrative", [
        ("我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千，累死了", "搬完最累的是收拾吧，新家离公司近吗？"),
        ("近多了，走路二十分钟。对了老王说他下周要去深圳出差，让我帮他喂猫", "老王经常出差啊，喂猫这事你答应了吗？"),
        ("答应了，但我其实有点怕猫，从小被挠过一次", "那你还答应，真是够意思。"),
        ("没办法，他人挺好的。我下周三要交季度报告，还没开始写", "那这周得抓紧了。"),
        ("嗯，我这个人就是拖，每次都要到最后两天才动手", "拖延症挺普遍的。"),
    ]),
    ("preference", [
        ("我吃不了辣，一点都受不了，上次吃火锅辣得我眼泪直流", "那以后点菜注意点，微辣也别碰？"),
        ("微辣还行，中辣就不行了。另外我不喝冰的，胃不好", "记住了，常温的。"),
        ("还有我特别讨厌香菜，闻到就难受", "香菜确实争议大。"),
        ("对了我一般晚上十一点睡，别那个点找我", "好，十一点后不打扰。"),
        ("早上我七点起，要赶地铁", "通勤时间不短啊。"),
    ]),
    ("emotion", [
        ("今天被老板当众骂了一顿，说我方案做得像垃圾", "当着多少人？"),
        ("整个组十几个人都在。我当时脸都烧起来了，恨不得钻地缝", "那确实很难受。"),
        ("最气的是他自己上周还夸过这个方案，说方向对", "前后不一致确实让人窝火。"),
        ("我现在就想辞职，但房贷还有二十年", "这个顾虑很现实。"),
        ("算了，先忍着吧，等年终奖下来再说", "先稳住也好。"),
    ]),
    ("facts", [
        ("我在字节做后端，今年是第三年了", "后端具体做什么方向？"),
        ("做推荐系统的召回，主要用 Go", "Go 在推荐这块用得挺多。"),
        ("我养了只猫，叫年糕，三岁了", "年糕这名字可爱。"),
        ("我老婆是护士，上夜班比较多", "那作息挺辛苦的。"),
        ("我们住在海淀，房子是前年买的", "海淀学区房不便宜吧。"),
    ]),
    ("plans", [
        ("提醒我明天早上八点开会", "好，明早八点。"),
        ("还有周五下午三点要去医院复查", "哪个医院？"),
        ("北大人民医院，复查甲状腺", "记下了。"),
        ("下个月开始每周三晚上去学吉他", "学吉他挺好的。"),
        ("对了下周日是我妈生日，得提前订蛋糕", "提前订确实稳妥。"),
    ]),
    ("chitchat", [
        ("早", "早上好！"),
        ("吃了吗", "吃了，你呢？"),
        ("哈哈", "有什么想聊的吗？"),
        ("今天天气不错", "是挺好的，适合出门。"),
        ("嗯", "嗯嗯。"),
    ]),
    # ---- 下面四个是"负样本"。没有它们，这份评测只能证明"模型会抄写" ----
    # 上一版 10 个窗口每条 user 消息都值得记，于是"每条都记"恰好全对，
    # 等于什么都没测到。这跟 M4 那次 P=1.000 是同一个错误。
    ("chitchat2", [
        ("哦", "AI 的长篇回复不该进记忆。"),
        ("原来这样", "用户只是在接话，没有信息。"),
        ("有道理", "同上。"),
        ("懂了", "同上。"),
        ("谢谢", "同上。"),
    ]),
    ("aitalk", [
        ("给我讲讲快速排序", "快速排序是分治算法，平均复杂度 O(n log n)，最坏 O(n²)。"),
        ("那归并呢", "归并排序稳定，复杂度恒为 O(n log n)，但要额外 O(n) 空间。"),
        ("堆排呢", "堆排序原地，复杂度 O(n log n)，但不稳定。"),
        ("明白了", "这几个是最常考的三个。"),
        ("再讲讲桶排", "桶排序是线性时间，但要求数据分布均匀。"),
    ]),
    ("halfanswer", [
        ("你老家哪儿的", "不知道你指哪个老家"),
        ("我是说出生地", "忘了，好像是个小地方"),
        ("那你父母现在在哪", "不太清楚，没细问过"),
        ("行吧", "嗯。"),
        ("这个不重要", "好。"),
    ]),
    ("repeat", [
        ("我不吃辣", "记住了。"),
        ("真的，我一点辣都吃不了", "明白，一点辣都不放。"),
        ("上次说了我不吃辣，别放辣椒", "放心，记着呢。"),
        ("我吃辣会胃疼", "那确实得避开。"),
        ("总之就是不吃辣", "好，不吃辣。"),
    ]),
    ("ambiguous", [
        ("那边最近怎么样", "哪边？"),
        ("就那边啊，你知道的", "我可能不太确定你指哪里。"),
        ("行吧，回头再说", "好。"),
        ("上次那个事办完了吗", "哪个事？"),
        ("算了不重要", "好，那先放着。"),
    ]),
    ("correction", [
        ("我住在朝阳", "朝阳哪个片区？"),
        ("不对，我说错了，是海淀", "海淀啊，那改过来。"),
        ("我今年二十八", "嗯。"),
        ("哦不对，我二十九了，刚过完生日", "那补一句生日快乐。"),
        ("谢谢，其实我不太在意年龄", "理解。"),
    ]),
    ("multiperson", [
        ("老王和小李下周三要一起来北京", "两个人都来？"),
        ("对，老王从深圳飞，小李从上海坐高铁", "行程不一样啊。"),
        ("小李怕坐飞机，从来不坐", "那高铁要五个小时。"),
        ("老王倒是无所谓，他常飞", "出差多的人习惯了。"),
        ("他们住我这儿，我那有间空房", "那挺方便。"),
    ]),
    ("longtext", [
        ("最近在考虑要不要换个工作，现在这家公司做了三年，技术栈有点老，"
         "团队氛围还行但晋升通道基本堵死了，领导是空降的，跟我不太对付，"
         "手上有两个 offer，一个是字节的，钱多百分之三十但要 996，"
         "另一个是一家做 AI 基础设施的创业公司，钱少一点但有期权，"
         "我比较纠结的是期权到底值不值钱，还有就是这个年纪换赛道风险大不大",
         "两个选择差别挺大的，你更看重什么？"),
        ("钱和成长我都想要，但好像只能选一个", "确实很难两全。"),
        ("我今年三十二，有房贷，孩子明年上小学", "那稳定性的权重会更高。"),
        ("所以偏向字节，虽然累但至少现金流稳", "这个考虑很实际。"),
        ("再想想吧，下个月给答复", "还有时间。"),
    ]),
]


def build_db(db: Path, corpus) -> None:
    conn = connect(str(db))
    migrate(conn)
    conn.close()
    for name, turns in corpus:
        for u, a in turns:
            record_pair(str(db), name, u, a)


async def run(model: str, limit: int) -> int:
    cfg = load_config()
    cfg.deepseek.extract_model = model
    cfg.deepseek.thinking = "disabled"
    key = cfg.deepseek.api_key
    if not key:
        print("没有 api_key，先配 ~/.hippo/config.yaml")
        return 2

    corpus = CORPUS[:limit]
    tmp = Path(tempfile.mkdtemp(prefix="hippo-live-"))
    db = tmp / "mem.db"
    build_db(db, corpus)

    store = Store.open(db)
    rows: list[dict] = []
    total_cost = 0.0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        ) as client:
            enc = Encoder(cfg, client, key)
            ext = Extractor(store, cfg, enc)

            for name, _turns in corpus:
                t0 = time.perf_counter()
                rep = await ext.run(name, force=True)
                wall = int((time.perf_counter() - t0) * 1000)

                # 逐窗口核算：数量指标从库里查，比从 rep 里拼更可信
                raws = store.raws_of_session(name, skip_kind="raw")
                det = store.window_detail(name)
                n_ev = len(raws)
                conv = "\n".join(f"{r}: {c}" for r, c in _turns)
                anchored = sum(1 for r in raws if anchor_raw(r, conv))

                for r in rep.results:
                    total_cost += r.cost

                rows.append({
                    "window": name,
                    "status": [r.status for r in rep.results],
                    "events": n_ev,
                    "anchored": anchored,
                    "raws": raws,
                    "facts": det["facts"],
                    "traits": det["traits"],
                    "plans": det["plans"],
                    "doubts": det["doubts"],
                    "details": [
                        {"i": f"{r.start_idx}-{r.end_idx}", "st": r.status,
                         "ev": r.events, "fact": r.facts, "trait": r.traits,
                         "plan": r.plans, "doubt": r.doubts,
                         "cached": r.cached_tokens, "rt": r.reasoning_tokens,
                         "ms": r.latency_ms, "cost": round(r.cost, 6),
                         "err": r.error}
                        for r in rep.results],
                    "wall_ms": wall,
                })
                d = " ".join(f"[{x['i']}]{x['st']}:{x['ev']}ev/c{x['cached']}"
                             for x in rows[-1]["details"])
                print(f"{name:<12} {d}  锚定 {anchored}/{n_ev}  {wall}ms")

        stats = store.run_stats()
    finally:
        store.close()

    # ---- 汇总 ----
    attempted = stats.get("attempted", 0)
    ok = stats.get("ok", 0)
    total_ev = sum(r["events"] for r in rows if r["window"] != "chitchat")
    total_anchored = sum(r["anchored"] for r in rows if r["window"] != "chitchat")
    skipped = stats.get("skipped", 0)

    # 缓存命中率：从 metrics 拿不到（抽取没写 metrics 表），用 extract_runs 的
    # prompt_tokens 反推不现实，所以这里单独打一次 requests 级别的统计
    summary = {
        "model": model,
        "cache_hit_rate": stats.get("cache_hit_rate"),
        "prompt_tokens": stats.get("prompt_tokens"),
        "cached_tokens": stats.get("cached_tokens"),
        "reasoning_tokens": stats.get("reasoning_tokens"),
        "thinking": "disabled",
        "windows": len(rows),
        "attempted": attempted,
        "ok": ok,
        "skipped": skipped,
        "degraded": stats.get("degraded", 0),
        "schema_ok_rate": round(ok / attempted, 4) if attempted else None,
        "events_total": total_ev,
        "anchored": total_anchored,
        "anchor_rate": round(total_anchored / total_ev, 4) if total_ev else None,
        "verbatim_fail": stats.get("verbatim_fail", 0),
        "cost_total": round(total_cost, 6),
        "cost_per_window": round(total_cost / len(rows), 6) if rows else None,
        "cost_per_turn": round(total_cost / (len(rows) * 5), 6) if rows else None,
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"窗口 {summary['windows']}  调用 {attempted}  成功 {ok}  "
          f"降级 {summary['degraded']}  预筛跳过 {skipped}")
    print(f"U-1 schema 合法率   {summary['schema_ok_rate']}")
    print(f"U-2 raw 锚定通过率  {summary['anchor_rate']}  "
          f"({total_anchored}/{total_ev})")
    print(f"U-3 总成本          ¥{summary['cost_total']}  "
          f"每窗口 ¥{summary['cost_per_window']}  每轮 ¥{summary['cost_per_turn']}")
    print(f"U-4 缓存命中率      {summary['cache_hit_rate']}  "
          f"({summary['cached_tokens']}/{summary['prompt_tokens']} token)")
    print(f"    思考 token      {summary['reasoning_tokens']}（关思考时应为 0）")
    lat = [d["ms"] for r in rows for d in r["details"] if d["st"] != "skipped"]
    if lat:
        print(f"    延迟 中位 {statistics.median(lat):.0f}ms  "
              f"最大 {max(lat)}ms  p90 {sorted(lat)[int(len(lat) * 0.9)]}ms")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mimo-v2.6-flash")
    ap.add_argument("--limit", type=int, default=len(CORPUS))
    args = ap.parse_args()
    return asyncio.run(run(args.model, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
