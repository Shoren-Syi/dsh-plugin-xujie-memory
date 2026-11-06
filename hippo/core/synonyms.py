"""同义词展开（M10.3）。

n-gram 只覆盖字面重叠。用户问"我在哪上班"、库里存"字节 后端 工作年限"，
倒排一条也命中不了——而 M9 已经证明这类查询认不出实体，候选是全库，
于是 L2 只能按 weight 兜底，AI 答非所问。

## 为什么是"组"，不是"映射表"

常见做法是一张 `查询词 → 库词` 的表。它在本机语料上分数好看，但：

  - 库里的措辞会变（同一个抽取器换个模型，"工作"就写成"任职"）
  - 查表是单向的，用户反过来问就失效
  - **最坏的是它看不出错**：查不到就是降级，降级就是"不记得"，
    没有人会发现是词表漏了

所以这里用等价组：组内任意两词互相扩展。库词变了只要还在组里就仍然有效。

## 触发方式：字符包含，不依赖分词

"我几点睡"里没有"睡觉"这个词，只有"睡"。所以不能拿查询词去查表，
得反过来——**看组里的词是否出现在查询文本里**。

单字触发词（"住""睡""猫"）因此变得关键，但也最危险："住"出现在"记住"里，
"猫"出现在"猫粮"里。所以：

  - 单字触发词只在**短查询**（≤12 字）时启用
  - 长句里单字误触发概率高，而长句本身就有足够的 2-gram 可用

## 展开出的词只进查询 token 集，不进 L1

L1 是"谁/何时"的硬过滤，同义词在那里展开会让"我住哪"把"住房"实体也捞进来。
在 L2 只影响打分，错了也只是排序偏一点。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from ..log import get_logger

log = get_logger(__name__)

__all__ = ["load_groups", "expand", "to_simplified", "t2s_map",
           "t2s_bad_pairs", "SHORT_QUERY_LEN"]

DATA = Path(__file__).resolve().parent.parent / "data" / "synonyms.yaml"

# 单字触发词的启用门槛。超过这个长度，一句话里的单字几乎必然有虚词/词中字，
# 误触发的收益远小于噪声。
SHORT_QUERY_LEN = 12


@lru_cache(maxsize=1)
def load_groups(path: str | Path | None = None) -> tuple[tuple[str, ...], ...]:
    """读表。返回不可变的组元组，方便缓存和跨请求复用。"""
    p = Path(path) if path else DATA
    if not p.exists():
        log.warning("synonyms.missing", extra={"fields": {"path": str(p)}})
        return ()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    groups: list[tuple[str, ...]] = []
    for item in raw:
        if not isinstance(item, list):
            continue
        words = tuple(dict.fromkeys(str(w).strip().lower() for w in item if str(w).strip()))
        if len(words) >= 2:
            groups.append(words)
    return tuple(groups)


def expand(text: str, *, groups: Iterable[tuple[str, ...]] | None = None) -> list[str]:
    """把查询文本里命中的同义词组展开成词列表。

    返回的是**组内全部词**（不只是没出现过的），去重后按出现顺序。
    已经出现在原文里的词也会返回——调用方反正要合并去重，
    在这里过滤等于让调用方少一层判断，而少的那层判断没人会写。
    """
    src = (text or "").lower()
    if not src:
        return []
    gs = groups if groups is not None else load_groups()
    short = len(src) <= SHORT_QUERY_LEN

    out: list[str] = []
    seen: set[str] = set()
    for g in gs:
        hit = False
        for w in g:
            if len(w) == 1 and not short:
                continue
            if w in src:
                hit = True
                break
        if not hit:
            continue
        for w in g:
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out


# ------------------------------------------------------------------ 繁简
# 两个字一对，连续书写。用字符串而不是 dict/yaml，是因为这张表要手改，
# 而 yaml 里一行 `金 錢钱` 这种"单字 + 字对"混排会让"按 2 字切"错位半步，
# 错了之后所有字都往后挪一位，症状是"繁体查询全都命中不了"——
# 看起来像繁简没生效，实际是解析错位。
#
# 只收**在简体里只有一个对应字**的。"乾"（乾隆/干净）、"著"（著作/显著）
# 这类多对应字不收：猜错了会把整句改歪，比不转更糟。
_T2S_PAIRS = """
個个 們们 這这 說说 時时 麼么 來来 對对 會会 學学 國国 經经 產产 業业 動动
開开 關关 車车 過过 現现 實实 點点 樣样 應应 該该 還还 種种 問问 題题 發发
覺觉 讓让 給给 從从 裡里 後后 與与 為为 無无 數数 據据 網网 電电 話话 聽听
讀读 寫写 語语 詞词 書书 報报 紙纸 機机 構构 條条 總总 結结 論论 標标 準准
務务 員员 內内 兩两 幾几 頭头 體体 長长 間间 買买 賣卖 價价 錢钱 醫医 藥药
歲岁 齡龄 媽妈 兒儿 婦妇 親亲 愛爱 歡欢 樂乐 憂忧 傷伤 難难 尷尴 丟丢 臉脸
氣气 憤愤 習习 慣惯 擔担 憶忆 記记 診诊 檢检 驗验 雙双 單单 邊边 環环
運运 轉转 換换 雖虽 僅仅 夠够 週周 東东 灣湾 臺台 鄉乡 縣县 區区 號号
樓楼 層层 門门 廚厨 衛卫 廳厅 牆墙 燈灯 熱热 涼凉 溫温 職职 場场 闆板
領领 導导 團团 隊队 組组 資资 歷历 績绩 獎奖 費费 稅税 貸贷 債债 帳账
億亿 萬万 營营 額额 潤润 虧亏 損损 賺赚 賠赔 銷销 稱称 復复 療疗 劑剂
針针 掛挂 腫肿 暈晕 燒烧 鬱郁 悶闷 慮虑 緊紧 張张 鬆松 興兴 奮奋 煩烦
嚇吓 憐怜 惱恼 慶庆 賀贺 禮礼 紅红 綠绿 藍蓝 銀银 鋼钢 鐵铁 絲丝 線线
織织 褲裤 襪袜 爺爷 嬸婶 姪侄 孫孙 輩辈 嬰婴 懷怀 計计 劃划 辦办 訊讯
軟软 檔档 傳传 輸输 連连 續续 斷断 啟启 執执 刪删 貓猫 獎奖 麼么
獅狮 豬猪 馬马 鳥鸟 魚鱼 蝦虾 雞鸡 鴨鸭 鵝鹅 驢驴 騾骡 蠶蚕
颱台 檯台 據据 鹼碱 麵面 麪面 麼么 麽么
終终 於于 並并 佔占 係系 確确 識识 認认 將将 屬属 陸陆 軍军
處处 對对 幾几 連连 續续 斷断 準准 復复 雜杂 誌志 週週 週周
邊边 裡里 裏里 麼么 甚什 麼么 樣样 種种 麼么
"""


@lru_cache(maxsize=1)
def _t2s_parse() -> tuple[dict[str, str], tuple[str, ...]]:
    """(映射表, 坏条目)。**按空白切分后每段必须是 2 个字**。

    跳过而不抛异常是刻意的：错一个对不该让整个检索不可用（那张表是数据，
    会被人手改）。但"跳过"必须是**可测的**——坏条目单独返回，
    测试断言它为空，否则一个 4 字的词混进表里会静默失效
    （第一版就是这样把"臺灣"写成 4 个字，结果"臺"根本没转）。
    """
    out: dict[str, str] = {}
    bad: list[str] = []
    for chunk in _T2S_PAIRS.split():
        if len(chunk) != 2:
            log.warning("t2s.bad_pair", extra={"fields": {"pair": chunk}})
            bad.append(chunk)
            continue
        out[chunk[0]] = chunk[1]
    return out, tuple(bad)


def t2s_map() -> dict[str, str]:
    return _t2s_parse()[0]


def t2s_bad_pairs() -> tuple[str, ...]:
    return _t2s_parse()[1]


def to_simplified(text: str) -> str:
    """逐字转简体。词级的歧义字不在表里，所以不用担心改歪。"""
    if not text:
        return text
    m = t2s_map()
    return "".join(m.get(ch, ch) for ch in text)
