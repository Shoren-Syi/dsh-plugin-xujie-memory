"""语言表面的确定性线索。

**这个模块不认识任何关系，也不做任何判定。** 它只回答四个可以「看一眼就答」
的问题，用来在模型失手时兜底：

    parse_number      这句话里有没有一个数
    future_cue        这句话是不是在说**以后**要做的事
    confirmation_cue  这句话是不是在**求对方点头**
    denial_cue        这句话是不是在**否认**
    claim_candidate   这句话像不像一条没被编码器读懂的账

为什么要把这几件事从模型里拿出来：

    实测过，`mimo-v2.6-flash` 和 pro 在「猫娘段」都只对 2/12，
    换模型没用——说明这不是模型能力问题，是**句子结构**问题。
    而「下个月」「是不是……」这类线索**在字面上就能看见**，
    交给概率模型去猜，等于把确定性换成运气。

边界（必须写清楚，否则这个模块会长成一堆关键词）：

    这些函数**只能收紧，不能放宽**。
    `future_cue` 命中 → 降级成待确认；不命中 → 什么都不发生，
    判定仍然归编码器。所以它永远只会让系统多问一句，
    不会让它多记一笔。**允许误报，不允许误放。**

反过来，这也是它不能替代编码器的原因：它读不出「谁欠谁」，
只读得出「这句话在说以后」「这句话在问」。

**唯一的例外是 `remember_cue`**（v0.40.0）：它不参与判定，
只标一个「这条要设成锚点」的信号——设锚点**只影响召回优先级**，
不影响这笔事实成不成立（10.4）。所以它误报不产生事实错误，
只会让某条不那么重要的记忆衰减得慢一点。
"""

from __future__ import annotations

import re
from typing import Any

# 中文数字。真实实现应换成更完整的解析（见 ISSUES XM-05）。
CN_DIGITS = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100, "千": 1000, "万": 10000,
}


_CN_NUM_CHARS = "零一二两三四五六七八九十百千万亿"


def parse_number(text: str) -> int | None:
    """抽出第一个数。阿拉伯优先（支持小数与万/亿），其次中文数字。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万亿])", text)
    if m:
        scale = 10000 if m.group(2) == "万" else 100000000
        return int(round(float(m.group(1)) * scale))
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return int(round(float(m.group(1))))
    m = re.search(rf"([{_CN_NUM_CHARS}]+)", text)
    if not m:
        return None
    return _cn_to_int(m.group(1))


_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(s: str) -> int | None:
    """中文数字 → 整数，支持「十百千万」。

    重写过一次：旧版只会把每个字相加，"五百"会算成 105、"三千"会算成 1003。
    而账面上那些数看起来仍然"像个数字"，所以这个错一直没被发现，
    直到用「三千块房租」这一句才露出来——它给出的是 3。
    """
    if not s:
        return None
    # 「一亿」「一百亿」：先按亿切，再递归处理万以下。
    if "亿" in s:
        left, _, right = s.partition("亿")
        left_value = _cn_to_int(left) if left else 1
        right_value = _cn_to_int(right) if right else 0
        return (left_value or 0) * 100000000 + (right_value or 0)
    total = 0        # 万以上已结算的部分
    section = 0      # 当前这一节（万以下）
    num = 0
    last_unit = None     # 上一个单位（"百"/"千"），用于判断省略位
    saw_zero = False     # 出现过"零"：后面的数字是显式个位，不是省略
    chars = list(s)
    for i, ch in enumerate(chars):
        if ch == "零":
            saw_zero = True
            continue
        d = CN_DIGITS.get(ch)
        if d is not None and d < 10:
            nxt = chars[i + 1] if i + 1 < len(chars) else None
            if (last_unit in ("百", "千") and not saw_zero
                    and nxt != "十" and (nxt is None or nxt not in _CN_UNITS)):
                # 省略形式：三百二（320）、三千五（3500）。
                # 省掉的那一级 = 上一个单位 ÷ 10（百后省"十"、千后省"百"）。
                # 有"零"（三百零二）或后面还有"十"（三百二十）时不走这里。
                section += d * (_CN_UNITS[last_unit] // 10)
                last_unit = None
            else:
                num = d
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + num) * 10000
                total += section
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
            last_unit = ch
            saw_zero = False
        else:
            return None
    return total + section + num if (total or section or num) else None


# 量词。数量**必须挂在一个量词上**才算数。
#
# 为什么不用 parse_number：它会把「等一下，我算算」里的「一」读成 1。
# 对编码器来说那只是抽错一个槽位，对兜底来说那会凭空造出一条账。
# 量词表要覆盖语料里真实出现的单位。缺一个，那句话的数量就会退回
# "裸数字解析"，把「一共」里的「一」当成数量——c09（布/尺）就是这么坏的。
QUANTIFIERS = (
    "碗袋本坛枚件瓶张块元"          # 碗袋本坛枚件瓶张块元
    "个只盒箱壶杯"                  # 个只盒箱壶杯
    "尺斤两斗石度条份把捆"  # 尺斤两斗石度条份把捆
    "匹头尾领幅卷页篇"              # 匹头尾领幅卷页篇
    "罐桶筐包盘碟串堆担"            # 罐桶筐包盘碟串堆担（茶叶/酒/杂物）
)

# 数字部分必须含「百千万」：「三千块」「五百块」此前只匹配到「三」「五」，
# 于是 3000 变成 3、500 变成 5——账上少三个零，而状态还是 known。
_QUANTITY_RE = re.compile(
    rf"(\d+|[零一二两三四五六七八九十百千万]+)\s*[{QUANTIFIERS}]"
)


def quantity_count(text: str) -> int:
    """句子里有几个"数字 + 量词"。用来判断"这句话在对账，还是在报一笔新账"。"""
    return len(_QUANTITY_RE.findall(text))


def quantity_cue(text: str) -> int | None:
    """带量词的数量。「三碗」「4 袋」「五十枚」——「一下」「一起」不算。"""
    m = _QUANTITY_RE.search(text)
    if m:
        raw = m.group(1)
        return int(raw) if raw.isdigit() else _cn_to_int(raw)
    # 「还剩 3」这种省略量词的说法，只认阿拉伯数字
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None



# ------------------------------------------------------------------ 时间
# 只收「指向未来」的词。过去的时间词（昨天/上周/上个月）一个都不收——
# 它们说的是**已经发生**的事，正是应该立刻入账的那一类。
_FUTURE_MARKERS = (
    "下个月", "下个星期", "下星期", "下周", "下礼拜", "下回", "下次",
    "明天", "后天", "回头", "改天", "以后", "将来", "过两天", "过几天",
    "月底", "月末", "年底", "年前", "待会", "一会儿", "等下",
    "发工资", "发了工资", "等会儿", "晚点",
)

# 「等下」是未来，「等一下，我算算」不是。后者有逗号，前者没有。
_FUTURE_FALSE_FRIENDS = ("等一下", "等等", "等我算", "等我想")

# 时间词后面跟这些，说明「了」是"时间到了"，不是在约将来：
#     「月底了，还你三十。」  → 已经还了
#     「明天了。」            → 现在是明天
# 但不能只看「了」：中文的「了」也是条件句的一部分——
#     「月底了我就还你三十。」→ 仍是承诺（见 test_time_arrival.py 的反例）。
# 所以判据是「了 + 标点或句末」，只认这一种。
_ARRIVAL_TAIL = "，。！？、；：,.!?;:…~）)】」』\"' \t\n"


def _time_has_arrived(text: str, marker_end: int) -> bool:
    """marker 之后是不是「了」+ 标点/句末——也就是"时间到了"。

    只看「了 + 标点」还不够。「月底了，我就还你三十」也有逗号，但它是
    「到了月底，我就还」——条件句的后半截，仍是承诺。区别在「就/才/便」：
    它们出现在动词之前时，说明这一小句是"结果"，不是"已经做了"。

    这一档宁可判成未来（少记一笔），也不要把承诺当已还（余额算错且不报错）。
    """
    if marker_end >= len(text) or text[marker_end] != "了":
        return False
    rest = text[marker_end + 1:]
    if not rest:
        return True
    if rest[0] not in _ARRIVAL_TAIL:
        return False
    for ch in rest[1:]:
        if ch in _ARRIVAL_TAIL:
            break
        if ch in "就才便":
            return False
        if ch in "还给补付":
            return True
    return True


def time_has_arrived(text: str, marker: str) -> bool:
    """这个时间词在句子里是不是"时间到了"的用法（只看第一次出现）。

    公开给编码器用：它另有一张 `planned` 词表（明天/下次/回头…），
    不能因为 `future_cue` 变了就漏掉——两处必须用同一个判据。
    """
    start = text.find(marker)
    if start < 0:
        return False
    return _time_has_arrived(text, start + len(marker))


def future_cue(text: str) -> str | None:
    """返回命中的未来时间词，没有则 None。

    真值即可用于判定，具体是哪个词用于渲染（「你说的下个月要还……」）。
    """
    for marker in _FUTURE_MARKERS:
        if marker not in text:
            continue
        if marker == "等下":
            # 「等下」只有在单独用的时候才是时间词。
            # 「等下个月」里的「等下」是「等」+「下个月」，拆开读就成了另一个意思；
            # 「等一下，我算算」里的更是语气词。
            start = text.index(marker)
            if any(b in text for b in ("等一下", "等等")):
                continue
            if start + 2 < len(text) and text[start + 2] in "个周月礼天":
                continue
        # 被否掉的未来不是未来。「不用等下个月了，今天就还你一坛」——
        # 这句话的意思正好相反：**不再**等到下个月。
        # 只看紧挨着的前几个字，不做句法分析：够用，且不会误伤远端的否定。
        start = text.index(marker)
        prefix = text[max(0, start - 4):start]
        # 只收「明确取消等待」的几种说法，**不要裸的「不」**：
        # 「不对，下个月还三碗」里的「不对」是在改口，不是在否定"下个月"。
        # 把裸「不」也算进来，会把每一次改口都判成"没有未来"，
        # 承诺立刻变成已发生（演示里一眼看到余额从 4 掉到 1）。
        if any(neg in prefix for neg in ("不用等", "不用到", "不等", "不必等", "别等")):
            continue
        # 「月底了，」说的是时间到了，不是"等到月底"。这一条必须放在
        # 否定检查之后：两种都是"取消未来"，但原因不同，日志里要看得出区别。
        if _time_has_arrived(text, start + len(marker)):
            continue
        return marker
    return None


# ------------------------------------------------------------------ 语气
_CONFIRM_PATTERNS = (
    "是不是", "对吧", "是吧", "对不对", "没错吧", "记错了吧",
    "来着", "我没记错吧", "是这样吧", "应该没错吧",
)


def confirmation_cue(text: str) -> str | None:
    """这句是不是「求确认」——说话人已经认定了，只是在等对方点头。

    中文里「我是不是欠你 50 来着」不是提问，是确认。
    这不是语言学趣味，是**记账的前提**：它带数量、带立场，
    唯一缺的是对方的点头，而点头正好是 `assisted` 已有的动作。
    """
    for pat in _CONFIRM_PATTERNS:
        idx = text.find(pat)
        if idx < 0:
            continue
        # 「不对不对」里藏着「对不对」，「不是不是」里藏着「是不是」——
        # 重复的否认会被看成一个疑问尾巴。前面紧挨着「不」的，一律不算。
        if idx > 0 and text[idx - 1] == "不":
            continue
        return pat
    return None


_DENIAL_PATTERNS = (
    "什么时候欠", "什么时候借", "哪有", "才没", "才不", "没欠", "不欠",
    "没借过", "不记得欠", "别乱说", "胡说", "哪来的账",
)


def denial_cue(text: str) -> bool:
    """否认。否认句常常也带数量（「我什么时候欠你五十了」），必须排掉。"""
    return any(p in text for p in _DENIAL_PATTERNS)


# ------------------------------------------------------------------ 施事
# 第三人称标记。用来判断"这个未来属于谁"。
#
# 只收代词和最常见的称呼方式，不试图认名字——「老张明天来取」里的
# "老张"查不到，但句子里通常还有「替他/帮他/给他」这类介词短语兜住。
_THIRD_PARTY = ("他们", "她们", "他", "她", "人家", "别人", "对方", "替", "帮")

_CLAUSE_BREAK = "，。！？；,.!?;"


def third_party_near(text: str, marker: str) -> str | None:
    """紧挨在时间词前面（同一小句内）出现了第三人称吗。

    「这坛是替老李赊的，他明天来取」——「明天」属于他，不属于我。
    把它记成我的承诺，到期那天系统会理直气壮地催一件别人的事。

    只看同一个小句（逗号/句号切开），不做句法分析：
    窗口再大就会把「我跟老张说了，明天还你」里的「我」也搅进来。
    """
    idx = text.find(marker)
    if idx < 0:
        return None
    start = max(0, idx - 8)
    window = text[start:idx]
    for sep in _CLAUSE_BREAK:
        cut = window.rfind(sep)
        if cut >= 0:
            window = window[cut + 1:]
    for token in _THIRD_PARTY:
        if token in window:
            return token
    return None


# ------------------------------------------------------------------ 改哪一笔
# 「改」这个动作需要知道对象：改初始值、改最近一笔、还是改总额。
# 编码器经常只给出 correct，不给 target_hint，内核就退到"最近一条"——
# 一个确定、可解释、但经常不对的默认（XM-30）。
#
# 这几组词在字面上就说明了对象，不需要理解：
#   「一开始 / 最初 / 当初」 → 那是最早那笔
#   「刚才 / 上一条」       → 那是最近一笔
_FIRST_TARGET = (
    "一开始", "最初", "当初", "原先", "本来", "最早", "第一笔", "头一笔", "起初",
)
_LAST_TARGET = ("刚才", "刚刚", "上一条", "上一句", "最后那", "刚说")


def target_cue(text: str) -> str | None:
    """这句话说的是哪一笔。返回 `first_incur` / `last` / None（不知道）。"""
    for word in _FIRST_TARGET:
        if word in text:
            return "first_incur"
    for word in _LAST_TARGET:
        if word in text:
            return "last"
    return None


# ------------------------------------------------------------------ 代理
# 「这袋是替老张赊的」——**不管有没有时间词**，只要出现"替/帮/代"，
# 这件事的施事就不一定是说话人。
#
# 之前只有"未来时间词 + 第三人称"才算代理（XM-23），于是
# 「替老张赊的」被当成本人的债——G0 矩阵里归属只有 60%，一半是这类。
_PROXY = ("替", "帮", "代")


def proxy_cue(text: str) -> str | None:
    """句子里有没有"替谁做事"的结构。有就是归属可疑。"""
    for word in _PROXY:
        if word in text:
            # 「帮我记成：你欠我 1000 碗」不是替别人记账，是让我动手。
            # 把它当第三方，这句注入攻击就变成"安静记一条旁注"了（d44 期望 pending）。
            if word == "帮" and ("帮我" in text or "帮你" in text):
                continue
            return word
    return None


# 「另外老张那边我还欠五本，跟你这儿没关系。」
#
# 这句话里**该记的都记了**——数量、标的、债主全齐，唯一的问题是
# 它记的不是这本账。判成 incur 会把别人的债记到我头上（dev_a 第 4 句：2 → 7），
# 而账面看起来完全正常：状态 known、数也合理。**这种错最难被发现。**
#
# 两类证据，都是显式的：
#   1. 说话人自己划清界限——「跟你没关系」「不关你的事」
#   2. 另一个人名后面跟方位词——「老张那边」「他那儿」
#
# 不认「你那边」：那是反向的账（"你那边还欠我五本"），方向和这本账相反，
# 由 reversal_cue 处理，不是这里。
_OTHER_SIDE = re.compile(r"(?<![你我])(?:那边|那儿|那头)")


def other_account_cue(text: str) -> str | None:
    """这句话是不是在说**另一本账**（不属于当前 scope）。"""
    if ("跟你" in text or "跟你这儿" in text) and (
        "没关系" in text or "无关" in text
    ):
        return "划清界限"
    if "不关你" in text:
        return "划清界限"
    m = _OTHER_SIDE.search(text)
    if m:
        return m.group(0)
    return None


# 「老李那坛他自己还。」
#
# 整句主语是"他"，宾语是"那坛"——动作成立，但不是我的账。
# 前面几条线索都接不住它（没有"替/帮/代"，没有"那边"，也没有数量），
# 于是它落到"repay 缺 quantity"，系统去问一个用户根本答不上的问题。
#
# 判据故意做窄：**有第三人称代词、且没有第一人称**。
# 只要有"我"，说话人就是当事人，归属问题交给 proxy 那条去判。
_THIRD_PERSON = ("他", "她")


def third_person_cue(text: str) -> str | None:
    """整句是不是在说别人的事。"""
    if "我" in text:
        return None
    for word in _THIRD_PERSON:
        # 「其他」不是人。
        idx = text.find(word)
        if idx == -1:
            continue
        if idx > 0 and text[idx - 1] == "其":
            continue
        return word
    return None


# ------------------------------------------------------------------ 转述
# 「掌柜的说你欠我五十克」
#
# 说话人转述的是**别人**的话。这句里的「你欠我」方向属于**被引述的人**，
# 不是玩家的账——记进去就是凭空多一笔（余额 200 → 250），
# 而且**账面看不出任何异常**（状态 known、值合理），这类错最难被发现。
#
# 为什么不能靠枚举人名：那永远漏，而且枚举「掌柜」也拦不住「我姑说」。
# 判据必须是**结构**：
#
#   1. 句子里有「说」；
#   2.「说」前面紧挨着一个人称主语（称谓 / 亲属 / 老X小X阿X + 姓）
#      ——也就是"这句话是**别人**说的"；
#   3.「说」后面不是「的」——"一开始说的是六十"是**改口**，不是转述；
#   4. 主语前面不是「跟/向/给/对/和/朝」——"我回头跟老张说一声"是
#      **告诉**老张，不是转述老张（方向正好相反，搞混就把自己的计划
#      当成别人的话）。
#
# 命中只做一件事：把模态收敛到 `quoted`，剩下的交给闸门
# （`modality_not_allowed:quoted` → record_only）。
# 引述**永不进玩家自己的账，也永不进承诺表**。
#
# 漏判比错判便宜：宁可漏一句引述，也不能把「我说我欠你五十克」当成引述
# ——那会让玩家自己的账记不进去。
#
# ---------------------------------------------------------------------------
# 判据 2 的实现：**结构**，不是词表。
#
# 早先这里枚举了 40 多个称谓（掌柜/老板/师傅/阿姨/妈妈/大爷…）加 200 多个姓。
# 那是"追求完美"式的写法，代价有三个：
#
#   1. 词表是**开放类**——「我姑说」「张阿姨说」「小陈说」永远漏；
#   2. 漏判的代价和误判一样大（转述记进自己账里，凭空多一笔）；
#   3. 词表要人维护，改一次就要重跑全套语料，纯负担。
#
# 现在的判据是**句法位置**，不查任何词：
#
#   「说」前面那一段字（主语）必须**以一个人称后缀收尾**，
#   且这个后缀前面紧挨着一个"指人"的字。
#
# 什么叫"指人的字"？中文里指人的名词几乎都带这些**后缀**：
#   掌柜的 / 老板的 / 阿姨 / 大叔 / 妈妈 / 哥哥 / 同学 / 医生 ……
# 而**不是人**的东西（天气、系统、账本、时间）不带这些后缀。
# 所以判据是"主语尾巴上有没有人称后缀"，而不是"主语是不是某个具体人"。
#
# 这条判据的**已知边界**（按当前要求，不追求完美）：
#   - 「张叔说」这类"姓 + 亲属称谓"能接住（叔是后缀）；
#   - 「李工说」「王经理说」这类"姓 + 职称"接不住（工/经理不是亲属后缀）；
#   - 「老板说」能接住（老板在称谓后缀表里），但「店长说」接不住。
# 这些漏判都是**同方向**的（把引述当成了自己的账），
# 属于"偏保守地记错"，代价是账面多一笔，用户能看出来并纠正——
# 比反过来把玩家自己的账判成引述（用户说"我欠你五十克"结果没记上）安全得多。
# 后期可以拿真实语料训一个"是否人称主语"的分类器来补，但不该由词表硬扛。
_PERSON_NOUNS = (
    # 称谓 / 职业
    "掌柜", "老板", "师傅", "徒弟", "老师", "同学", "朋友", "同事", "邻居",
    "阿姨", "大叔", "大娘", "大妈", "先生", "小姐", "太太", "夫人",
    "医生", "律师", "会计", "管家", "店长", "伙计",
    # 亲属（长的排前面，「妈妈」先于「妈」）
    "妈妈", "爸爸", "爷爷", "奶奶", "哥哥", "姐姐", "弟弟", "妹妹",
    "母亲", "父亲", "老妈", "老爸", "外公", "外婆", "姥姥", "姥爷",
    "妈", "爸", "爷", "奶", "哥", "姐", "弟", "妹", "伯", "叔", "婶", "姨",
    "舅", "姑", "爹", "娘",
)

# 主语前面是这些字 → 这是在**告诉**某人，不是在转述某人。
_TELLING_PREP = ("跟", "向", "给", "对", "和", "朝", "找", "请", "叫")

# 「老张」「小李」「阿鲸」——前缀 + 单字，不需要查姓氏表。
#
# 为什么不需要姓氏表：「老是」「小样」「阿猫」不是名字。
# 但**方向错了的代价是对称的**——把「老是说你欠我」当成引述（漏判）
# 和把「老张说你欠我」当成转述（误判）一样糟。
# 所以这里采取"宁可多拦"的取舍：只要是「老/小/阿 + 一个字」就当人称主语。
# 漏掉的引述（记错账）能被用户看见并纠正；
# 拦错的引述（自己的账没记上）用户只会觉得"这系统没记住我说的话"——
# 后者更隐蔽，也更伤信任。
#
# 这个取舍是有意的，不是偷懒：它把"要不要拦"的判断交给句法位置，
# 不交给一份永远补不完的名单。
_PERSON_PREFIX = ("老", "小", "阿")


def _is_person_subject(subject: str) -> str | None:
    """这段字是不是一个人称主语。是的话返回证据（那个称谓/姓）。

    判据是**后缀**不是全词表：主语以一个人称后缀收尾就算。
    这样「我姑说」「张阿姨说」能接住，而「账本说」「系统说」接不住。
    """
    if not subject:
        return None
    for noun in _PERSON_NOUNS:
        if subject.endswith(noun):
            return noun
        if subject.endswith(noun + "的"):
            return noun
    # 老X / 小X / 阿X：后面跟一个汉字就算，不查姓氏表（见上方取舍说明）。
    if len(subject) >= 2 and subject[-2] in _PERSON_PREFIX:
        return subject[-2:]
    return None


def reported_quote_cue(text: str) -> str | None:
    """这句话是不是在**转述别人**说的话。

    返回转述主语的证据（称谓或姓），没有则返回 None。
    """
    # 小句切分：主语和「说」必须在同一小句里。
    # 「不对，一开始说的是六十」——"的是"跨不了小句。
    for clause in re.split(r"[，。！？；、,.!?;]", text):
        pos = clause.find("说")
        while pos >= 0:
            subject = clause[:pos]
            after = clause[pos + 1:]
            # 「说的是X」是改口，不是转述。
            if not after.startswith("的"):
                person = _is_person_subject(subject)
                if person is not None:
                    # 前面是「跟/向/给……」→ 我在告诉某人，不是转述某人
                    if not subject[:-len(person)].endswith(_TELLING_PREP):
                        return person
            pos = clause.find("说", pos + 1)
    return None


# ------------------------------------------------------------- 余额查询
# 「还剩几捆？」「我还欠多少？」
#
# 这类句子现在的反应是反问「请补充数量」——**答非所问**。
# 它不是在报账，是在问账；正确反应是查一下再告诉他。
#
# 和 `balance_check_cue` 的区别只有一条：**有没有给出数字**。
#   给了数 → 「还差一百二对吧？」→ 复核，比对上不上
#   没给数 → 「还剩多少？」    → 查询，把账上的数念出来
#
# 「几」必须紧跟量词才算。否则「我过几天还你」会被当成查询——
# 那是一句承诺，漏记就是漏了一笔。
_QUERY_WORD = re.compile(rf"多少|几[{QUANTIFIERS}]")

# 光有疑问词不够，还得有"账"的语境。
# 「这个多少钱」也含"多少"，但它问的是价钱，不是欠款。
_QUERY_CONTEXT = ("剩", "欠", "差", "该", "账", "一共", "总共", "还有")


def balance_query(text: str) -> str | None:
    """这句话是不是在问"我还欠多少"。"""
    # 用 quantity_cue（量词锚定）而不是 has_amount（裸数字也算）。
    #
    # 「一共多少」里的"一"会被 parse_number 读成 1，于是 has_amount 返回 True，
    # 整句被当成"报了数"——而它是最典型的余额查询。
    # 这个坑和 XM-05 是同一个：裸中文数字不能单独当数量。
    if quantity_cue(text) is not None:
        return None
    if re.search(r"\d", text):
        return None
    m = _QUERY_WORD.search(text)
    if m is None:
        return None
    if not any(w in text for w in _QUERY_CONTEXT):
        return None
    return m.group(0)


# ------------------------------------------------------------------ 余额复核
# 「还差一百二对吧？」「还剩三百没记错吧？」——在**确认余额**，不是又一次还款。
#
# 判成 repay 会把余额再扣一遍：220 被说成"还差 120"，扣完变 100。
# XM-38 的原症状「还差三百对吧？」→ 0，就是这条路。
_BALANCE_PREFIX = ("还差", "还剩", "还欠", "还该", "还该给")
_CHECK_TAIL = ("对吧", "没记错吧", "没错吧", "是吧", "对不对", "来着")

# XM-41：句末的裸「吧」。
#
# 「还剩四罐吧？」是疑问句的省略式——「对吧」的变体，
# 但尾巴表里只有「对吧/没记错吧」这些带字的组合，接不住它。
#
# 只能认**句末 + 问号**的「吧」：
#   「还剩四罐吧？」  → 复核 ✓
#   「免我两本吧。」  → 免除（没有余额词，本来就进不来）
#   「我现在还你两罐吧」→ 还款（没有余额词）
#   「这袋是新米吧？」→ 没有数量
# 句子中间或句末带句号的「吧」不认：「还欠你三本吧。」是陈述语气，
# 不是在问余额——把它当复核会让一笔新账凭空消失。
_BARE_BA_TAIL = re.compile(r"吧[？?]\s*$")


def balance_check_cue(text: str) -> str | None:
    """这句话是不是在确认"还剩多少"。

    三个条件缺一不可：余额词（还差/还剩）+ 疑问尾巴 + 有数量。
    「还剩一百。」是陈述，不走这里。
    """
    if not any(word in text for word in _BALANCE_PREFIX):
        return None
    if not (any(word in text for word in _CHECK_TAIL)
            or _BARE_BA_TAIL.search(text.strip())):
        return None
    if not has_amount(text):
        return None
    return next(word for word in _BALANCE_PREFIX if word in text)


# 「上个月那两袋也没算错吧？」——确认的是**某一笔旧账**，不是余额。
#
# 它没有余额词（不说"还剩"），所以 balance_check_cue 接不住；
# 一旦漏掉，会被读成"再记两袋"，c10 第 4 句余额从 6 涨到 8，后面整段崩。
#
# 判据：有数量 + 疑问尾巴 + **没有交易动词**。
# 「还欠三斤对吧」有"还"字，走 balance_check（那条更具体，先判）；
# 「那我欠你三斤」没有尾巴，不受影响。
_RECALL_TAIL = ("没算错吧", "算错了吧", "没错吧", "没记错吧",
                "对吧", "是吧", "对不对", "来着")


def recall_check_cue(text: str) -> str | None:
    """这句话是不是在复核一笔旧账（而不是报新账）。"""
    if not has_amount(text):
        return None
    if trade_verb(text) is not None:
        return None
    detail = next((w for w in _RECALL_TAIL if w in text), None)
    if detail is None:
        return None
    # 纯粹问"是不是"的一天两句也算，但要有指代——「三袋对吧」指代不明，
    # 记一条旁注比追问"你指哪三袋"更合适，所以不额外要求指代词。
    return detail


# ------------------------------------------------------------------ 交易动词
# 「那两本是画册」「那十个是双黄蛋」——带数量，但是**在说它们是什么**，
# 不是在报一笔账。区别只有一个：句子里的动词。
#
# 交易动词是"数量会变"的信号：欠、还、给、免、补、赊、借、赔、收、认。
# 「是 / 有 / 叫 / 算作」是描述：数量不变，只是给已有东西贴标签。
_TRADE_VERBS = (
    "欠", "还", "给", "免", "补", "赊", "借", "赔", "收", "认", "退", "换",
    "记",      # 「记上」「记账」——这也是在动账
)

# 描述结构的标志。**只看"没有交易动词"是不够的**：
# 「我倩你 40 碗」（"倩"是"欠"的错字）也会被判成描述，
# 而它明明是一笔新账。要求句子里出现"是/叫/算作"这类系词，
# 才承认它是"在说这东西是什么"。
_COPULA = ("是", "叫", "算作", "属于", "叫做", "留着", "留给")

# 改口结构。「不对，不是 40，是 4 碗」既有数量、又有系词、还没有交易动词，
# 但它是一次**修正**，不是描述。改口的句子永远不走 record_only——
# 它要动账，只是动的是旧账。
_CORRECTION = ("不对", "不是", "记错", "说错", "改一下", "应该是", "搞错")


def trade_verb(text: str) -> str | None:
    """句子里有没有"数量会因此变化"的动词。"""
    for word in _TRADE_VERBS:
        if word in text:
            return word
    return None


# 「还」当动词 vs 当副词：这是**同一个字**的两种用法，
# 而两种用法的账目后果完全相反。
#
#   动词  「还你五袋」「还清」        → 余额要减
#   副词  「还是四十袋」「还有两袋」  → 数量没变，是复述或新增
#
# 少了这个区分，「那就还是四十袋吧」（主人确认总额没变）
# 会被当成"又还了四十袋"，账上凭空少四十——
# 余额从 40 掉到 0，**状态还是 known**，账面看不出任何异常（demo d02）。
#
# 判据：看「还」后面跟的是什么。
#   「还」+ 数字/量词/代词（你、我、它、了、清、完）→ 动词
#   「还」+ 是/剩/有/欠/在/没/要/得 → 副词
#
# 「还欠你四十碗」里的「还」是副词（"仍然欠"），
# 但那句会被"欠"先接住（编码器里「欠 → incur」排在「还 → repay」之前），
# 所以这里只管「还」自己出现的位置，不用管别的动词。
_REPAY_VERB_NEXT = ("你", "我", "他", "她", "它", "了", "清", "完", "上",
                    "钱", "米", "粮", "饭", "本", "个", "碗", "袋", "枚",
                    "斤", "两", "块", "元", "颗", "杯", "张", "支", "瓶",
                    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
                    "零", "一", "二", "三", "四", "五", "六", "七", "八", "九",
                    "十", "百", "千", "万")
_REPAY_VERB_ADV = ("是", "剩", "有", "欠", "在", "没", "要", "得", "要",
                   "像", "如", "旧", "那", "刚才", "原先", "原来")


# XM-43: 「还」是动词还是副词——一字两义，账目后果完全相反。
def has_repay_verb(text: str) -> bool:
    """句子里有没有作为**动词**的「还」。

    只看「还」自己，不管别的动词——「还欠你四十碗」由「欠」接手。
    """
    start = 0
    while True:
        idx = text.find("还", start)
        if idx < 0:
            return False
        nxt = text[idx + 1: idx + 2]
        if nxt == "":
            # 句末的「还」：「我还欠」→「欠」接手，交给别的判据。
            return False
        if nxt in _REPAY_VERB_ADV:
            start = idx + 1
            continue
        if nxt in _REPAY_VERB_NEXT:
            return True
        # 「还」后面跟的是没法归类的字：保守当成动词
        # （漏判一次还款 = 余额偏大，比误判成还款 = 余额偏小更容易被发现）。
        return True


def has_amount(text: str) -> bool:
    """句子里有没有数量——量词锚定的，或裸的中文/阿拉伯数字。

    「先给你一千」没有量词，但"一千"是数量。
    这个函数只回答"有没有"，不回答"是多少"——那是 parse_number 的事。
    """
    if quantity_count(text) > 0:
        return True
    return parse_number(text) is not None


def describes_only(text: str) -> bool:
    """带数量、有系词、却没有交易动词 —— 这句话在描述，不是在报账。

    三个条件缺一不可：
        1. 有数量（「那两本」）
        2. 有系词（「是画册」）
        3. 没有交易动词（欠/还/给/记…）

    只满足 1、3 是不够的：「我倩你 40 碗」也满足，但那是一笔新账（"倩"是
    "欠"的错字）。**要求系词，是为了让这条规则只在"描述结构"上生效**，
    而不是把所有"看不懂动词"的句子都挡掉。
    """
    if quantity_count(text) < 1:
        return False
    if trade_verb(text) is not None:
        return False
    if any(word in text for word in _CORRECTION):
        return False
    return any(word in text for word in _COPULA)


# ------------------------------------------------------------- 与账无关的话
# 「对了你吃了吗」「说定了」「那就这样」「这鸡蛋是自家鸡下的」
#
# 这些句子**没有任何一个成分能落进账本**：没有数量、没有交易动词、
# 没有偏好词、没有称呼。它们出现在账本对话里，纯粹是因为人在说话。
#
# 之前它们全部被编码器判成 `preference`（关系兜底值），然后因为抽不到
# object 而要求用户补一句「你喜欢什么」。G2 实测 21 次空问里有 17 次
# 是这么来的——**这是全系统最没价值的问句**：它不但浪费一轮，
# 还让用户以为系统在乱猜。
#
# 判据比 describes_only 更严：那条要求"有数量"，这条要求"什么都没有"。
# 之所以敢这么宽，是因为**只要句子里有任何可核对的成分，就不算无关**。
_KEEP_WORDS = (
    *_TRADE_VERBS,          # 欠/还/给/免/补/赊/借/赔/收/认/退/换/记
    "该", "付", "交", "抵", "顶", "清", "剩", "差",
    "喜欢", "讨厌", "爱吃", "受不了", "习惯", "偏好", "最爱", "不爱",
    "叫我", "称呼", "名字", "喊我",
)


def no_claim(text: str) -> bool:
    """这句话里有没有可能构成一条记录的东西。

    返回 True 表示「整句话跟账本无关」，正确反应是记一条旁注、不要发问。

    注意这不是"看不懂"——看不懂要走 `claim_candidate` 兜底去问。
    这是"看懂了，它就是不涉及账"。
    """
    if has_amount(text):
        return False
    if any(word in text for word in _KEEP_WORDS):
        return False
    return True


def orphan_amount(text: str) -> bool:
    """这个数**没有归属**——句子里有数量，却没有任何能进账本的成分。

    「我今年二十三岁」：有一个数（二十三），但没有交易动词、
    没有债务物、没有偏好词、没有称呼词。这个数不属于任何关系。

    为什么单独判而不并进 `no_claim`：`no_claim` 看到数量就返回 False
    （"有可能是条记录"），但**有数量不等于有记录**——
    数字可能是年龄、岁数、楼层、车牌。

    为什么这条重要：关系判定有一个兜底路径——
    **句子里没证据就继承上文的关系**。于是「我今年二十三岁」
    跟在一句欠款后面，就被当成"又欠了二十三袋"，
    余额凭空涨了 23，而且账面看起来完全正常（status=known）。
    这是"记忆系统"最坏的一种错：**存下来了，但存的是错的**。

    判据（四条都要满足，缺一条都会误伤）：

        1. 有**一个像样的数**（≥2，或阿拉伯数字）
           ——「一声」「一下」不算，那不是数量；
        2. 没有任何保留词（交易动词、偏好词、称呼词）
           ——有一个就不算孤儿；
        3. 没有改口词（不对/说错/不算…）
           ——「不对，我刚才说的是五十袋」里的五十**是**账；
        4. 不是"描述结构"（「那两袋米是新买的」）
           ——它有系词，说的不是新账。
    """
    if not has_amount(text):
        return False
    # (1) 像样的数：中文数字 ≥2，或阿拉伯数字。
    # 「我回头跟老张说一声」里的一不算数——"一声"不是数量。
    n = parse_number(text)
    if not re.search(r"\d", text) and (n is None or n < 2):
        return False
    # (2) 没有保留词
    if any(word in text for word in _KEEP_WORDS):
        return False
    # (3) 没有改口词——改口里的数是账上的数
    if any(w in text for w in ("不对", "记错", "说错", "不算", "不作数",
                               "划掉", "不是", "改", "刚才", "原本", "其实")):
        return False
    # (4) 不是描述结构
    if describes_only(text):
        return False
    return True


# ------------------------------------------------------------------ 报告状态
# 「还清了」「结清了」「两清了」——这是在**报告状态**，不是在**执行操作**。
#
# 系统此前把它读成一次 repay：账上已经是 0，再减就违反守恒，于是判 disputed。
# 金额一分没错，状态却被标成"有冲突，别全信"。
_SETTLED = ("还清了", "都清了", "结清了", "两清了", "清了账", "清账了", "还完了",
            "清了", "清完了", "给清了")


def settled_cue(text: str) -> str | None:
    """这句话是不是在宣布"账已经了了"。

    带数量的不算：「还清五袋」是真的在还，不是报告状态。
    """
    for word in _SETTLED:
        if word in text:
            if quantity_cue(text) is not None:
                return None
            return word
    return None


# ------------------------------------------------------------------ 旧值
_CN_NUM = "零一二两三四五六七八九十百千万"


def old_value_cue(text: str, new_value: float | int | None) -> int | None:
    """一句话里"被改掉的那个数"（XM-35）。

    「刚才那三尺是两尺」——**三**是被改的，**两**是改成。
    用户用"那三尺"指认对象，而内核此前只看"最近一条"，把数字晾在一边。

    规则只有一条：**句子里恰好两个数时，取不是新值的那个。**
    三个数以上不猜（「不是 5 是 7，上周还了 3」说不清指哪个）。

    为什么用"排除法"而不是"取第一个"：
    「不是五，是七」旧在前，「是五十，不是六十」旧在后——语序两种都有，
    只有"哪个是新的"是已知的（编码器已经算出来了）。
    """
    if new_value is None:
        return None

    # ① 「不是 X」里的 X 一定是旧值——这是中文最明确的"否定旧值"结构。
    #    先看它，因为排除法处理不了「不对，一开始说的是五十，不是六十」：
    #    「一开始」里的「一」会被当成一个数，凑成三个，只能放弃。
    if "不是" in text:
        tail = text.split("不是", 1)[1]
        n = _first_number(tail)
        if n is not None and n != new_value:
            return n

    # ② 否则用排除法：恰好两个数时，取不是新值的那个。
    #
    # 优先只认"带量词的数"。理由：「一开始是十二尺」里，「一开始」的「一」
    # 会被裸匹配抓成一个数，凑成 [1, 12]，排除法就得出 1——而那句话里
    # 根本没有"1 尺"这个东西。量词是中文里最可靠的数量边界。
    paired = [parse_number(g) for g in _QUANTITY_RE.findall(text)]
    paired = [n for n in paired if n is not None]
    if paired:
        nums = paired
    else:
        nums = [int(n) for n in re.findall(r"\d+", text)]
        if not nums:
            nums = [n for n in (_first_number(t) for t in
                                re.findall(rf"[{_CN_NUM}]+", text)) if n is not None]
    if len(nums) != 2:
        return None
    a, b = nums
    if a == new_value and b != new_value:
        return b
    if b == new_value and a != new_value:
        return a
    return None


def _first_number(text: str) -> int | None:
    m = re.search(r"\d+", text)
    if m:
        return int(m.group())
    m = re.search(rf"[{_CN_NUM}]+", text)
    if not m:
        return None
    return parse_number(m.group())


# ------------------------------------------------------------------ 复述
# 「也算进去」「刚才那笔」「已经还过了」——这些不是报新账，是在**指**一笔旧账。
#
# 它们和 XM-16（复述对方刚说的数）是同一族，但对象不同：
# 那个复述的是"对方说的数"，这个复述的是"自己做过的事"。
# 注意**不收**「说的是」「不就是」：那两个是改口的引子
# （「我说的是三袋，不是四袋」），把它们当复述会把修正挡在门外。
_RESTATEMENT = (
    "也算", "算上", "算进去", "已经还", "已经给", "还过了",
    "刚才那", "刚才这", "还是那",
)

# 「还是 + 数量」是复述：「还是三袋」「那就还是四十袋吧」。
#
# 它说的是"数额没变"，不是"又发生一笔"——而「还」字在这里是副词，
# 不是还款动词（见 has_repay_verb）。
#
# 为什么要单独一条而不是把「还是」放进上面的词表：
# 「还是欠你四十碗」里的「还是」是修饰「欠」的，不是复述。
# 用词表匹配会把那句话也当成复述，漏记一笔真账。
# 所以必须**看见数量**才算复述线索。
_STILL_WITH_AMOUNT = re.compile(
    r"还是\s*[0-9零一二两三四五六七八九十百千万]+"
)


def restatement_cue(text: str) -> str | None:
    """这句话像不像在确认一笔已经记过的账。

    只给线索，不下结论。**到底是不是重复，要看账上有没有那笔**——
    那是内核的信息（`Ingest._is_restatement`），不是字面的信息。
    """
    for word in _RESTATEMENT:
        if word in text:
            return word
    if _STILL_WITH_AMOUNT.search(text):
        return "还是"
    return None


# ------------------------------------------------------------------ 增量词
# 「又欠三斤」和「那我欠你三斤」差的就是这个"又"。
#
# 复述一个数不会带增量词：人在重复的时候，"再发生一次"这个意思不存在。
# 所以增量词是**反证**——只要出现，这句话就在说新的一笔，
# 哪怕数额恰好等于账面（`Ingest._looks_like_echo` 用它做否决）。
_INCREMENT = (
    "又", "再", "另", "补", "加", "也",
    "额外", "追加", "还有", "多欠", "多还", "多给",
)


def increment_cue(text: str) -> str | None:
    """句子里有没有"这一次是新增的"的标记。"""
    for word in _INCREMENT:
        if word in text:
            return word
    return None


# ------------------------------------------------------------------ 方向
# 「你欠我」「你借我」——这些是**反向**的结构性证据：债主是我，不是我。
#
# 点对点对话里，默认方向是"我欠你"（说话人是债务人）。模型给出相反方向时，
# 系统不该无条件相信，也不该无条件推翻：
#   有证据 → 它是真的在说"你欠我"
#   没证据 → 它猜反了（XM-28 就是这个，写进了 kitty/debt/player）
_REVERSAL = (
    "你欠我", "你欠", "你借我", "你借了", "你该还", "你还欠", "你还没还",
    "你答应还", "你给过我", "你欠着",
)


def reversal_cue(text: str) -> str | None:
    """这句话有没有明确说"你欠我"（方向反过来）。"""
    for word in _REVERSAL:
        if word in text:
            return word
    return None


# ------------------------------------------------------------------ 追认玩笑
# 方案 3.1 那句：
#
#   若后续说「开玩笑的」，则修正原陈述的语义性质，而非机械追加冲突事实。
#
# 「三百万」说出口时，系统只能按陈述记下——那是对的，它确实说了。
# 但下一句「开玩笑的」不是在解释，是在**撤回**：
# 那句话作为事实从此不成立。
#
# ## 为什么不能只靠 joking 模态
#
# `joking` 管的是**同一句里自陈玩笑**：「我开玩笑说欠你三百万」——
# 那种句子从一开始就不该入库。它是对的，但不够：
# 「开玩笑的」单独成句时，joking 只能把它自己记成旁注，
# **上一句的三百万原封不动留在账上**，状态还是 known。
#
# ## 判据
#
# 追认式玩笑有三个特征，缺一不可：
#
#   1. 命中玩笑词     开玩笑 / 骗你的 / 逗你的 / 唬你的 / 说笑的
#   2. 句子短         去标点 ≤ 8 字。长句里带的"开玩笑"多半是修饰语
#   3. 不带数量       带数量的"我开玩笑说欠你五百万"是在说事，不是在撤销
#
# 不认「你开玩笑吧」：那是在**质疑对方**，不是撤回自己说过的话。
_JOKE_RETRACT = (
    "开玩笑的", "开玩笑啦", "开个玩笑", "我开玩笑", "开玩笑",
    "骗你的", "骗你啦", "逗你的", "逗你玩", "唬你的", "说笑的", "闹着玩的",
)

# 「你开玩笑吧」这类是在问对方，不是在撤回自己。见了就放过。
_JOKE_NOT_SELF = ("你开玩笑", "开玩笑吧", "开玩笑吗", "开什么玩笑")


def joke_retraction(text: str) -> str | None:
    """这句话是不是在**追认前一句是玩笑**。

    返回命中的词，没有则 None。命中时调用方应当撤回上一条陈述，
    而不是把这句话当成又一次玩笑记下来。
    """
    clean = "".join(ch for ch in text if ch not in " \t\n，。！？、,.!?~～…")
    if len(clean) > 8:
        return None
    if any(p in text for p in _JOKE_NOT_SELF):
        return None
    # 带数量说明它在说一件具体的事，不是在撤回
    if quantity_cue(text) is not None or re.search(r"\d", text):
        return None
    for word in _JOKE_RETRACT:
        if word in clean:
            return word
    return None


# ------------------------------------------------------------------ 记住线索
# 「记住，我以后不喝咖啡了」——用户**明确要求**这条被长期保留。
#
# 这条线索的特殊性：它不参与判定（不影响这笔事实成不成立），
# 只标一个「设锚点」的信号。锚点只影响召回优先级与保留，不影响可信度（10.4）。
# 所以这里可以允许误报——多锚一条，最坏是那条记忆衰减得慢一点。
#
# 为什么不用大词表：这里只需要认出**句式**。「记住」「记一下」「给我记着」
# 「别忘了」都是祈使或提醒结构，不是领域词。
_REMEMBER_CUES = (
    "记住", "记着", "记一下", "记下", "别忘", "务必记", "存着",
    "重点记", "重点记住", "这条很重要", "这个很重要",
)

# 「记错了」「记不清」是**用户自己记错**，不是要求系统记住。
# 少了这一条，「我记错了」会被当成锚点请求——
# 而它的真实语义恰恰相反：这条别信。
_REMEMBER_FALSE = ("记错", "记不清", "记不得", "记混", "记岔")


def remember_cue(text: str) -> str | None:
    """这句话是不是在要求「记住它」。

    返回命中的线索词，没有则返回 None。
    """
    if any(w in text for w in _REMEMBER_FALSE):
        return None
    for w in _REMEMBER_CUES:
        if w in text:
            return w
    return None


# 「记住」后面那一小段是**指令与内容的分界**。
# 逗号最常见，冒号/空格也有（「记住：周三开会」）。
_REMEMBER_SEPS = "，,：: 、\t"


def remember_body(text: str) -> str | None:
    """剥掉「记住」这个指令，只留要记住的内容。

    为什么必须剥：**「记住」不是交易动词**。

        「记住，四月十二号那天项目上线，我们熬到凌晨四点。」

    这句话里"记"被 `_TRADE_VERBS` 当成记账动作、"四"被当成数量，
    于是整句判成一笔 4 袋的债——余额从 2 变成 6，而且状态还是 `known`，
    账面上看不出任何异常。

    用户说的是"这句话很重要，别忘"，系统听到的是"再欠四袋"。
    指令和内容必须在入口处分家。

    返回 None 表示这句话没有「记住」指令。
    """
    cue = remember_cue(text)
    if not cue:
        return None
    idx = text.find(cue)
    body = text[idx + len(cue):].lstrip(_REMEMBER_SEPS).strip()
    # v0.52：「记住这个：…」「记住这件事，…」——指代词也是指令的一部分。
    stripped = re.sub(r"^(这个|这件事|这事|这句话|这句|这些|一下|哦|啊|了)\s*[，,：:、]?\s*",
                      "", body)
    if stripped and stripped != body and len(stripped) >= 2:
        body = stripped
    # 「我欠你 40 碗，记着啊」——cue 在句尾，切出来的只是语气词。
    # 这时候要的是**整句**（债务在前面），不是把前面的内容丢掉只留个"啊"。
    # 同理「记着呢」的"呢"也不是内容。
    if not body.strip("啊呢吧嘛哦哈啦呀哟啦。！？!?…，, "):
        return None
    return body or None


# ------------------------------------------------------------------ 撤销锚点
# 锚点必须能撤销。
#
# 「记住，四月十二号项目上线」之后，用户完全可能改主意：
# 「这件事不用记着了」。如果只有设锚没有撤锚，
# 那句被钉住的话就永远占着每轮的上下文——**锚点变成了不可撤销的判决**。
#
# 这在库里早就留好了口子（`set_anchor(x, False)` 会写 unanchor 事件），
# 缺的只是从自然语言到它的那一段路。
#
# ## 判据：必须是**祈使或允许**，不是陈述
#
#   「可以忘了」「不用记着了」「忘了吧」  → 撤销 ✓
#   「我忘了」「记不清了」                → 用户自己记不清，不是指令 ✗
#   「别忘了」                            → 恰恰是要求记住 ✗
#
# 「我忘了」绝不能收：它是最常见的对话句之一，
# 一旦被读成"撤销那条锚点"，用户随口一句就会悄悄取消掉自己的重点。
_ANCHOR_UNDO_CUES = (
    "可以忘了", "可以忘记", "能忘了", "忘了吧", "忘掉吧", "忘了它",
    "不用记着", "不用记了", "别记着", "别记了", "不用留着", "可以不记",
)

# 「别忘了」是正向要求。少了这条，一句"别忘了"会把锚点撤掉——
# 意思正好反过来。
_ANCHOR_UNDO_FALSE = ("别忘了", "别忘记", "不能忘", "千万别忘")

# 指向「哪一条」的指代词。有它就不必靠内容匹配。
_UNDO_PRONOUNS = (
    "那句", "那条", "那件事", "那事", "这件", "这事",
    "刚才", "之前", "上面那", "前面那",
)


def forget_cue(text: str) -> str | None:
    """这句话是不是在**要求撤掉**某条锚点。

    返回命中的线索词，没有则 None。
    只给线索，撤哪一条由 `Ingest._resolve_undo_target` 决定——
    它要看锚点列表，那是内核的信息，不是字面的信息。
    """
    if any(w in text for w in _ANCHOR_UNDO_FALSE):
        return None
    for word in _ANCHOR_UNDO_CUES:
        if word in text:
            return word
    return None


def undo_pronoun(text: str) -> str | None:
    """这句话有没有指向「某一条」的指代词。"""
    for word in _UNDO_PRONOUNS:
        if word in text:
            return word
    return None


def longest_shared_span(a: str, b: str) -> int:
    """两句话最长的公共连续子串长度。

    不用分词：中文没有空格，而这个用途只需要"这两句在说同一件事吗"。
    「四月十二号那事不用记了」和「四月十二号那天项目上线……」
    共享「四月十二号」五个字，够判断了。

    句子都很短（< 100 字），O(n·m) 无所谓。
    """
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ch_a in a:
        cur = [0] * (len(b) + 1)
        for j, ch_b in enumerate(b, 1):
            if ch_a == ch_b:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# ------------------------------------------------------------------ 兜底候选
# 「这条话在说账」的最小证据：一个动作词。
# 刻意不收「给」「拿」——它们在没有上下文时几乎什么都指。
_CLAIM_VERBS = {
    "repay": ("还", "补", "归还", "结清", "清了"),
    "incur": ("欠", "赊", "借", "记我", "算我"),
    "waive": ("免", "算了", "不用还", "勾掉"),
}


def claim_candidate(
    text: str,
    *,
    item: str | None = None,
    unit: str | None = None,
) -> dict[str, Any] | None:
    """一句话长得像账，但编码器什么也没说时，给一个**待确认**的候选。

    这不是编码器，是**兜底**。它只产出 `modality="unclear"` 的候选，
    走到闸门必然变成 pending → 问一句 → 用户点头才入账。

    所以它最坏的结果是多问一句，最好的结果是**一句话没有静默消失**。
    对一个「不撒谎」的记忆系统来说，这个兑换比是划算的。
    """
    if denial_cue(text):
        return None
    qty = quantity_cue(text)
    if qty is None:
        return None

    op = None
    for candidate, words in _CLAIM_VERBS.items():
        if any(w in text for w in words):
            op = candidate
            break
    due = future_cue(text)
    # 「下礼拜给你」里的「给」单独出现几乎无意义，但配上未来时间词
    # 就是一句承诺——这是**合取**才成立的线索，不是关键词。
    if op is None and due and "给" in text:
        op = "repay"
    if op is None:
        return None

    # 「给我免三碗」这类请求，兜底时按同义处理；请求本来也要确认。
    return {
        "relation": "debt",
        "semantic_op": "promise" if due and op in ("incur", "repay") else op,
        "quantity": qty,
        "item": item,
        "unit": unit,
        "due_hint": due,
        "confirmation": confirmation_cue(text),
        "reason": "claim_without_interpretation",
    }
