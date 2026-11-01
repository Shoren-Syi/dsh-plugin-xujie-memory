-- Xujie-Memory 参考存储模式（阶段 1）
-- 对应设计文档 17.1。此处只实现内核所需表；clarifications / outbox / recall_log
-- 属后续阶段，留空不影响内核语义。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = FULL;

-- ---------------------------------------------------------------- 作用域
CREATE TABLE IF NOT EXISTS scopes (
    scope_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    world_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (tenant_id, agent_id, world_id)
);

-- ---------------------------------------------------------------- 痕迹（不可变）
CREATE TABLE IF NOT EXISTS traces (
    trace_id             TEXT PRIMARY KEY,
    scope_id             TEXT NOT NULL REFERENCES scopes(scope_id),
    source_ref           TEXT NOT NULL,
    actor_id             TEXT NOT NULL,
    source_kind          TEXT NOT NULL,
    content              TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    source_time          TEXT,
    source_time_precision TEXT,
    context_ref          TEXT,
    erased_at            TEXT,
    UNIQUE (scope_id, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_traces_actor ON traces(scope_id, actor_id);

-- ---------------------------------------------------------------- 编码与解释
CREATE TABLE IF NOT EXISTS encodings (
    encoding_id      TEXT PRIMARY KEY,
    trace_id         TEXT NOT NULL REFERENCES traces(trace_id),
    encoder_version  TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    generated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interpretations (
    interpretation_id TEXT PRIMARY KEY,
    encoding_id       TEXT NOT NULL REFERENCES encodings(encoding_id),
    trace_id          TEXT NOT NULL REFERENCES traces(trace_id),
    relation          TEXT NOT NULL,
    modality          TEXT NOT NULL,
    polarity          TEXT NOT NULL,
    slots_json        TEXT NOT NULL,
    semantic_op       TEXT,            -- 语义操作：incur/repay/waive/set_total/correct/retract/noop
                                       -- 注意与 events.operation 区分：后者是 append/update/retract，
                                       -- 描述"事件怎么进流"，这里描述"这句话是什么意思"。
                                       -- 可空：v0.5 之前只写 slots.intent，读时回落。
    signals_json      TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interp_trace ON interpretations(trace_id);

-- ---------------------------------------------------------------- 闸门判定（可审计）
CREATE TABLE IF NOT EXISTS gate_decisions (
    decision_id       TEXT PRIMARY KEY,
    interpretation_id TEXT NOT NULL REFERENCES interpretations(interpretation_id),
    spec_version      TEXT NOT NULL,
    outcome           TEXT NOT NULL,   -- commit / pending / record_only / reject
    reasons_json      TEXT NOT NULL,
    admission_mode    TEXT NOT NULL,   -- shadow / assisted / auto
    decided_at        TEXT NOT NULL
);

-- ---------------------------------------------------------------- 事件（唯一事实源）
CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    scope_id          TEXT NOT NULL REFERENCES scopes(scope_id),
    state_key         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    operation_group_id TEXT,
    operation         TEXT NOT NULL,   -- append / update / retract / conflict
    effect_kind       TEXT NOT NULL,   -- set / delta / invalidate
    payload_json      TEXT NOT NULL,
    gate_decision_id  TEXT REFERENCES gate_decisions(decision_id),
    actor_id          TEXT NOT NULL,
    effective_at      TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    sequence_no       INTEGER NOT NULL,
    spec_version      TEXT NOT NULL,
    target_ref        TEXT,
    superseded_by     TEXT,
    void              INTEGER NOT NULL DEFAULT 0,  -- 1 = 已被纠正/撤销，不参与求值
    UNIQUE (scope_id, idempotency_key),
    UNIQUE (state_key, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_events_stream ON events(state_key, sequence_no);
CREATE INDEX IF NOT EXISTS idx_events_effective ON events(state_key, effective_at, sequence_no);
CREATE INDEX IF NOT EXISTS idx_events_group ON events(operation_group_id);

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id   TEXT NOT NULL REFERENCES events(event_id),
    trace_id   TEXT NOT NULL REFERENCES traces(trace_id),
    span_start INTEGER,
    span_end   INTEGER,
    PRIMARY KEY (event_id, trace_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_trace ON event_evidence(trace_id);

-- ---------------------------------------------------------------- 状态投影
CREATE TABLE IF NOT EXISTS states (
    state_key      TEXT PRIMARY KEY,
    scope_id       TEXT NOT NULL REFERENCES scopes(scope_id),
    value_json     TEXT,
    status         TEXT NOT NULL,   -- known/unknown/disputed/stale/retracted
    valid_from     TEXT,
    valid_to       TEXT,
    last_known_json TEXT,
    conflict_reason TEXT,
    version        INTEGER NOT NULL DEFAULT 0,
    watermark      INTEGER NOT NULL DEFAULT 0,
    evaluated_as_of TEXT,
    spec_version   TEXT NOT NULL
);

-- ---------------------------------------------------------------- 依赖登记
CREATE TABLE IF NOT EXISTS state_deps (
    derived_key  TEXT NOT NULL,
    input_key    TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (derived_key, input_key, rule_version)
);

-- ---------------------------------------------------------------- 投影检查点（可选加速）
CREATE TABLE IF NOT EXISTS state_snapshots (
    state_key   TEXT NOT NULL,
    watermark   INTEGER NOT NULL,
    value_json  TEXT,
    status      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (state_key, watermark)
);

-- ---------------------------------------------------------------- 澄清队列（阶段 3）
CREATE TABLE IF NOT EXISTS clarifications (
    request_id          TEXT PRIMARY KEY,
    scope_id            TEXT NOT NULL REFERENCES scopes(scope_id),
    interpretation_ids_json TEXT NOT NULL,
    kind                TEXT NOT NULL,   -- missing_slot / ambiguous / unconfirmed / conflict
    detail_json         TEXT,
    status              TEXT NOT NULL,   -- open / answered / expired / dismissed
    created_at          TEXT NOT NULL,
    expires_at          TEXT,
    -- 这条问题**已经念给模型看过了**的时间。
    --
    -- 为什么必须有这一列：宿主形态下每轮都会重新拼一次材料，
    -- 没有这个标记，「还没确定的事」就会每轮重复出现——
    -- 用户被同一句话连问五遍，那不是澄清，是审问。
    --
    -- 语义是"已提过"，不是"已回答"。提过就不再主动提第二次，
    -- 用户自己说清楚了（或超时）才算结束。
    asked_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_clarif_scope ON clarifications(scope_id, status);

-- ---------------------------------------------------------------- 只记录未提交（阶段 3 补）
-- XM-10：record_only 的内容必须有独立落点，否则"记住他说过"只做到一半。
--         status=open 的内容可在后续被 promote 成事件，这条路径现在预留、未实现提升逻辑。
CREATE TABLE IF NOT EXISTS observations (
    observation_id    TEXT PRIMARY KEY,
    scope_id          TEXT NOT NULL REFERENCES scopes(scope_id),
    trace_id          TEXT NOT NULL REFERENCES traces(trace_id),
    interpretation_id TEXT NOT NULL,
    relation          TEXT NOT NULL,
    modality          TEXT NOT NULL,
    polarity          TEXT NOT NULL,
    slots_json        TEXT NOT NULL,
    excerpt           TEXT NOT NULL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open',   -- open / promoted / archived
    promoted_event_id TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_scope ON observations(scope_id, relation, status);
CREATE INDEX IF NOT EXISTS idx_obs_trace ON observations(trace_id);

-- ---------------------------------------------------------------- 全文检索（阶段 4）
-- 独立 FTS 表而非外部内容表：traces 的主键是 TEXT，rowid 对齐不可靠。
-- trigram 分词对中文有效（3 字符滑窗），但要求查询 >= 3 字符，短查询在代码里降级为 LIKE。
CREATE VIRTUAL TABLE IF NOT EXISTS traces_fts USING fts5(
    trace_id UNINDEXED,
    content,
    tokenize = 'trigram'
);

-- ---------------------------------------------------------------- 生命周期（阶段 5）
-- 5.9：生命周期元信息与不可变痕迹分离。痕迹不改，权重和层级可改。
CREATE TABLE IF NOT EXISTS lifecycle (
    ref_type      TEXT NOT NULL,          -- trace
    ref_id        TEXT NOT NULL,
    scope_id      TEXT NOT NULL,
    base          REAL NOT NULL DEFAULT 1.0,
    use_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at  TEXT,
    anchor        INTEGER NOT NULL DEFAULT 0,
    level         TEXT NOT NULL DEFAULT 'hot',   -- hot / cold / archived
    created_at    TEXT NOT NULL,
    PRIMARY KEY (ref_type, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_scope ON lifecycle(scope_id, level, anchor);

-- ---------------------------------------------------------------- 权重事件
-- v0.40.0：「权重曲线」要能画出"这条记忆的权重是怎么变的"，
-- 光有 lifecycle 表的 use_count 是不够的——那是一个**计数**，
-- 不是**历史**。计数回答不了"三个月前它有多重要"。
--
-- 为什么是事件表而不是加一列「priority_history」：
-- 本项目的铁律是「事件是唯一事实源，投影可以重算」。
-- priority 是派生值（base × 时间衰减 × 使用增益），
-- 把它存成第二份就等于有两个真相源（CONTRIBUTING 红线四）。
--
-- 所以这里只记**事实**：这条记忆在某个时刻被用了 / 被搜到了 / 被设成锚点了。
-- 曲线由这些事件重放算出来——和 events → states 是同一种关系。
--
-- 为什么不用 JSON 列：重放要按时间排序、要做区间聚合，
-- 独立成表能走索引；塞进 lifecycle 的一列就得整行反序列化。
CREATE TABLE IF NOT EXISTS weight_events (
    event_id     TEXT PRIMARY KEY,
    ref_type     TEXT NOT NULL,
    ref_id       TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- use（真被用）/ search（只被搜到）/ anchor（设锚）/ unanchor（取消锚）
    occurred_at  TEXT NOT NULL,
    detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_wgev_ref ON weight_events(ref_type, ref_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_wgev_scope ON weight_events(scope_id, occurred_at);

-- 摘要产物。合并生成新行，旧行标 merged，不覆盖（12.2）。
CREATE TABLE IF NOT EXISTS summaries (
    summary_id       TEXT PRIMARY KEY,
    scope_id         TEXT NOT NULL,
    input_refs_json  TEXT NOT NULL,
    text             TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    coverage_json    TEXT,
    omissions_json   TEXT,
    validation_status TEXT NOT NULL DEFAULT 'unchecked',  -- unchecked / ok / failed
    lifecycle_state  TEXT NOT NULL DEFAULT 'active',      -- active / merged / archived
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_scope ON summaries(scope_id, lifecycle_state);

-- ---------------------------------------------------------------- 承诺（投影）
-- 「下个月我还你两袋」不改变余额，但它留下了一个**可以被追讨的对象**。
--
-- 这张表是投影，不是事实源。事实源仍然是 events：
-- 承诺是 effect_kind='none' 的事件，兑现是一条 delta<0 的事件。
-- 所以这张表可以随时用 `Engine.rebuild_commitments()` 从事件流重建，
-- 重建结果与增量维护**逐字相同**（有测试守着）。
--
-- 为什么不做成"事件表里的一列"：承诺有生命周期
-- （open → kept / broken / cancelled），而事件是不可变的。
-- 把可变状态放进不可变表，就再也分不清"记录"和"现状"了。
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id  TEXT PRIMARY KEY,
    scope_id       TEXT NOT NULL REFERENCES scopes(scope_id),
    state_key      TEXT NOT NULL,
    event_id       TEXT NOT NULL REFERENCES events(event_id),
    actor_id       TEXT NOT NULL,
    quantity       REAL NOT NULL,
    remaining      REAL NOT NULL,
    unit           TEXT,                -- 展示用（「2 碗」）；不参与计算
    due_hint       TEXT,
    due_at         TEXT,
    status         TEXT NOT NULL,       -- open / kept / partial / cancelled
    settled_by     TEXT,                -- 结清它的那条事件
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commitments_open
    ON commitments(scope_id, state_key, status, created_at);


-- ---------------------------------------------------------------- 准入与熔断
-- 每个 `(关系, 句式)` 一条。**它是唯一会"自己变"的配置**——
-- 因为准入等级本来就该随证据动：稳的格子放开，被纠正的格子收回。
--
-- 句式（pattern）不是自然语言模板，是**风险分类**：
--     incur:plain        「我欠你三袋」——有数量、无歧义、无第三方
--     incur:third_party  「替老张赊的」——施事不明，永远 assisted
--     correct:revision   「不对，是五十」——改历史，永远 assisted
--
-- 为什么不存"每句话的指纹"：那会过拟合到语料。分类要粗到能解释、
-- 细到能区分风险。
CREATE TABLE IF NOT EXISTS admission_state (
    relation        TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    level           TEXT NOT NULL,       -- auto / assisted / shadow
    failures        INTEGER NOT NULL DEFAULT 0,   -- 连续被纠正次数
    successes       INTEGER NOT NULL DEFAULT 0,   -- 熔断后连续确认次数
    opened_at       TEXT,                -- 熔断时刻（NULL = 未熔断）
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (relation, pattern)
);

-- 熔断流水，供审计：谁在什么时候把这个格子关掉了
CREATE TABLE IF NOT EXISTS breaker_log (
    log_id      TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    relation    TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    action      TEXT NOT NULL,           -- open / close / failure
    reason      TEXT NOT NULL,
    at          TEXT NOT NULL
);

-- ---------------------------------------------------------------- 人物画像
-- 画像回答的是「这个人是谁」，不是「账上还有多少」。
--
-- 三条设计约束（对应方案第四章）：
--   1. 标签是**印象**，不是事实。事实层是 events，这里只放"我对他的看法"。
--   2. 来源必须分开记：confirmed（用户明说）/ observed（系统看到）/ inferred（模型猜）。
--      推断**永不自动升级为事实**——它可以影响语气，不能授权。
--   3. 账号不等于人物。可疑期间新标签先暂存（staged），不污染主画像。
CREATE TABLE IF NOT EXISTS person_profiles (
    profile_id   TEXT PRIMARY KEY,
    scope_id     TEXT NOT NULL REFERENCES scopes(scope_id),
    account_id   TEXT NOT NULL,
    display_name TEXT,
    -- active 正常 / suspect 有换人线索（新标签进暂存区）
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (scope_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_profiles_scope
    ON person_profiles(scope_id, status);

CREATE TABLE IF NOT EXISTS person_tags (
    tag_id     TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES person_profiles(profile_id),
    scope_id   TEXT NOT NULL,
    -- 标签类别：address（怎么称呼）/ preference（喜好）/ habit（习惯）
    --          style（表达方式）/ topic（常聊的话题）/ relation（关系）
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    -- 来源三分，决定这条标签能不能当事实用
    source     TEXT NOT NULL,     -- confirmed / observed / inferred
    -- 可信度与"多常用"是两件事（3.3）：反复提起的谎话很好想起，但仍然不可信。
    confidence REAL NOT NULL DEFAULT 0.5,
    seen_count INTEGER NOT NULL DEFAULT 1,
    -- active 生效 / staged 暂存（画像可疑时）/ corrected 被纠正 / expired 失效
    status     TEXT NOT NULL DEFAULT 'active',
    -- 出处。标签必须能追到一句原话，否则它只是模型的想象。
    evidence_trace_id TEXT,
    -- 被这条标签取代的上一条（纠正时留痕，不覆盖）
    supersedes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_profile
    ON person_tags(profile_id, status, kind);
CREATE INDEX IF NOT EXISTS idx_tags_evidence
    ON person_tags(evidence_trace_id);

-- 认知异常：轻量模型只出**线索**，不下身份结论（4.5）。
CREATE TABLE IF NOT EXISTS profile_alerts (
    alert_id    TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    profile_id  TEXT NOT NULL REFERENCES person_profiles(profile_id),
    kind        TEXT NOT NULL,       -- style_shift / address_shift / explicit_claim
    signals_json TEXT NOT NULL,      -- 命中的具体线索，给人看
    status      TEXT NOT NULL DEFAULT 'open',   -- open / dismissed / escalated
    evidence_trace_id TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_open
    ON profile_alerts(scope_id, status);

-- 判断记录：主 AI 对异常给出过什么结论（保留历史，便于事后复盘）。
CREATE TABLE IF NOT EXISTS profile_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    alert_id      TEXT NOT NULL REFERENCES profile_alerts(alert_id),
    verdict       TEXT NOT NULL,     -- same_person / different_person / unknown
    reasons_json  TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------- 情绪与关系
-- 方案第六章。两条线必须分开：
--
--   短期情绪  anger / joy / sadness / anxiety / grievance
--             会随时间自然消退。**这是"消气"。**
--   长期关系  trust / closeness / respect
--             不会因为时间过去自己变好。**这是"原谅"。**
--
-- 6.2 那句话是这一层的全部难点：
--     消气不等于原谅，原谅不等于遗忘。
-- 所以两张表、两套规则：情绪有衰减，关系没有。
CREATE TABLE IF NOT EXISTS emotion_events (
    event_id    TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    profile_id  TEXT NOT NULL REFERENCES person_profiles(profile_id),
    emotion     TEXT NOT NULL,       -- anger / joy / sadness / anxiety / grievance
    -- 这次事件把情绪推了多少。正数累积，负数安抚。
    delta       REAL NOT NULL,
    -- 触发原因。不写原因的情绪，事后无法解释"你当时为什么生气"。
    reason      TEXT NOT NULL,
    -- 指向具体事件或痕迹，能追到是哪句话惹的
    evidence_trace_id TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_emotion_events
    ON emotion_events(profile_id, emotion, occurred_at);

-- 关系事件。**没有衰减**——信任掉下去不会自己长回来。
CREATE TABLE IF NOT EXISTS relationship_events (
    event_id    TEXT PRIMARY KEY,
    scope_id    TEXT NOT NULL,
    profile_id  TEXT NOT NULL REFERENCES person_profiles(profile_id),
    dimension   TEXT NOT NULL,       -- trust / closeness / respect
    delta       REAL NOT NULL,
    reason      TEXT NOT NULL,
    evidence_trace_id TEXT,
    -- 这次事件是不是"修复"。修复与普通正向事件分开记，
    -- 因为"关系回暖"和"把话说开了"是两件事：
    -- 前者可以靠时间，后者必须有一句话。
    is_repair   INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationship_events
    ON relationship_events(profile_id, dimension, occurred_at);

-- 未了结的事。**这张表是"原谅不等于遗忘"的落地。**
--
-- 一条 grievance 开出来之后，只有 `repair()` 能关掉它。
-- 情绪可以自己消退到 0，但这条记录一直在——
-- 那就是「不生气了，但还等着一个解释」。
CREATE TABLE IF NOT EXISTS grievances (
    grievance_id TEXT PRIMARY KEY,
    scope_id     TEXT NOT NULL,
    profile_id   TEXT NOT NULL REFERENCES person_profiles(profile_id),
    -- 什么事。一句话，给人看的。
    summary      TEXT NOT NULL,
    severity     REAL NOT NULL DEFAULT 1.0,   -- 1 小事 / 3 明显冲突 / 5 严重
    status       TEXT NOT NULL DEFAULT 'open',  -- open / repaired / forgiven
    evidence_trace_id TEXT,
    opened_at    TEXT NOT NULL,
    closed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_grievances_open
    ON grievances(profile_id, status, opened_at);

-- v0.52 性能索引：会话变长后这两条查询是每轮必走的。
CREATE INDEX IF NOT EXISTS idx_traces_scope_time ON traces(scope_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_tags_lookup ON person_tags(profile_id, kind, value, status);
CREATE INDEX IF NOT EXISTS idx_states_scope ON states(scope_id, status);
-- 0.7 edits replace immutable traces. No plaintext copy of old content here.
CREATE TABLE IF NOT EXISTS memory_edits (
    edit_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
    old_trace_id TEXT NOT NULL REFERENCES traces(trace_id),
    new_trace_id TEXT NOT NULL REFERENCES traces(trace_id),
    edited_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_maintenance (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_anchor ON lifecycle(scope_id, ref_type, anchor);
