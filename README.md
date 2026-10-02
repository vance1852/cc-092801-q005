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

三条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析及交易风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

### 跨中心验证能力预检

研发组合团队在跨中心验证排期前，可一次性判断目标中心在期望时间窗口内是否具备
所需样本类型产能、设备能力、可用排期窗口和资源余量。预检只读，不扣减库存、不
占用资源；中心、样本能力、设备或排期数据变化后再次调用即返回基于最新已提交快照
的结果（结果中的 `data_revision` 为输入快照摘要，便于离线比对）。

结论取值：

- `executable`：目标中心逐项需求全部满足；
- `has_gaps`：存在缺口，但同区域/其他区域有可完整执行的替代中心，或目标中心在
  可等待的资源释放（占用结束、维护恢复、后续开放窗口）后可执行；
- `not_executable`：存在无法恢复的硬缺口（不支持样本类型/设备、中心停用），且
  没有任何可完整执行的替代中心。

预检相关接口（均为 `POST`，需要 `X-Actor-Id` 头）：

- `/validation_schemes`：登记候选项目的验证方案（样本类型需求量、所需设备类型与
  数量、方案时长）；
- `/centers/sample_capabilities`：登记/更新中心对样本类型的日产能；
- `/centers/equipment`：登记设备及其可用、维护、退役状态；
- `/centers/calendar_windows`：登记中心可承接验证的开放时间窗口；
- `/centers/bookings`：登记既有的样本/设备排期占用；
- `/validation_prechecks`：执行预检。

预检请求示例：

~~~json
{
  "candidate_project_id": "ct-77",
  "scheme_id": "scheme-ct-1",
  "target_center_id": "validation-east",
  "window_starts_at": "2026-09-26T02:00:00Z",
  "window_ends_at": "2026-09-26T06:00:00Z",
  "search_region": "east"
}
~~~

响应包含目标中心逐项需求判定（`target.items`）、缺口逐项原因与差额（`gaps`）、
按同区域优先、满足度降序、最早可用时间升序排列的替代中心（`alternatives`），
以及每项资源的 `earliest_available_at`。`search_region` 可选；缺省时在同区域与
其他区域全部中心中检索。

中心登记时可通过 `region` 字段声明所属区域（默认 `default`）。

