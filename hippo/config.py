"""配置加载与校验（M0.2 的 M1 所需子集）。

校验策略：
  - 结构错、类型错 → 立刻抛错，并指出是哪个字段。
  - 未知字段 → 只告警，不阻断（方便用户提前写未来版本的配置）。
  - 环境变量覆盖：HIPPO_PACK__MEMORY_TOKENS=300（双下划线表示层级）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.default.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class DeepSeekCfg(_Section):
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    extract_model: str = "deepseek-flash"
    temperature: float = Field(1.1, ge=0, le=2)
    api_key: str = ""
    timeout: float = Field(120.0, gt=0)
    # 显式 enabled/disabled 优先；空值时官方 deepseek-flash 的抽取关闭思考，
    # 其他上游不发送该字段，以保留兼容性。
    thinking: str = ""


class PricingCfg(_Section):
    """元 / 百万 token。价格会变，改这里，别改代码。"""

    input_miss_per_m: float = Field(2.0, ge=0)
    input_hit_per_m: float = Field(0.5, ge=0)
    output_per_m: float = Field(8.0, ge=0)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class SessionCfg(_Section):
    header: str = "X-Hippo-Session"
    default: str = "main"


class BudgetCfg(_Section):
    daily_tokens: int = Field(500_000, ge=0)
    # degrade = 降级（M27.3）；off = 只统计不改变行为。
    # 陌生值一律按 degrade 处理——宁可少记，不可拒绝对话。
    on_exceed: str = "degrade"


class PackCfg(_Section):
    persona_tokens: int = Field(500, ge=0, le=4000)
    cards_tokens: int = Field(400, ge=0, le=4000)
    pinned_tokens: int = Field(200, ge=0, le=4000)
    memory_tokens: int = Field(600, ge=100, le=4000)
    dynamic_tokens: int = Field(150, ge=0, le=4000)
    time_anchor_last: bool = True


class ReflectCfg(_Section):
    """让对话模型"顺手"参与纠错，而不是插件自己硬扛。

    人会说"不对，是十四号不是十五号"——这类自我反思本来就该由模型来做，
    但**不能让模型每轮都反思**：那等于每轮多一段指令 + 更多输出 token。

    所以只在两种轮次里注入一行提示（约 20 token）：
      - 本轮正在问一个存疑问题（记忆本身不确定）
      - 本轮注入了"当前事实"（模型有可对照的值）
    其余轮次一律不注入。这是"必要时"的边界。
    """

    # **默认关闭**：往系统提示里塞"请纠正我"这类指令是有感的——它会改变模型
    # 行为、多付 token，还可能让回复出现"我注意到我的记忆有误"这种痕迹。
    # 主人的要求是"绝对无感"，所以正确做法是**被动挖掘**：从用户/模型自己的
    # 自然语言里识别纠正（"不对，是十四号不是十五号"），插件静默更新，
    # 不额外调模型、不改提示词、不增加延迟。
    # 这个开关只在"主动请模型反思"的实验分支里用，生产默认关。
    correction_hint: bool = False
    marker: str = "[纠正]"
    # 被动纠正每天最多应用几条（防止把闲聊里的"不对"全当纠正）
    max_passive_per_day: int = 3


class BackupCfg(_Section):
    """备份：每日自动一份 + 面板手动入口。

    `ensure_daily` 以前只在 CLI 的 serve 里调，插件运行期从不备份——
    数据安全功能藏在命令行里，等于没有。
    """

    enabled: bool = True
    keep: int = 7
    daily: bool = True


class MaintenanceCfg(_Section):
    """自动维护：权重/tier 重算 + 本地巩固。

    这两个能力以前只挂在 CLI 上（recompute / consolidate），
    插件跑起来从没执行过——"设计了但没用"。
    这里做成后台任务：默认每天一次，纯本地、不调模型。
    """

    enabled: bool = True
    recompute_hours: int = 24
    consolidate_hours: int = 24
    # 巩固是否重写摘要。默认关：摘要重写要调模型，关键词并集已保证检索不丢。
    consolidate_summary: bool = False


class DigestCfg(_Section):
    """会话摘要的自动生成策略。

    摘要是"任务型对话里唯一总能捞回上下文"的东西，但只在 /drain 生成等于没有
    ——DSH 很少走到 drain。所以改成后台轮询：攒够 `min_new_turns` 轮，或者
    空闲 `idle_minutes` 分钟且有新内容，就补一条；同一条会话最多每
    `refresh_minutes` 分钟重写一次，避免每轮都花一次调用。
    """

    enabled: bool = True
    min_new_turns: int = 8
    refresh_minutes: int = 30


class RetrieveCfg(_Section):
    # 事件没命中时，是否把 turns 里的原话兜底带上来（L1_RAW 等价层）。
    # 关掉可以复现旧行为，用于 A/B 对比。
    raw_turns: bool = True
    # 二级兜底：关键词捞不到原话时，带最近两条用户原话。
    # **默认关闭**：143 题 A/B 里它没有带来任何新增命中（terms 60 → terms+recent 60），
    # 只会多占 token。代码保留，将来改成"按问题类型"再开。
    raw_recent: bool = False
    # 跨会话：事实/特征属于"这个人"而不是"这次对话"。
    # 事件、计划、存疑仍严格按会话隔离。
    cross_session: bool = True

    trigger_jaccard: float = Field(0.25, ge=0, le=1)
    max_summaries: int = Field(8, ge=1, le=50)
    max_raws: int = Field(2, ge=0, le=10)
    min_weight: float = Field(0.15, ge=0, le=1)
    max_candidates: int = Field(800, ge=1)
    # M27.1 后缀兜底：库里的词以查询片段结尾时也算命中（"陈医生" ⊃ "医生"）。
    # 中文的"姓+职业""地名+中心词"都是这个结构，前缀一个都捞不到。
    # 代价是一次用不上索引的全扫（8000 条事件约 1.6ms），只在精确/前缀
    # 全空时才跑。关掉它 = 回到 M26 的行为（P23 会漏）。
    suffix_fallback: bool = True
    # "最近/这几天"没有明确边界，用一个固定窗口兜住（M9.2）。
    # 30 天是"还能叫最近"的上限：再长用户会说"前阵子"，再短会漏掉"上个月聊的那事"。
    recent_days: int = Field(30, ge=1, le=365)

    # ---- M11 原话层 ----
    # 一条原文最多给多少。400 是手册的数：事件本身在前 400 token 就说完，
    # 后面通常只剩对话余波。调大几乎只增加成本，不增加信息。
    raw_max_tokens: int = Field(400, ge=50, le=2000)
    # 剩余预算低于这个数就别拉了——拉一条 50 token 的残句比不拉更坏，
    # 模型会把残句当完整信息用。
    raw_min_budget: int = Field(200, ge=0, le=2000)

    # 两次检索的最小间隔（轮）。0 = 关闭。
    #
    # 默认关闭是**实测结论**，不是偷懒：在 99 轮标定集上，间隔 1 轮能把
    # 触发率从 44% 压到 28%，但漏触发从 16% 涨到 55%——它挡掉的大部分是
    # 真的换了话题。用"AI 忘事"换"省钱"，这笔账不划算（见 R-12.3）。
    # 留着这个开关，是因为有人可能更在意成本。
    min_interval_turns: int = Field(0, ge=0, le=20)


class RankCfg(_Section):
    """L2 排序权重（M10.2）。全放这里是为了 M26 调参时不用改代码。

    五个权重加起来 >1 是刻意的：分数不是概率，是排序键。
    归一化只会让"每加一项就要动其他四项"，调参时反而看不清单项的影响。
    """

    w_keyword: float = Field(0.55, ge=0, le=1)
    w_weight: float = Field(0.25, ge=0, le=1)
    w_recency: float = Field(0.10, ge=0, le=1)
    w_pinned: float = Field(0.10, ge=0, le=1)
    # M9 的 D61 把"最近"从硬过滤降级成软信号，这一项就是它的落点。
    # 设 0 = 关掉"最近"的加权效果，检索退回纯相关度。
    w_soft_since: float = Field(0.05, ge=0, le=1)

    # 单条摘要的 token 上限。80 是手册给的数：一条记忆超过 80 token，
    # 说明模型在写摘要而不是在提炼，M13 的 600 token 预算装不下 3 条。
    summary_max_tokens: int = Field(80, ge=20, le=400)
    # 记忆区能用掉 PackCfg.memory_tokens 的多少。剩下的留给 L3 原文。
    memory_budget_ratio: float = Field(0.6, ge=0.1, le=1)


class DecayCfg(_Section):
    half_life_days: dict[str, float] = Field(
        default_factory=lambda: {"hot": 3, "warm": 30, "cold": 180}
    )
    recall_bonus: float = Field(0.20, ge=0, le=1)
    arousal_bonus: float = Field(0.30, ge=0, le=1)
    pinned_multiplier: float = Field(2.0, ge=1)


class WeightCfg(_Section):
    """M14 权重参数。与上面的 `DecayCfg` 是**两件不同的事**，别混：

    - `DecayCfg` 管 **排序用的新近度**（`rank._recency`，按 event_time 算，
      回答"这件事多久以前发生的"）
    - `WeightCfg` 管 **记忆本身的衰减**（`core.weight`，按 last_access 算，
      回答"这条记忆多久没被想起来了"）

    一个影响排序，一个影响存亡。混在一起的后果是：讲一件 2019 年的旧事，
    它会被判成"极旧"而立刻沉底——但它其实是今天才说的。
    """

    # 半衰期按 w0 分档（不是按 tier）：[[w0 下界, 天数], ...] 从高到低匹配。
    # 方向是"越重要忘得越慢"，与手册的 hot=3 相反，理由见 core/weight.py。
    half_life_by_w0: list[list[float]] = Field(
        default_factory=lambda: [[0.70, 180.0], [0.40, 30.0], [0.0, 3.0]]
    )
    pinned_half_life: float = Field(36500.0, gt=0)   # 100 年 = 不衰减
    recall_hl_gain: float = Field(0.25, ge=0, le=2)  # 每召回一次，半衰期 ×(1+g)
    recall_cap: int = Field(5, ge=0, le=50)
    hot: float = Field(0.70, ge=0, le=1)
    warm: float = Field(0.30, ge=0, le=1)
    hysteresis: float = Field(0.05, ge=0, le=0.2)
    # 每日重算的批量大小。百万级库要分批提交，否则一个事务写太久，会挡住读。
    batch: int = Field(2000, ge=1)


class ExtractCfg(_Section):
    # 关掉它 = 只记录对话、不记忆。调试代理透传时有用，也给"我只想要原文"的人留条路。
    enabled: bool = True
    every_n_turns: int = Field(5, ge=1)
    # 首次抽取的门槛。真实使用里用户很少连发 5 条，攒不够就一条记忆都没有，
    # 界面上看着像"装了没用"。所以会话第一次抽取只要求 2 条用户消息，
    # 之后照旧 every_n_turns。设成 0 或 >= every_n_turns 等于关闭这条加速。
    first_extract_user_turns: int = Field(2, ge=1)
    # 二次抽取：一个窗口抽出 0 条、且本地判断"这像在定义规则/数值/格式"时，
    # 带着定向指令再问一次。**先本地判断再花钱**——不是每个空窗口都补。
    #
    # **默认关闭**：194 题实测，28 次额外调用只多抽出 3 条事件，
    # 判定反而 127→123（噪声内）。成本优先，不划算，默认不开。
    retry_empty: bool = False
    # 代码窗口是否附加「工程记忆」提示词（纯本地判断，零额外调用）
    coding_prompt: bool = True
    retry_min_chars: int = Field(20, ge=8, le=1000)
    window_tokens: int = Field(1200, ge=100)
    prescreen: bool = True
    # M29：预筛模式。
    #   rules     —— 只认词表（M4 原行为，真实对话里漏抽严重）
    #   heuristic —— 规则命中即放；不中时按信息量启发式判断（默认）
    #   off       —— 全放（每 5 轮都抽，成本最高）
    prescreen_mode: Literal["rules", "heuristic", "off"] = "heuristic"
    # heuristic 模式长度门槛；带数字的短句不受此限（"5500" 也有信息）
    heuristic_min_chars: int = Field(8, ge=4, le=40)
    # 留空 = 用包内自带词表。想调"它多大程度愿意记事"，改这里指向自己的 yaml。
    lexicon_path: str = ""
    # ---- M5 调用参数 ----
    # 0.2 是刻意的：抽取要稳定，不要创意。用户说"我搬去上海"，
    # 这次抽成"搬到上海"、下次抽成"定居上海"，检索就会分裂。
    temperature: float = Field(0.2, ge=0, le=1)
    max_tokens: int = Field(1500, ge=200, le=8000)
    # 摘要不合格时是否再调一次模型修。关掉 = 只截断不重写，省钱但摘要会变差。
    summary_repair: bool = True
    # 30s 是照 DeepSeek 的经验设的，实测在 mimo 上不够：
    # 关思考的真实抽取要 15~22s，网络抖动一下就顶到 30s 超时。
    # 超时的代价是"这一窗记忆降级成原文"，比多等 60 秒贵得多。
    timeout: float = Field(90.0, gt=0)
    retries: int = Field(1, ge=0, le=3)
    # 抽取全失败时，至少把窗口原文写成一条 cold 事件。关掉这个开关
    # 等于允许"记忆静默丢失"，只在测试里关。
    degrade_to_raw: bool = True
    # 每窗口最多带多少个已知人物进 prompt。太多会喧宾夺主，
    # 而且人物表越长模型越倾向"套用"现有名字。
    known_entities_limit: int = Field(40, ge=0, le=200)


class DoubtsCfg(_Section):
    max_per_turn: int = Field(1, ge=0, le=3)
    cooldown_turns: int = Field(8, ge=0)
    max_ask_count: int = Field(2, ge=0)
    expire_days: int = Field(7, ge=1)
    max_active: int = Field(20, ge=1)


class AffectCfg(_Section):
    baseline: dict[str, float] = Field(
        default_factory=lambda: {
            "joy": 60, "sadness": 10, "anger": 5,
            "fear": 5, "disgust": 5, "surprise": 20,
        }
    )
    tau_minutes: dict[str, float] = Field(
        default_factory=lambda: {
            # M21.2 校准后的值（原手册值 joy180/sadness360/anger10/fear30/
            # disgust240/surprise3，fear/sadness/surprise 按直觉拟合调整）
            "joy": 180, "sadness": 480, "anger": 10,
            "fear": 14, "disgust": 240, "surprise": 5,
        }
    )
    delta_cap: float = Field(20, ge=0, le=100)


class CuriosityCfg(_Section):
    daily_quota: int = Field(3, ge=0)
    only_readonly: bool = True
    quiet_hours: list[int] = Field(default_factory=lambda: [23, 8])
    # 队列长度。手册取 top-5：再多也只是"备选"，反正一天最多说 3 条。
    queue_size: int = Field(5, ge=1)
    # 30 分钟内有过对话 → 推迟。用户正聊着，AI 在回复里顺手问就行，
    # 不需要后台另起一句。
    busy_minutes: int = Field(30, ge=0)
    # 说完多久没人接算"没回应"。24 小时：当天没回，第二天就不追了。
    response_hours: int = Field(24, ge=1)
    # 排队多久作废。三天前想问的一句"你搬家了吗"，今天冒出来只会莫名其妙。
    pending_hours: int = Field(6, ge=1)
    # 同一个缺口问过后多少天内不再问。
    refractory_days: int = Field(3, ge=0)
    # 低置信 trait / 失效 fact 的"多久没更新才算缺口"。
    stale_days: int = Field(30, ge=1)


class GiveupCfg(_Section):
    levels: list[str] = Field(
        default_factory=lambda: ["retry_alt", "ask_help", "partial", "decline"]
    )
    require_reason_on: str = "decline"


class HeartbeatCfg(_Section):
    interval_seconds: int = Field(60, ge=1)
    max_llm_calls_per_day: int = Field(10, ge=0)
    # M24.1 各步频率。mood 每 5 分钟、tier 每 60 分钟、consolidate 每天
    # 03:00 —— 手册定的三个数。都放进 config 是为了能在真机上量，
    # 不是为了给用户调（调了也没有收益，只会让心跳变贵）。
    mood_seconds: int = Field(300, ge=30)
    tier_seconds: int = Field(3600, ge=60)
    consolidate_hour: int = Field(3, ge=0, le=23)
    # M24.3 升级调用的硬约束：上下文 ≤300 token、max_tokens=120、温度 0.8
    escalate_max_context: int = Field(300, ge=50, le=2000)
    escalate_max_tokens: int = Field(120, ge=16, le=1000)
    escalate_temperature: float = Field(0.8, ge=0, le=2)


class Config(BaseModel):
    model_config = ConfigDict(extra="allow")

    db_path: str = "~/.hippo/mem.db"
    timezone: str = "Asia/Shanghai"

    deepseek: DeepSeekCfg = Field(default_factory=DeepSeekCfg)
    pricing: PricingCfg = Field(default_factory=PricingCfg)
    session: SessionCfg = Field(default_factory=SessionCfg)
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    pack: PackCfg = Field(default_factory=PackCfg)
    digest: DigestCfg = Field(default_factory=DigestCfg)
    maintenance: MaintenanceCfg = Field(default_factory=MaintenanceCfg)
    backup: BackupCfg = Field(default_factory=BackupCfg)
    reflect: ReflectCfg = Field(default_factory=ReflectCfg)
    retrieve: RetrieveCfg = Field(default_factory=RetrieveCfg)
    rank: RankCfg = Field(default_factory=RankCfg)
    decay: DecayCfg = Field(default_factory=DecayCfg)
    weight: WeightCfg = Field(default_factory=WeightCfg)
    extract: ExtractCfg = Field(default_factory=ExtractCfg)
    doubts: DoubtsCfg = Field(default_factory=DoubtsCfg)
    affect: AffectCfg = Field(default_factory=AffectCfg)
    curiosity: CuriosityCfg = Field(default_factory=CuriosityCfg)
    giveup: GiveupCfg = Field(default_factory=GiveupCfg)
    heartbeat: HeartbeatCfg = Field(default_factory=HeartbeatCfg)

    @property
    def db_file(self) -> Path:
        return Path(os.path.expanduser(self.db_path))

    def resolve_api_key(self, header_value: str | None = None) -> str:
        """优先级：请求头 > 配置 > 环境变量。

        请求头优先是刻意的：一个代理可以被多个客户端共用，
        各自的 key 不该互相串。环境变量垫底，方便容器部署。
        """
        if header_value:
            v = header_value.strip()
            if v.lower().startswith("bearer "):
                v = v[7:].strip()
            if v:
                return v
        return self.deepseek.api_key or os.environ.get("DEEPSEEK_API_KEY", "")


def _env_overrides() -> dict[str, Any]:
    """HIPPO_DEEPSEEK__MODEL=... → {"deepseek": {"model": ...}}"""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("HIPPO_") or "__" not in key:
            continue
        parts = [p.lower() for p in key[len("HIPPO_"):].split("__")]
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = _coerce(value)
    return out


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _log():
    """配置层自己的日志句柄：不依赖 core.log，避免循环导入。"""
    import logging
    return logging.getLogger("hippo.config")


def load_config(path: str | Path | None = None, *, env: bool = True) -> Config:
    p = Path(os.path.expanduser(str(path))) if path else Path.home() / ".hippo" / "config.yaml"
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw = loaded if isinstance(loaded, dict) else {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            # 配置文件坏了不能让插件"静默死亡"：用户改错一个缩进，
            # 结果整个记忆功能起不来，现象是"装完没反应"，极难排查。
            # 退到默认值继续跑，并把原因写进日志。
            _log().warning("配置读取失败，改用默认值：%s", exc)
            raw = {}
    if env:
        raw = _deep_merge(raw, _env_overrides())
    try:
        return Config(**raw)
    except Exception as exc:  # noqa: BLE001 - 字段名写错/类型不对
        _log().warning("配置内容不合法，改用默认值：%s", exc)
        return Config()


def default_config_text() -> str:
    header = (
        "# 海马体插件 · 配置\n"
        f"# 由 `hippo init` 于 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M} 生成\n"
        "# 完整说明见《完整手册》第一部分 §19\n\n"
    )
    return header + DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
