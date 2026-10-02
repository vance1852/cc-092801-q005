# 修复尽调样本与研究资源错配基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.acceptance --workspace .
PYTHONPATH=src python3 -m discovery_lab.acceptance --workspace .
PYTHONPATH=src python3 -m licensing_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析及交易风险处置，不访问外部网络。`portfolio_ops` 验收还包含一次跨中心验证能力预检，输出 `precheck.conclusion`（feasible / gap / infeasible）。

## 跨中心验证能力预检

在启动跨中心验证之前，可以通过预检一次性判断目标中心是否同时具备所需样本类型能力、设备能力、可用连续排产时段和足够研究资源余量：

~~~bash
curl -s -X POST http://127.0.0.1:8080/validation_prechecks \
  -H 'Content-Type: application/json' -H 'X-Actor-Id: plan' -d '{
    "precheck_id": "precheck-001",
    "project_id": "candidate-onco-1",
    "protocol_id": "protocol-pk-001",
    "center_id": "collection-east",
    "window_start": "2026-09-25T00:00:00Z",
    "window_end": "2026-09-25T14:00:00Z",
    "required_minutes": 240,
    "sample_requirements": [{"sample_type": "PLASMA", "quantity_units": "200"}],
    "equipment_requirements": [{"equipment_kind": "SEQUENCER", "quantity_units": "2"}],
    "resource_requirements": [{"preservation_resource_kind": "preservation-box", "quantity_units": "100"}]
  }'
~~~

预检结论为 `feasible`（可执行）、`gap`（存在可恢复缺口）或 `infeasible`（不可执行）。结论为缺口时，`gaps` 逐项给出维度（样本 / 设备 / 排期 / 研究资源）、需要与可用数量、缺口数量及原因；`alternatives` 按满足度和最早可用时间排好序，分别给出同区域与其他区域替代中心。预检是只读快照操作：不扣减库存、不占用排期、不写审计事件，每次调用读取已提交的最新中心、样本、设备、库存与排期数据。

预检目录通过以下接口维护（均需要 planner 角色并写入审计链）：

- `POST /response_centers/{center_id}/region`：登记中心所属区域；
- `POST /response_centers/sample_capabilities`：登记/更新样本类型处理能力；
- `POST /response_centers/equipment_capabilities`：登记/更新设备验证能力；
- `POST /response_centers/schedule_windows`：登记每周重复的本地时间排产窗口（按中心时区换算为 UTC）。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
