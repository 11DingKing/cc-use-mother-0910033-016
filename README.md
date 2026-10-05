# 月度活动统计对账

本项目维护月度活动统计对账的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖活动统筹员、讲解员、学校联系人、场馆管理员，并明确统计口径版本、来源差异对账、月度封账更正、指标下钻复算等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/reconciliation/`：对账后端（引擎、持久化、服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo_flow.py`：端到端对账流程演示。
- `tests/`：契约完整性回归测试与后端测试。

## 后端服务

纯标准库实现（Python ≥ 3.11，无第三方依赖），模块划分：

- `models.py`：指标、调整事件类型、活动状态与领域异常。
- `caliber.py`：统计口径版本规则（去重窗口、迟到宽限、跨月分摊、人数与时长口径）。
- `engine.py`：纯函数统计引擎，输出指标值与逐记录包含/排除血缘。
- `store.py`：SQLite 持久化；月报只增不改，旧修订版置为 superseded。
- `service.py`：应用服务，串联快照、对账、封账、调整事件与复算。
- `api.py`：标准库 HTTP API。

启动服务：`PYTHONPATH=src python3 -m reconciliation.api --db reconciliation.db --port 8080`

### 对账流程

1. **录入来源数据**：`POST /venues`、`/activities`、`/check-ins`、`/venue-reports`（场馆自报数）。
2. **来源快照**：`POST /snapshots` 冻结活动、签到与自报数据，生成 SHA-256 校验和；之后所有计算只读快照。
3. **统计口径版本**：`POST /calibers` 注册口径（内置默认口径 `v1`），`GET /calibers` 查询。
4. **差异清单**：`POST /runs` 按口径计算县级数并与场馆自报数比对，逐场馆逐指标生成差异。
5. **场馆确认**：`POST /differences/{id}/confirm`；全部确认后才能封账。
6. **封账月报**：`POST /runs/{id}/close` 输出稳定月报；`GET /reports?venue_id=&month=` 查看生效版与全部修订历史。
7. **调整事件**：`POST /adjustments` 统一处理迟到签到（`late_checkin`）、身份合并（`identity_merge`）、跨月分摊（`cross_month_override`）、已签报告更正（`report_correction`）。封账后登记调整事件会自动复算并生成新月报修订版，旧版保留。
8. **指标下钻**：`GET /reports/{id}/drilldown?metric=sessions|service_persons|lecture_minutes` 返回包含与排除记录及原因（重复签到、迟到超出宽限、活动已取消、跨月分摊至其他月份等）。
9. **历史口径复算**：`POST /recalculations` 用任一口径版本重算历史月份并与当前生效月报对比，不改写已封账数据。

### 口径规则键

| 键 | 取值 | 含义 |
| --- | --- | --- |
| `dedupe_window_minutes` | 非负数值 | 同一身份在同一活动内的签到去重窗口 |
| `late_grace_minutes` | 非负数值 | 活动结束后仍计入的迟到宽限 |
| `cross_month_rule` | `by_start` / `by_day_prorate` | 跨月活动计入开始月或按天分摊 |
| `service_person_mode` | `unique_person` / `checkin_count` | 服务人数按去重人数或签到人次 |
| `duration_mode` | `actual` / `planned` | 讲解时长取实际（缺省回退计划）或计划 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

流程演示：`python3 tools/demo_flow.py`
