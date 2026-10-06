# 月度活动统计对账

本项目维护月度活动统计对账的领域约定、角色边界与样例数据，并提供完整的 Python 后端：县级主管部门月底汇总场次、服务人数和讲解时长时，先保存各场馆来源快照，按统计口径版本重算并生成差异清单，由场馆确认后封账输出稳定月报；迟到签到、身份合并、跨月分摊和已签报告更正均通过调整事件处理，任意指标可下钻到包含与排除记录，并支持历史口径复算。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/monthly_recon/`：对账后端（模型、口径、引擎、服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tools/demo.py`：端到端演示完整对账流程。
- `tests/`：契约、引擎、流程与 API 回归测试。

## 后端架构

- `monthly_recon.models`：来源快照、差异清单、确认、调整事件、月报等数据模型。
- `monthly_recon.caliber`：统计口径版本（去重窗口、迟到宽限、跨月分摊规则、人数去重范围、过短场次下限），内置 `v0.9` / `v1.0` 两个版本。
- `monthly_recon.engine`：按口径计算月度指标，产出包含/排除台账（下钻数据源）。
- `monthly_recon.service`：报送快照 → 执行对账（差异清单）→ 场馆确认 → 封账（稳定月报）→ 调整事件 → 复算/下钻。
- `monthly_recon.api`：基于标准库 `http.server` 的 JSON API，无第三方依赖。

关键规则：

- 快照不可变，重复报送生成新版本；对账后快照变更会使确认失效，需重新对账。
- 封账月报为稳定版本，之后的调整事件只影响复算产生的新修订版本（`sealed=false`）。
- 已签报告更正（`REPORT_CORRECTION`）仅允许在封账后登记，并只体现在复算版本中。
- 跨月场次默认按分钟分摊，可由 `CROSS_MONTH_ALLOCATION` 事件人工指定分摊比例（覆盖口径默认值）。

## HTTP API

启动：`python3 tools/run_server.py --port 8000`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/venues` | 登记场馆 |
| POST | `/calibers` | 注册统计口径版本 |
| POST | `/snapshots` | 报送来源快照（含自报数） |
| POST | `/reconciliations/run` | 执行对账，生成差异清单 |
| GET | `/reconciliations/{month}` | 查看对账周期状态与差异 |
| POST | `/reconciliations/{month}/confirm` | 场馆确认差异清单 |
| POST | `/reconciliations/{month}/close` | 封账，产出稳定月报 |
| POST | `/adjustments` | 登记调整事件（迟到签到/身份合并/跨月分摊/已签更正） |
| GET | `/adjustments?month=` | 查询调整事件 |
| GET | `/reports/{month}?revision=` | 查询月报（默认封账版） |
| POST | `/reports/{month}/recalculate` | 复算（可指定历史口径与截止时间） |
| GET | `/reports/{month}/drilldown?metric=&venue_id=` | 指标下钻到包含/排除记录 |

指标取值：`sessions`（场次）、`headcount`（服务人数）、`explanation_minutes`（讲解时长）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

端到端演示：`python3 tools/demo.py`
