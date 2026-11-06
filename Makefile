.PHONY: test test-v smoke e2e live probe eval-m6 eval-m7 eval-m8 eval-contra eval-time probe-contra eval-entity test-live verify serve journal bench-ps fmt clean

PY ?= python3

test:            ## 全部 L1 单测（不联网、不花钱）
	$(PY) -m pytest tests/ -q

test-v:          ## 详细输出 + 慢用例耗时
	$(PY) -m pytest tests/ -v --durations=5

smoke:           ## 端到端：起桩上游 + 真代理 + 真 TCP（零成本）
	$(PY) -m tools.smoke

e2e:             ## 抽取端到端演练（真 HTTP + 真库，模型是桩，零成本）
	$(PY) -m tools.e2e_extract

probe:           ## 上游能力探针（思考开关/json/缓存，约 1 分钱）
	$(PY) -m tools.probe_mimo

eval-m6:         ## M6 三级表示评测（约 3 分钱）：关键词 + 摘要
	$(PY) -m tools.eval_m6

eval-m7:         ## M7 实体与角色卡评测（约 3 分钱）：类型 / 槽位 / 卡规模
	$(PY) -m tools.eval_m7

eval-m8:         ## M8 真机评测（约 3 分钱）：key 词表合规 / 值是否是句子
	$(PY) -m tools.eval_m8

eval-contra:     ## M8.2 矛盾检测 35 用例（零成本，纯规则）
	$(PY) -m tools.eval_contradiction

eval-time:       ## M8.3 时间解析 44 用例（零成本）
	$(PY) -m tools.eval_timeparse

probe-contra:    ## 真机：用户改口，旧值会不会被关掉（约 4 分钱）
	$(PY) -m tools.probe_contradiction

eval-entity:     ## 实体归一 24 用例（零成本，纯规则）
	$(PY) -m tools.eval_entity

live:            ## 真机抽取评测 14 窗口（约 1.5 分钱）+ 全链路演练
	$(PY) -m tools.live_extract
	$(PY) -m tools.live_proxy --rounds 6

test-live:       ## 真连上游一次（花钱！需在 ~/.hippo/config.yaml 配 key）
	@test -n "$$DEEPSEEK_API_KEY" || (echo "需要 DEEPSEEK_API_KEY" && exit 1)
	$(PY) -m tools.live_check

serve:           ## 起代理，默认 127.0.0.1:8787
	$(PY) -m hippo.cli serve

bench-ps:        ## 预筛基准：make bench-ps S=dev（S 可 dev/holdout/test/blind）
	$(PY) -m tools.bench_prescreen --split $(or $(S),dev) --window 5

verify:          ## 生成某子任务的验收报告骨架：make verify M=1.1
	@test -n "$(M)" || (echo "用法: make verify M=1.1" && exit 1)
	$(PY) -m tools.verify --milestone $(M)

journal:         ## 生成施工记录骨架：make journal M=3.1
	@test -n "$(M)" || (echo "用法: make journal M=3.1" && exit 1)
	$(PY) -m tools.journal $(M) $(if $(T),--title "$(T)")

fmt:
	@command -v ruff >/dev/null 2>&1 && ruff format hippo tests || echo "ruff 未安装，跳过"

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info
