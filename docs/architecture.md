# 扁鹊架构

Bianque is an alert-driven root cause analysis agent for Kubernetes. An Alertmanager webhook (or a manual trigger) starts an investigation: a LangGraph graph plans the work, dispatches one evidence sub-agent per evidence domain (`kubernetes`, `logs`, `change`) that collects read-only evidence through a single tool registry and executor, judges whether the evidence suffices, and writes a structured report in which every claim cites evidence IDs. Every model output passes a deterministic evidence gate before it is stored or shown; a report that keeps failing the gate becomes a degraded report built without a model. All model traffic leaves through one gateway that redacts first. Remediation suggestions are confirmed or rejected by a person and only recorded. The same registry is served to external clients over MCP. Storage is PostgreSQL only. Evaluation in CI replays recorded responses of a real model against recorded evidence packs, with no network access.

本文描述扁鹊的模块划分、数据流与关键机制，路径都相对仓库根目录。架构决策的理由与取舍见 [`docs/adr/`](./adr/)，功能与代码位置的对照见 [`capabilities.md`](capabilities.md)，安装与运行见 [`README.md`](../README.md)。

## 1. 概览

扁鹊的后端是一个 Python 进程（`backend/bianque/`），HTTP 接口、调查运行、追问、MCP 服务与评测命令共用同一组模块；前端是一个 React 单页应用（`frontend/`），由 nginx 提供静态文件并反向代理 `/api` 与 `/mcp`；存储是一个 PostgreSQL 16 实例（带 pgvector 扩展）。

```text
 Alertmanager webhook ──▶ POST /api/alerts/alertmanager ─┐
 手动触发 ──────────────▶ POST /api/investigations ───────┤
                                                          ▼
                                  InvestigationService（进程内后台任务）
                                                          │
                                       LangGraph 调查图（第 2 节）
                     ┌────────────────────┼─────────────────────┐
                     ▼                    ▼                     ▼
              模型网关 bianque.llm   工具执行器 bianque.tools   证据门 bianque.gate
              （先脱敏，唯一出口）    （注册表、真实/回放后端）   （确定性校验）
                     │                    │
                     ▼                    ▼
           OpenAI 兼容模型服务     Kubernetes API（只读）、清单 Git 仓（只读）
           或录制响应、scripted    或证据包

 浏览器 ──▶ nginx ──▶ /api（REST、SSE）      MCP 客户端 ──▶ /mcp（同一注册表与执行器）
                          │
                          ▼
                   PostgreSQL（业务表、LangGraph 检查点、知识向量与全文索引）
```

一次调查的数据流：

1. 告警经 webhook 进入，或由登录用户手动触发（重查一条已收到的告警，或在回放形态下回放一个评测用例）。后端建一行 `investigations`，在同一进程的后台任务里执行调查图（`backend/bianque/agents/service.py`）。
2. 调查图把告警原文存为第一条证据，检索运维手册，生成计划，把计划步骤按证据域分派给取证子 Agent 并行取证，判断证据是否充分，不充分时补充计划，最后生成报告。
3. 每个模型输出先过证据门；通过的报告发布为正式报告并生成修复建议，重试用尽时发布降级报告。
4. 过程中的每一步写进度事件，页面经 SSE 实时显示；每次模型调用、工具调用与知识检索写一条调用记录。
5. 报告发布后可以开追问会话；修复建议由人确认或驳回，只记录。

运行方式由三个配置决定，业务代码不判断当前处于哪种形态：

| 配置 | 取值 | 作用 |
| --- | --- | --- |
| `BIANQUE_TOOL_BACKEND` | `live`、`pack` | 工具读真实集群与清单仓，或读证据包 |
| `BIANQUE_LLM_MODE` | `live`、`recorded`、`scripted` | 模型模式（第 13 节） |
| `BIANQUE_EMBEDDING_MODE` | `local`、`fixture` | 本地嵌入模型，或向量夹具（第 7 节） |

| 运行方式 | 工具后端 | 模型模式 | 嵌入 | 入口 |
| --- | --- | --- | --- | --- |
| 演示形态 | `live` | `live` | `local` | `make demo` |
| 回放形态 | `pack` | `recorded`（可选 `live`、`scripted`） | `fixture` | `make replay` |
| 评测 | `pack` | `recorded` | `fixture` | `make eval` |
| 测试与端到端测试 | `pack` 或测试替身 | `scripted` | `fixture` | `make test`、`make e2e` |

模块之间的依赖边界由 import-linter 契约保证（`backend/pyproject.toml`），例如：只有提供方层导入模型 SDK，只有真实 Kubernetes 后端导入 `kubernetes_asyncio`，只有嵌入模块导入 fastembed，`bianque.gate` 不导入 `bianque.llm`，只有 `bianque.mcp` 导入 MCP SDK，只有 `bianque.api` 导入 FastAPI。

## 2. 调查图

调查图定义在 `backend/bianque/agents/graph.py`，节点实现在 `backend/bianque/agents/nodes.py`（取证子 Agent 在 `backend/bianque/agents/subagent.py`）。它是一个 LangGraph `StateGraph`，以调查编号为线程编号写检查点。

```text
intake → retrieve_knowledge → plan → dispatch ──Send──▶ evidence_agent（每个证据域一个任务）┐
                                       ▲                                                   │
                                       └── replan ◀── 不充分且还有轮次 ── assess ◀──────────┘
                                                                         │ 充分、预算用尽或轮次用尽
                                                                         ▼
                        degrade ◀── 重试用尽或模型不可用 ── gate ◀── report ◀── 被拒且可重试
                           │                                  │ 通过
                           ▼                                  ▼
                        publish ◀─────────────────────────────┘ → END
```

### 2.1 节点

| 节点 | 职责 | 模型调用 |
| --- | --- | --- |
| `intake` | 调查状态改为 `running`；告警原文存为证据（来源 `alertmanager`），告警标签里的对象名作为保留标识符；写 `investigation_started` 与第一条 `evidence_added` | 无 |
| `retrieve_knowledge` | 用告警名与注解组成查询检索运维手册，命中的知识编号与标题放进状态，写一条 `kind=knowledge` 的调用记录；检索出错时知识为空，调查照常进行 | 无 |
| `plan` | 生成计划第 1 版（函数 `submit_plan`），经证据门校验；第一轮只保留 `first_round` 为真的证据域的步骤（`check_range`），越界步骤丢弃并记进调用记录的证据门结果；全部越界时退回默认计划 | 1 次结构化输出，被拒重试 1 次 |
| `dispatch` | 标记一轮开始；路由函数 `route_steps` 把本轮步骤按证据域分组，每组一个 LangGraph `Send` | 无 |
| `evidence_agent` | 取证子 Agent，见第 3 节 | 每步若干次 |
| `assess` | 模型判断证据是否充分（函数 `submit_assessment`），再跑确定性充分性规则，最后检查 token 预算 | 1 次结构化输出，被拒重试 1 次 |
| `replan` | 针对上一轮判断的缺失项生成补充计划（新版本，只含补充步骤），轮次加一 | 1 次结构化输出，被拒重试 1 次 |
| `report` | 用全部证据、发现、失败的调用、知识编号与标题生成报告（函数 `submit_report`）；上一次被拒时附上违规列表 | 1 次结构化输出 |
| `gate` | 按报告的校验清单校验；被拒的尝试写一行 `reports`（状态 `rejected`）并发 `report_rejected`，决定重试或降级 | 无 |
| `degrade` | 由证据与确定性规则生成降级报告（`backend/bianque/gate/degrade.py`），同样过报告的校验清单 | 无 |
| `publish` | 写最终报告（`published` 或 `degraded`），正式报告生成修复建议行，调查状态改为 `reported` 或 `degraded`，发 `report_published` 或 `report_degraded` 与 `investigation_finished` | 无 |

### 2.2 充分性判断

`assess` 由两部分组成，结果写一行 `assessments`：

- 模型判断：是否充分、理由、当前假设的根因类别（与报告的根因类别同一个枚举）、缺什么。
- 确定性规则（`InvestigationNodes._rule_missing`）：没有 Pod 状态证据（`POD_STATUS_TOOLS`，即 `k8s_get_pod_status`）时一律不充分；假设类别属于某个证据域的 `required_for_categories`（`change` 域声明了 `config_error`、`resource_limit`、`probe_misconfig`、`image_error`）而该域还没有证据时不充分。规则推翻模型的「充分」时，`rule_override` 记为真。模型输出两次都没通过证据门时，由规则单独决定。

### 2.3 轮次、步数与预算

| 上限 | 配置 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 补充计划轮数 | `BIANQUE_MAX_REPLAN_ROUNDS` | 2 | 第一轮之外最多再取证 2 轮 |
| 子 Agent 每步的模型轮数 | `BIANQUE_SUBAGENT_MAX_STEPS` | 6 | 到上限时强制提交发现 |
| 报告重试次数 | `BIANQUE_REPORT_MAX_RETRIES` | 2 | 被拒后最多再生成 2 次，之后降级 |
| 调查 token 预算 | `BIANQUE_INVESTIGATION_TOKEN_BUDGET` | 120000 | 超出后不再补充计划，直接生成报告 |
| 图的 superstep 上限 | `RECURSION_LIMIT`（`graph.py`） | 100 | 只防路由缺陷 |

token 预算只在 `assess` 里检查一次：每次模型调用的调用记录与该调用的 token 在同一个事务里累加到 `investigations.tokens_used`，`assess` 读取这一合计，超出预算就在判断理由后追加说明并转入 `report`。同一轮内并发的子 Agent 不读预算，所以是否收敛与它们完成的先后无关；预算不切分给各子 Agent，也不裁剪提示词内容。

### 2.4 持久化

图的状态只放编号与少量控制值（轮次、本轮步骤、失败列表、知识编号与标题、判断结果、报告尝试次数），业务内容都在数据库里，每个节点自己提交写入，调查进行中即可从接口读到。

| 表 | 内容 |
| --- | --- |
| `alerts` | 告警原文；同一 fingerprint 在调查未结束时再次送达不新建调查 |
| `investigations` | 编号（`inv_` 加 ULID）、触发方式（`alert`、`manual`、`replay`）、回放用例、状态（`pending`、`running`、`reported`、`degraded`、`interrupted`、`failed`）、工具后端、模型模式与模型名、token 预算与用量 |
| `plans`、`steps` | 计划的各个版本（来源 `model` 或 `default`）与每个步骤（证据域、目标、建议工具、期望证据、状态、发现） |
| `assessments` | 每轮的判断，含 `rule_override` |
| `evidence` | 证据原文与脱敏文本、保留标识符、来源工具、产生它的调用与步骤 |
| `reports` | 每次报告尝试，含被拒的尝试与证据门结果 |
| `suggestions`、`confirmations` | 修复建议与确认记录（第 9 节） |
| `call_records` | 调用记录（第 12 节） |
| `investigation_events` | 进度事件（第 10 节） |
| `followup_sessions`、`followup_messages` | 追问会话与问答（第 8 节） |
| `knowledge_documents`、`knowledge_chunks` | 运维手册与分块（第 7 节） |

表结构由 Alembic 迁移建立（`backend/migrations/versions/`）。LangGraph 检查点由 `langgraph-checkpoint-postgres` 自建表，放在迁移预先创建的 `langgraph` schema 里：检查点用独立的连接池，连接参数把 `search_path` 设为 `langgraph`（`backend/bianque/agents/runner.py`），Alembic 只管理默认 schema。

编号规则（`backend/bianque/storage/ids.py`）：

| 前缀 | 生成方式 | 原因 |
| --- | --- | --- |
| `inv_` | ULID | 按时间有序；不进提示词 |
| `ev_` 加 10 位十六进制 | 取证工具证据：SHA-256（工具名、身份参数、脱敏文本）；告警证据：SHA-256（`alertmanager`、fingerprint、脱敏文本） | 证据编号进提示词，必须由内容决定；同一内容在一次调查内只存一份 |
| `sg_` 加 10 位十六进制 | SHA-256（序号、动作、命令、风险等级、引用） | 追问提示词里有修复建议编号 |
| `kn_` 加 12 位十六进制 | SHA-256（文件路径、标题链、分块序号） | 重复导入得到同一编号 |

`ev_` 与 `sg_` 编号因此只在一次调查内唯一，接口都按调查读取。

### 2.5 中断与失败

后端启动时、开始接受请求之前，`interrupt_unfinished` 把状态为 `pending` 或 `running` 的调查改为 `interrupted`，未结束的步骤改为 `failed`，各写一条 `investigation_interrupted`。调查不从检查点续跑；要重查时由告警再次送达新建调查，或手动触发。

调查图抛出的异常（例如录制响应查不到）使调查结束为 `failed`，未结束的步骤记 `failed`，错误字段只存异常类名，详情只进日志。

## 3. 取证子 Agent 与证据域

### 3.1 证据域声明

证据域只在 `backend/bianque/agents/domains.py` 一处声明（`DOMAINS`）。计划的越界检查、`dispatch` 的分组、默认计划、充分性规则、证据门的置信度上限、子 Agent 的工具筛选、进度事件的取值校验都读这份声明。

| 证据域 | 第一轮允许 | 取证工具 | 充分性规则要求它的根因类别 | 调用失败时限制根因置信度 |
| --- | --- | --- | --- | --- |
| `kubernetes` | 是 | `k8s_list_pods`、`k8s_get_pod_status`、`k8s_list_events`、`k8s_get_deployment` | 无 | 否 |
| `logs` | 是 | `logs_get_container_logs` | 无 | 是 |
| `change` | 否 | `change_get_rollout_history`、`change_list_commits`、`change_show_commit` | `config_error`、`resource_limit`、`probe_misconfig`、`image_error` | 否 |

`change` 不在第一轮：变更归因要先知道怀疑哪个对象与字段。Deployment 的修订历史归在 `change` 域，它回答「何时改了什么」，与 Git 提交互相印证；`kubernetes` 域只读当前状态。每个证据域的 `default_steps` 同时是默认计划的步骤（`backend/bianque/agents/default_plan.py`）。

### 3.2 一个子 Agent 任务

`dispatch` 为本轮每个有步骤的证据域发一个 `Send("evidence_agent", AgentTask)`，三个证据域共用 `EvidenceAgent` 一份实现。同一个 superstep 里各证据域的任务并行执行，一轮的耗时取决于最慢的一路；同一证据域的多个步骤在该任务内依次执行。第一轮只有 `kubernetes` 与 `logs` 两路并行，`change` 从补充轮起参与。

每个步骤是一段独立的 ReAct 对话（`EvidenceAgent._react`）：

1. 提示词由 `prompts/evidence_agent.md`（填入证据域名）与三个数据段组成：告警证据、本步的目标与建议工具与期望证据、本证据域之前步骤的发现。
2. 模型只能看到本证据域的取证工具与 `submit_findings`，通过 function calling 请求工具。
3. 同一轮返回的多个工具调用并发交给执行器（`asyncio.gather`），结果按模型给出的顺序回填；成功的结果以 `Evidence ID: ev_…` 开头，后面是脱敏文本；失败的结果只有错误类别与对象。
4. 模型不再请求工具时调用 `submit_findings` 提交发现，每条发现必须引用证据编号；与其他工具调用同时出现的 `submit_findings` 不受理，要求模型读完结果后单独调用。到步数上限或模型既不调用工具也不提交时，强制调用 `submit_findings`。
5. 发现经证据门的发现校验清单校验，被拒重试 1 次，仍被拒则丢弃发现、保留证据。校验时可引用的证据只有告警与本证据域的证据：同时运行的其他子 Agent 取到的证据不影响本域发现是否通过。

子 Agent 只产出证据与发现，不产出结论；报告只由调查 Agent 生成，结论必须直接引用证据编号。

### 3.3 工具筛选

子 Agent 以 `CallerScope(agent=<域>, domain=<域>)` 调用执行器。模型请求了别的证据域的工具、辅助工具或不存在的工具时，执行器拒绝执行（状态 `rejected`），错误回填给模型，并照常写调用记录。

### 3.4 失败处理

| 情况 | 处理 |
| --- | --- |
| 工具超时、连接错误、HTTP 5xx、429 | 执行器重试（第 4 节）；仍失败时错误回填给模型，记入状态的 `failures`，该步骤若没有取到任何证据则记 `failed` |
| 子 Agent 内的其他异常（含模型服务重试后仍不可用） | `EvidenceAgent.run` 自己捕获，不向图抛出：LangGraph 里一个 `Send` 任务抛错会使整个 superstep 失败，其他子 Agent 的结果也会丢失。剩余步骤记 `failed`，失败写进 `failures` |
| 模型服务不可用之外的模型错误：录制响应查不到或格式错误、请求被拒、发送前复查发现脱敏残留 | 属于配置或回放缺陷，向上抛出，调查结束为 `failed`，避免一次坏掉的回放被当作缺失证据而照常结束 |

`failures` 是带合并函数的状态字段，各子 Agent 并行追加。它进入后续提示词的「失败的调用」一节，并作为证据门上下文的失败调用列表：报告必须在缺失证据一节列出它们，`logs` 域的失败还使根因置信度不能为 `high`（第 5 节）。

## 4. 工具注册表与执行器

### 4.1 注册表

工具注册表（`backend/bianque/tools/registry.py`）是扁鹊唯一的工具实现，取证子 Agent 与 MCP 服务读同一个注册表（[ADR 0003](./adr/0003-single-tool-registry-and-mcp.md)）。工具是用 `@tool(...)` 声明的异步函数，声明内容：名称、类别（`evidence` 或 `auxiliary`）、证据域与证据来源（取证工具）、`read_only`、参数模型（pydantic，同时生成 function calling 与 MCP 的输入 schema）、超时、身份参数。

注册在模块导入时发生，违反规则时抛出 `ToolRegistrationError`，进程无法启动：

- `read_only` 必须为 `True`；
- 名称匹配 `^[a-z][a-z0-9_]{0,63}$`，模型的 function calling 与 MCP 客户端都接受这种名称；
- 取证工具必须有证据域与来源，辅助工具不能有；
- 身份参数必须是参数模型的字段；超时必须为正数；名称不能重复。

身份参数决定「取回的是哪个对象」，证据编号只用工具名与身份参数，`tail_lines` 一类只影响截取范围的参数不进身份。

| 工具 | 类别 | 证据域 | 身份参数 | 读什么 |
| --- | --- | --- | --- | --- |
| `k8s_list_pods` | 取证 | `kubernetes` | `namespace`、`label_selector` | 命名空间内的 Pod、阶段与重启次数 |
| `k8s_get_pod_status` | 取证 | `kubernetes` | `namespace`、`pod` | 容器状态、上一次终止的原因与退出码 |
| `k8s_list_events` | 取证 | `kubernetes` | `namespace`、`involved_object` | 命名空间事件，可按涉及对象过滤 |
| `k8s_get_deployment` | 取证 | `kubernetes` | `namespace`、`name` | 镜像、环境变量、资源限制、探针 |
| `logs_get_container_logs` | 取证 | `logs` | `namespace`、`pod`、`container`、`previous` | 当前或上一个容器实例的日志 |
| `change_get_rollout_history` | 取证 | `change` | `namespace`、`deployment` | 修订历史与最近两个修订的 Pod 模板差异 |
| `change_list_commits` | 取证 | `change` | `path` | 清单仓的提交列表 |
| `change_show_commit` | 取证 | `change` | `commit` | 一个提交的差异 |
| `knowledge_search` | 辅助 | 无 | 无 | 运维手册检索 |
| `investigation_list` | 辅助 | 无 | 无 | 一个命名空间最近的调查 |
| `investigation_get_report` | 辅助 | 无 | 无 | 一次调查的正式报告或降级报告 |

取证工具在调查中调用时结果保存为证据；辅助工具只提供给 MCP 客户端，结果不是证据。日志工具在结果进入模型之前折叠连续重复行（保留重复次数）、保留末尾最多 200 行、截断超过 1000 字符的行（`backend/bianque/tools/logs.py`）。工具结果按固定字段顺序渲染为 JSON，时间一律是绝对的 UTC 时间戳（`backend/bianque/tools/output.py`）。

### 4.2 执行器

执行器（`ToolExecutor.call`，`backend/bianque/tools/executor.py`）是工具调用的唯一入口：

1. 检查工具是否存在、调用方是否有权调用（子 Agent 的证据域；MCP 客户端的工具集合）、参数是否合法（pydantic）、参数里的 `namespace` 是否在 MCP 客户端的范围内。权限检查先于参数检查，调用方从无权调用的工具得不到任何参数信息。
2. 在 `asyncio.timeout(timeout_s)` 下执行（现有工具都是 10 秒）。错误类别（`backend/bianque/tools/errors.py`）中 `timeout`、`connection`、`server_error`、`throttled` 可重试，按 0.5 秒、1 秒退避最多重试 2 次；`not_found`、`forbidden`、`invalid_arguments` 等直接失败。
3. 脱敏：工具从结构化结果里取出对象名、容器名、镜像、标签值与提交号作为保留标识符，与原文一起交给脱敏引擎（第 6 节）。
4. 调查中的取证工具调用成功时计算证据编号并写 `evidence`（同一调查内已存在则复用）。MCP 调用与辅助工具不写证据。
5. 无论成败写一条调用记录，含重试次数与耗时。PostgreSQL 的文本列存不了 NUL，工具名、参数、输出与错误文本里的 NUL 转义后再写，保证每次调用都能留下记录。
6. 返回 `ToolResult`：状态（`ok`、`error`、`rejected`）、证据编号、`model_view`（`RedactedText`，唯一可以进入模型上下文的文本）、错误、保留标识符。失败与拒绝都是返回值，不是异常。

返回给模型的错误文本只有「类别: 对象」，不含耗时与重试次数，同样的失败在每次运行里读起来相同（第 13 节）。错误详情只写进调用记录。

### 4.3 真实后端与回放后端

每个取证工具背后有两套实现同一接口的后端（`backend/bianque/tools/backends/`）。后端只取原始数据，过滤、排序、截断与渲染都在工具函数里，所以同样的查询在两套后端上产生同样的工具输出。

| 数据源 | 真实后端 | 回放后端 |
| --- | --- | --- |
| Kubernetes 对象与日志 | `kubernetes_live.py`：`kubernetes_asyncio`，集群内用 ServiceAccount，本机用 kubeconfig；只调用 `read_` 与 `list_` 开头的方法 | `pack.py`：从证据包的对象快照与日志文件回答同样的查询；证据包里没有的对象返回 `not_found` |
| 清单 Git 仓 | `git.py`：`git` 命令行，只允许 `log`、`show`、`rev-parse`，参数以列表传入、不经 shell；子进程不继承扁鹊的环境，忽略系统与全局 Git 配置，以 `-c safe.directory` 标记仓库 | 证据包的 `repo.fast-export` 导入临时仓库后，使用同一个 `git.py` |

证据包（`eval/cases/<用例>/pack/`，格式见 `backend/bianque/tools/pack/format.py`）是录制时整个命名空间与清单仓的快照，而不是逐次调用的录音，所以模型换一种调用顺序或参数，回放后端仍能回答：

```text
meta.json          格式版本、用例、命名空间、录制时间、组件版本
alert.json         Alertmanager webhook 原文
k8s/               pods.json、events.json、deployments.json、replicasets.json
logs/              <pod>/<container>.log、<pod>/<container>.previous.log
repo.fast-export   清单仓的 git fast-export 文本
faults.yaml        可选：回放时注入的故障，例如某个工具超时
```

回放时工具以证据包的录制时间为「当前时间」，时间窗口选出的事件与录制时相同。证据包由 `bianque pack record` 在演示形态下录制（`make record-pack` 包装了注入故障、取告警与录制），录制时去掉 `managedFields` 与 last-applied 注解，把私有地址泛化为只保留前两段，写完后用脱敏引擎的 `detect()` 检查每个文件。`bianque pack derive` 从已有证据包派生新用例，例如注入工具超时或只保留部分文件。

## 5. 证据门

证据门（`backend/bianque/gate/`）是一组确定性校验器，唯一入口是 `validate(output_type, output, context)`，运行时与评测调用同一个函数。证据门不依赖模型网关（import-linter 契约）。

### 5.1 输出类型与 schema

每种模型输出是一个 pydantic 模型（`backend/bianque/gate/schemas.py`），同一个模型既是强制模型调用的函数 schema，也是 `schema` 校验器使用的 schema。所有模型禁止未知字段。

| 输出类型 | 模型 | 产生者 | 函数 |
| --- | --- | --- | --- |
| `report` | `Report` | 调查 Agent 的 `report` 节点 | `submit_report` |
| `plan` | `Plan` | `plan`、`replan` 节点 | `submit_plan` |
| `assessment` | `Assessment` | `assess` 节点 | `submit_assessment` |
| `findings` | `Findings` | 取证子 Agent | `submit_findings` |
| `followup_answer` | `FollowupAnswer` | 追问流程 | `submit_answer` |

报告包含 `summary`、`impact`、`timeline`、`root_cause`、`claims`、`missing_evidence`、`suggestions`、`references`、`followup_questions`，全部字段必填。`root_cause` 的 `status` 是 `identified` 或 `unknown`，`identified` 时必须给出 `category` 与 `object`（`kind`、`namespace`、`name`、可选 `field`）；`category` 是有限枚举 `RootCauseCategory`：`config_error`、`resource_limit`、`probe_misconfig`、`image_error`、`dependency_unavailable`、`code_defect`、`other`。修复建议带风险等级 `read_only` 或 `change`。`bianque gate export-schemas` 导出全部输出类型的 JSON Schema。

### 5.2 校验器与校验清单

校验器在 `backend/bianque/gate/validators.py`，每种输出类型的校验清单在 `backend/bianque/gate/checklists.py`（`CHECKLISTS`）。`schema` 先运行，解析失败时其余校验器不运行；没有登记校验清单的输出类型一律拒绝。

| 校验器 | 规则 | `report` | `followup_answer` | `plan` | `assessment` | `findings` |
| --- | --- | --- | --- | --- | --- | --- |
| `schema` | 按 schema 严格解析，枚举取值合法 | ✓ | ✓ | ✓ | ✓ | ✓ |
| `citation_integrity` | 引用的编号都属于本调查；报告的时间线条目、结论、修复建议与非未知的根因，回答的非「证据不足」段，每条发现，都至少有一条引用。回答还可以引用修复建议编号 | ✓ | ✓ | | | ✓ |
| `knowledge_boundary` | `kn_` 编号只能出现在 `references`（计划里是 `knowledge_refs`），且必须是给过模型的知识编号；出现在引用或其他文本里即违规 | ✓ | ✓ | ✓ | | ✓ |
| `unknown_discipline` | 根因没有引用时 `status` 必须为 `unknown`；`unknown` 时置信度必须为 `low` | ✓ | | | | |
| `missing_evidence_consistency` | 本调查有失败的取证调用时 `missing_evidence` 不能为空；失败的调用属于 `failure_caps_confidence` 为真的证据域（`logs`）时，根因置信度不能为 `high` | ✓ | | | | |
| `forbidden_actions` | 写类命令（`backend/bianque/gate/forbidden.py`：`kubectl`、`oc`、`helm`、`git` 的写动词）只能出现在报告里风险为 `change` 的修复建议的动作或命令中，其余位置一律违规 | ✓ | ✓ | ✓ | ✓ | ✓ |
| `redaction_residue` | 用脱敏引擎的 `detect()` 扫描全部文本字段，保留标识符之外有命中即违规 | ✓ | ✓ | ✓ | ✓ | ✓ |

校验上下文（`GateContext`）包含本调查的证据编号、给过模型的知识编号、修复建议编号（追问）、保留标识符、失败的取证调用与脱敏引擎。保留标识符是本调查全部证据的标识符、告警的标识符，加注册表里的全部工具名：告警名与工具名常常超过高熵检测的长度阈值。

违规记录为「校验器名、JSON 路径、固定代码」，不回显输出正文；路径里模型自编的键写成 `*`。被拒时违规列表写进对应记录（报告写 `reports.gate_result`，其余写模型调用记录的 `gate_result`），并作为反馈放进下一次生成的输入。

### 5.3 重试与退回

| 输出类型 | 被拒后 | 仍不通过时 |
| --- | --- | --- |
| 计划 | 带违规列表重试 1 次（`generate_gated`，`backend/bianque/agents/gated.py`） | 退回默认计划（来源记为 `default`），不经模型 |
| 判断 | 重试 1 次 | 由确定性充分性规则决定，`rule_override` 为真 |
| 发现 | 重试 1 次 | 丢弃发现，证据保留 |
| 报告 | 最多再生成 `BIANQUE_REPORT_MAX_RETRIES` 次 | 降级报告 |
| 追问回答 | 重试 1 次 | 发 `failed` 事件（`gate_rejected` 与校验器名） |

在证据门之前，模型网关的 `complete_structured` 已把输出 schema 声明为函数并强制模型调用，参数不能通过 pydantic 校验时给模型一次修正机会；修正后仍不合法按 `schema` 违规处理。被拒的输出只留在模型调用记录与 `reports` 的被拒尝试里，不进入业务表、事件与接口：接口对被拒的报告只返回拒绝它的校验器名称。

模型服务重试后仍不可用（`LLMUnavailableError`）时：计划、判断与发现按上表退回；报告节点直接转入降级报告，降级报告的摘要列出之前拒绝过报告的校验器。

### 5.4 降级报告

`build_degraded_report`（`backend/bianque/gate/degrade.py`）不调用模型，只读证据：

- 根因为 `unknown`，置信度 `low`，没有修复建议；
- 结论是每条证据的来源汇总，加上从 Pod 状态与事件证据的结构化字段读出的事实，以及规则提示：容器终止原因 `OOMKilled` 提示 `resource_limit`，等待原因 `ImagePullBackOff`、`ErrImagePull`、`InvalidImageName` 提示 `image_error`，`CreateContainerConfigError` 提示 `config_error`，存活探针失败的 `Unhealthy` 事件提示 `probe_misconfig`；
- 时间线来自带时间的事实，缺失证据来自失败的取证调用。

取值只来自解析后的字段，不从证据文本里截取，所以不会切开脱敏占位符。降级报告同样过报告的校验清单；不通过视为代码缺陷，调查结束为 `failed`。

## 6. 脱敏

任何文本进入模型之前都经过脱敏引擎（`backend/bianque/redaction/`）。模型网关（`backend/bianque/llm/gateway.py`）是唯一导入提供方层的模块，它在三处把关：

1. 静态类型：网关方法只接受 `RedactedPrompt`，其内容是 `RedactedText`；`RedactedText` 只能由 `redact_for_llm()` 构造（`backend/bianque/llm/redacted.py`），mypy strict 拒绝普通 `str`。
2. 运行时：`isinstance` 检查消息内容与工具调用参数的类型，覆盖动态构造与反序列化。
3. 发送前复查：对每条消息与每个工具调用参数再跑一次 `detect()`，有残留就抛出 `RedactionResidueError`，什么也不发送。

脱敏引擎先跑规则（`patterns.py`：键值形式的口令、令牌、密钥，`Authorization` 与 Bearer 头，Cookie，带凭据的 DSN，私有 IPv4 地址泛化为 `10.12.x.x` 形式，配置的内部域名后缀泛化为 `*.internal`），再跑高熵兜底（`entropy.py`：20 个字符以上、Shannon 熵超过阈值的串替换为保留首尾两个字符的占位符）。引擎是确定性的且幂等，同样的输入得到同样的输出，这是录制回放的前提。内部域名后缀由 `BIANQUE_REDACT_INTERNAL_DOMAINS` 配置。

高熵兜底对以下内容不遮蔽：

- 保留标识符：Pod 名、镜像、提交号等对象标识随工具结果与告警标签传入（`preserve` 参数），模型才能把它们用于下一次工具调用，镜像拉取类故障也能写明是哪个镜像。规则类检测对保留标识符照常生效。
- 带算法前缀的摘要（如 `sha256:<64 位十六进制>`）与带标签的 trace、span 编号。
- 只由单词组成的串，例如 Kubernetes 的字段名与状态原因（`ProgressDeadlineExceeded`、`maxUnavailable/maxSurge`），以及其中的资源数量与数组下标。

保留标识符随证据存进 `evidence.identifiers`，证据门的 `redaction_residue` 使用同一集合。证据表同时保存原文与脱敏文本；接口与界面只返回脱敏文本。调用记录的摘要先脱敏再截断到 2000 字符。

## 7. 知识检索

知识是本项目原创的英文运维手册（`knowledge/runbooks/`），覆盖 CrashLoopBackOff、OOMKilled、探针、配置错误、镜像拉取、Pending Pod、修订历史、变更归因、事件与日志等主题。

| 阶段 | 实现 |
| --- | --- |
| 分块 | `backend/bianque/knowledge/chunking.py`：H1 是标题，按 H2、H3 分块，标题链用 ` > ` 连接记为 `heading_path`；嵌入文本（标题链加正文）超过 300 个词时按段落、再按句子切分。上限按词计而不是按 token 计，因为 `fixture` 模式没有分词器，两种模式必须切出同样的块 |
| 导入 | `bianque knowledge ingest`（`ingest.py`）：文档 SHA-256 未变则跳过，变了则重建分块，目录里已删除的文档连同分块删除 |
| 嵌入 | `local`：`BAAI/bge-small-en-v1.5`（fastembed，ONNX，384 维），模型文件由 `bianque knowledge download-model` 下载并核对 SHA-256，运行时只读本地文件；`fixture`：读 `eval/embeddings/bge-small-en-v1.5.json`，向量以 float32 原始字节存放，查不到文本时报错 |
| 全文 | `knowledge_chunks.tsv` 是生成列，标题链权重 A、正文权重 B，建 GIN 索引；查询把 `plainto_tsquery` 的「与」改为「或」，按 `ts_rank_cd` 排序 |
| 向量 | 精确余弦距离排序，不建近似索引 |
| 融合 | `search.py`：两路各取前 20，RRF 分数为各路 `1 / (60 + 名次)` 之和，取前 5；所有排序最后按知识编号决胜，结果在不同机器上一致 |
| 降级 | 嵌入或向量查询失败时只用全文一路，结果标 `degraded: vector_unavailable` |

调查的检索查询是告警名（按驼峰拆成词）加注解的值（`query.py`）；命名空间、Pod 等标签是对象名，对选择手册没有信息量，不放进查询。向量夹具由 `bianque knowledge embed-fixtures`（`make knowledge-fixtures`）用本地模型生成，覆盖全部分块、评测用例的检索查询与追问问题、检索评测查询。使用夹具的原因：录制与 CI 在不同 CPU 架构上运行，同一模型的浮点结果可能有微小差异，检索排序一变，提示词就变，录制响应就对不上。

知识不是证据：提示词把知识放在单独的数据段，证据纪律要求知识只能列为参考资料（`prompts/discipline.md`），证据门的 `knowledge_boundary` 拒绝任何出现在引用或正文里的 `kn_` 编号。检索评测（`bianque eval retrieval`，查询与标注在 `eval/retrieval/queries.yaml`）报告 hit@1、hit@3、hit@5 与 MRR。

## 8. 追问与记忆

报告发布（正式报告或降级报告）后，可以对调查开追问会话。追问不是一张长期运行的图，而是每个问题执行一次的短流程（`backend/bianque/agents/followup.py`）：

1. 读出调查的全部证据（脱敏文本）、最终报告、修复建议与本会话最近 10 轮问答；检查本会话的 token 用量是否已达 `BIANQUE_FOLLOWUP_TOKEN_BUDGET`（默认 150000），已达则直接结束（`budget_exhausted`）。
2. 用问题检索运维手册（直接调用知识模块，不经工具执行器），写一条 `kind=knowledge` 的调用记录。
3. 模型以函数 `submit_answer` 返回分段回答：每段有文本、引用（证据编号或修复建议编号）与「证据不足」标志，另有参考的知识编号。
4. 按追问回答的校验清单校验，被拒重试 1 次。
5. 通过后，问题与回答在同一个事务里写入 `followup_messages`，然后才把各段文本推送给客户端。

追问只用已有证据，不调用取证工具。

响应是 `POST /api/sessions/{id}/messages` 返回的 SSE 流：

| 事件 | 字段 | 说明 |
| --- | --- | --- |
| `progress` | `stage`（`retrieving`、`generating`、`validating`、`retrying`）、`elapsed_ms` | 阶段不变时每 2 秒重复一次；没有可以携带模型正文的字段 |
| `content` | `segment_index`、`text`、`citations` | 只在校验通过并写库之后发送 |
| `done` | `message_id`、`validated: true` | |
| `failed` | `reason`（`gate_rejected`、`model_unavailable`、`model_error`、`budget_exhausted`）、`violations`（校验器名称） | 不含模型正文 |

未通过证据门的文本因此不会到达客户端；代价是正文要等整段生成并校验后才开始推送，生成期间客户端只看到阶段与耗时。

追问在独立的后台任务里运行，客户端断开不会取消它，已经生成的回答仍会校验并保存。同一会话同时只能回答一个问题，否则返回 409。记忆就是数据库里的调查数据与会话问答，进程重启后从数据库重建上下文，不依赖进程内状态。追问的模型调用与知识检索只记 `session_id`，不计入调查的 token。

`recorded` 模式下请求键包含本会话之前的问答，所以只能按录制顺序问录制过的问题：`GET /api/investigations/{id}/questions` 列出用例 `questions.yaml` 里的问题与 `recorded_only` 标志，提交的不是本会话下一道题时返回 422 并给出应问的题，不调用模型。

## 9. 人工确认

正式报告发布时，每条修复建议写一行 `suggestions`（编号见第 2.4 节），之后不再修改。降级报告没有修复建议。

登录用户通过 `POST /api/investigations/{id}/suggestions/{sg}/decision` 确认或驳回一条建议，可以附说明。处理流程只做一件事：插入一行 `confirmations`。不运行任何工具，不调用数据库之外的任何系统；代码里也不存在写集群或写 Git 仓的路径（[ADR 0002](./adr/0002-human-confirmation-records-only.md)）。

`confirmations` 在数据库层面只追加：

- 触发器 `confirmations_append_only` 在 `BEFORE UPDATE OR DELETE` 时抛出异常（`backend/migrations/versions/0001_initial.py`）；
- `(investigation_id, suggestion_id)` 唯一约束保证每条建议只能决定一次，已决定的建议返回 409；并发的两次决定由唯一约束裁决。

建议的状态（待确认、已确认、已驳回）由有无确认记录推出，不存状态字段。写类命令只能出现在风险为 `change` 的建议里，由证据门的 `forbidden_actions` 保证。

## 10. 接口与事件

HTTP 接口在 `backend/bianque/api/`，由 FastAPI 提供。

| 方法与路径 | 说明 | 需要登录 |
| --- | --- | --- |
| `POST /api/alerts/alertmanager` | Alertmanager webhook；配置了 `BIANQUE_ALERTMANAGER_TOKEN` 时校验 Bearer 令牌；返回 202 | 否（令牌） |
| `POST /api/investigations` | 手动触发：`case_id`（回放一个用例，要求 `pack` 后端）或 `alert_id`（重查已收到的告警）；不去重 | 是 |
| `GET /api/investigations` | 调查列表 | 否 |
| `GET /api/investigations/{id}` | 详情：告警、计划各版本与判断、步骤与发现、证据（脱敏文本）、最终报告、被拒尝试的校验器名、修复建议与确认状态 | 否 |
| `GET /api/investigations/{id}/events` | 进度事件的 SSE 流 | 否 |
| `GET /api/investigations/{id}/calls` | 调用记录与合计（第 12 节） | 否 |
| `POST /api/investigations/{id}/suggestions/{sg}/decision` | 确认或驳回修复建议 | 是 |
| `POST /api/investigations/{id}/sessions` | 建追问会话（调查要有最终报告） | 是 |
| `GET /api/investigations/{id}/sessions` | 该调查的追问会话 | 否 |
| `GET /api/investigations/{id}/questions` | 录制过的追问问题 | 否 |
| `GET /api/sessions/{id}` | 会话历史 | 否 |
| `POST /api/sessions/{id}/messages` | 提问，响应是 SSE 流（第 8 节） | 是 |
| `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/session` | 登录、登出、当前登录人 | |
| `/mcp` | MCP 服务（第 11 节） | 否（令牌） |
| `GET /healthz` | 存活检查 | 否 |

读接口不需要登录，写操作（触发、确认、追问）需要登录。账号由 `BIANQUE_DEMO_USERS` 配置（用户名到 bcrypt 哈希），`make replay` 与 `make demo` 每次生成随机口令。会话不存服务端（`backend/bianque/auth/sessions.py`）：Cookie 里是签名令牌（用户名与签发时刻，HMAC-SHA256），签名密钥由 `BIANQUE_SESSION_SECRET` 与该用户当前的口令哈希派生，换口令或换密钥使旧令牌失效；有效期 `BIANQUE_SESSION_TTL_SECONDS`（默认 12 小时）。Cookie 为 `HttpOnly`、`SameSite=Strict`、`Path=/api`，`Secure` 由 `BIANQUE_SESSION_COOKIE_SECURE` 控制。前端与接口同源，nginx 对两个 SSE 路径与 `/mcp` 关闭代理缓冲并延长读超时（`frontend/nginx.conf.template`）。

### 10.1 进度事件

进度事件是 `investigation_events` 表的行，每个调查内的 `seq` 连续递增。每种事件类型有一个 payload 模型（`backend/bianque/agents/events.py`），写入前校验；字段只有编号、状态、计数与校验器名称，字符串字段都受枚举或编号格式约束，没有字段能携带模型文本。页面收到事件后再从详情接口读取已通过校验的内容。

| 事件 | 字段 |
| --- | --- |
| `investigation_started` | `trigger` |
| `plan_created` | `plan_id`、`version`、`source` |
| `step_started`、`step_finished` | `step_id`、`domain`、`status` |
| `evidence_added` | `evidence_id`、`source`、`tool` |
| `assessment_recorded` | `round`、`sufficient`、`rule_override` |
| `report_rejected` | `attempt`、`violations` |
| `report_published`、`report_degraded` | `report_id` |
| `investigation_finished`、`investigation_interrupted` | `status` |

`GET /api/investigations/{id}/events` 的每帧带 `id: <seq>`。流从 `Last-Event-ID`（浏览器 `EventSource` 重连时发送）与查询参数 `after`（页面重新加载时发送）中较大的一个之后开始，在 `investigation_finished` 或 `investigation_interrupted` 之后结束；空闲时每 15 秒发一行注释保持连接。进程内的 `EventNotifier` 在事件提交后唤醒该调查的读者；读者被唤醒或等满 1 秒后都重新读表，数据库是事件的唯一来源（[ADR 0004](./adr/0004-postgresql-only.md)）。

## 11. MCP 服务

MCP 服务（`backend/bianque/mcp/`）用 MCP Python SDK 的 Streamable HTTP 传输，挂在后端同一进程的 `/mcp`，无状态、以 JSON 应答，请求之间不保存会话。工具定义由注册表生成（名称、描述、输入 schema，并标注只读），调用走同一个 `ToolExecutor`，超时、重试、脱敏与调用记录与子 Agent 的调用完全相同（[ADR 0003](./adr/0003-single-tool-registry-and-mcp.md)）。

| 机制 | 实现 |
| --- | --- |
| 客户端配置 | `BIANQUE_MCP_CLIENTS`：JSON 数组，每项 `name`、`token_sha256`、`tools`（名称或 `fnmatch` 通配）、`namespaces`；通配在启动时按注册表展开，匹配不到任何工具即启动失败 |
| 认证 | `/mcp` 前的 ASGI 中间件 `BearerAuth` 计算 Bearer 令牌的 SHA-256 并做常量时间比较；无令牌或令牌不属于任何客户端返回 401 |
| 工具集合 | `list_tools` 只列出该客户端允许的工具；直接调用其他工具由执行器拒绝 |
| 命名空间范围 | 执行器检查所有带 `namespace` 参数的调用；`investigation_list` 与 `investigation_get_report` 只返回告警在该命名空间的调查，其他命名空间的调查按不存在处理 |
| 调用记录 | 每次调用写一条调用记录，`agent` 为 `mcp`，`caller` 为客户端名；拒绝也写 |
| 错误 | 失败返回 MCP 工具错误结果，内容是结构化的 `{"error": {"code", "message"}}` |

MCP 调用不属于任何调查，不产生证据；`pack` 后端下读 `BIANQUE_PACK_CASE` 指定用例的证据包。`make replay` 与 `make demo` 每次生成随机令牌，只把它的 SHA-256 配置给后端；客户端接入方式见 README。

## 12. 可观测性

每次模型调用、工具调用与知识检索各写一行 `call_records`：

| 字段 | 内容 |
| --- | --- |
| `kind` | `model`、`tool`、`knowledge` |
| `agent` | `investigation`、`kubernetes`、`logs`、`change`、`followup`、`mcp` |
| `name` | 模型调用是调用点（`plan`、`assess`、`replan`、`report`、`evidence_agent`、`answer`），工具调用是工具名，知识检索是 `retrieve_knowledge` |
| `investigation_id`、`session_id`、`caller` | 所属调查、追问会话、MCP 客户端 |
| `input_summary`、`output_summary` | 脱敏后截断到 2000 字符 |
| `status`、`error`、`retries` | 结果、错误、重试次数 |
| `started_at`、`latency_ms` | 开始时间与耗时 |
| `tokens_in`、`tokens_out` | 模型调用的 token |
| `gate_result` | 该输出的证据门结果 |

模型调用的记录由网关的 `CallRecorder` 写入（`DatabaseCallRecorder`，`backend/bianque/agents/records.py`），同一事务里把 token 累加到调查；工具调用由执行器写入；知识检索由调查图与追问流程写入。证据记录了产生它的调用编号。

`GET /api/investigations/{id}/calls` 返回调用记录与合计（调用次数按类型分、输入与输出 token、耗时之和、从调查开始到结束的总时长）。模型调用的输入、输出摘要与错误文本不返回，只返回名称、Agent、耗时、token、状态与证据门结果的校验器名；工具调用与知识检索返回摘要。界面上：

- 调用时间线（`frontend/src/components/CallTimeline.tsx`）按 Agent 分行，横轴是调查开始后的秒数，可展开每次工具调用的参数、结果摘要、耗时与重试；
- 流程图（`FlowGraph.tsx`）按取证子 Agent 分泳道，每个计划版本一列，颜色表示步骤状态；
- 调用记录表（`CallTable.tsx`）列出全部调用与合计。

## 13. 确定性与录制回放

### 13.1 模型模式

模型网关的三种模式经同一个提供方接口接入（`backend/bianque/llm/providers/`），业务代码不知道当前模式（[ADR 0005](./adr/0005-recorded-model-responses-for-eval.md)）：

| 模式 | 实现 | 用途 |
| --- | --- | --- |
| `live` | `openai_compat.py`：OpenAI 兼容接口，`BIANQUE_LLM_BASE_URL`、`BIANQUE_LLM_MODEL`、`BIANQUE_LLM_API_KEY`（可用 `file:` 引用密钥文件）、`BIANQUE_LLM_EXTRA_BODY`；`temperature=0`；超时 `BIANQUE_LLM_TIMEOUT_SECONDS`，超时、连接错误、429 与 5xx 按指数退避重试 `BIANQUE_LLM_MAX_RETRIES` 次 | 演示形态、重新录制 |
| `recorded` | `recorded.py`：按请求键在 `eval/recordings/<用例>/` 查出录制的响应，只查本调查用例的目录；查不到即报错，不回退到其他模式 | 回放形态、CI 评测 |
| `scripted` | `bianque.llm.scripted` 加 `backend/bianque/agents/scripted_rules.py`：按 Agent 角色读请求里的证据与工具决定响应的规则；只能显式开启 | 单元、集成与端到端测试 |

结构化输出统一用 function calling：输出 schema 声明为函数并强制模型调用，只依赖 OpenAI 兼容接口都支持的 `tools` 参数。

### 13.2 请求键与录制清单

请求键（`backend/bianque/llm/request.py`）是「Agent 名称、规范化的消息列表、工具 schema、采样参数、模型名」规范 JSON 的 SHA-256；调查编号与会话编号不在请求里。录制文件保存规范化的请求与响应，文件名含请求键前 16 位。

`eval/recordings/manifest.json`（录制清单，`backend/bianque/config/manifest.py`）记录录制所用的模型名、`extra_body`、模型服务主机、录制时间、用例列表、每个提示词文件的 SHA-256 与全部工具 schema 的 SHA-256。`recorded` 模式从清单读取模型名与 `extra_body`（两者都进请求键），显式配置与清单不一致时启动失败。

### 13.3 提示词的确定性

请求键要在每次运行中保持不变，提示词的构造（`backend/bianque/agents/prompting.py`）遵守这些规则：

- 不放当前时间；回放时工具的「当前时间」取证据包的录制时间。
- 不放调查编号、会话编号；证据编号与修复建议编号由内容决定（第 2.4 节）。
- 证据、发现、失败列表与违规列表按编号排序后拼接；数据段用固定标题（`Evidence`、`Step`、`Findings`、`Failed calls`、`Knowledge` 等）。
- 同一轮多个工具调用的结果按模型给出的顺序回填，与完成先后无关。
- 工具错误只写「类别: 对象」，不写耗时与重试次数。
- token 预算只在两轮之间检查。
- 子 Agent 的保留标识符只取告警、工具名与本证据域的证据，不受并行运行的其他子 Agent 影响。
- 检索使用向量夹具，排序在不同机器上一致。

`backend/tests/agents/test_investigation_concurrency.py` 在 `scripted` 模式下打乱子 Agent 的完成顺序，断言每个请求键不变。

提示词放在 `backend/bianque/agents/prompts/`，每个模型调用点一个 Markdown 文件（`plan.md`、`replan.md`、`assess.md`、`report.md`、`evidence_agent.md`、`followup.md`），共用的证据纪律在 `discipline.md`，占位符用 Python `string.Template`。提示词只写角色、证据纪律与本步任务；输出格式由函数 schema 约束，工具说明来自注册表生成的 schema。改提示词、工具 schema 或模型都会改变请求键，需要用 `make record` 重新录制：它对每个用例以 `live` 模式运行调查与追问并录制，再以 `recorded` 模式回放，用 `bianque eval compare` 核对回放逐项重现了录制那次运行，然后才替换 `eval/recordings/`。

### 13.4 评测

`bianque eval run`（`backend/bianque/evals/`）在一个空数据库里导入知识、运行检索评测，再对 `eval/cases/` 的每个用例回放调查、按 `questions.yaml` 逐题追问，计算指标并写 `report.json` 与 `report.md`。硬门槛都是确定性检查：每个调查以报告结束；每份最终报告与回答用运行时同一个 `validate` 与当时的上下文重新校验都通过；最终报告符合导出的 JSON Schema；没有注册写工具；证据没有来自 Agent 的；知识编号没有出现在引用里；加 `--check-network-isolation` 时确认评测进程连不上外部地址。根因判对率等准确率指标只报告，并注明模型模式与模型名。

## 14. 部署

### 14.1 回放形态（Docker Compose）

`make replay` 用 `deploy/compose/replay.yaml` 起 PostgreSQL（`pgvector/pgvector:pg16`）、后端与前端，页面在 `127.0.0.1:8080`。后端配置为 `BIANQUE_TOOL_BACKEND=pack`、`BIANQUE_EMBEDDING_MODE=fixture`、`BIANQUE_LLM_MODE=recorded`（`LLM_MODE` 可改为 `scripted` 或 `live`），仓库的 `eval/` 与 `knowledge/` 只读挂入。`BIANQUE_REPLAY_SEED=true` 让后端启动后导入知识，并对每个有证据包、还没有回放调查的用例依次建一次调查。数据库口令在第一次运行时随机生成（数据卷保留它），演示口令、会话密钥与 MCP 令牌每次运行重新随机生成，都写进被 Git 忽略的 `.demo/`；MCP 客户端只配置令牌的 SHA-256。`LLM_MODE=live` 时叠加 `deploy/compose/live.yaml`，把密钥文件只读挂进容器并以 `file:` 引用，密钥不进环境变量与镜像。

后端镜像启动时先执行 `alembic upgrade head`，再启动 uvicorn。

### 14.2 演示形态（kind 与 Helm）

`make demo` 在本机建 kind 集群，安装 Prometheus、kube-state-metrics 与 Alertmanager（`demo/monitoring/`），部署演示应用到 `demo-s1` 命名空间，然后用 Helm chart `deploy/helm/bianque` 安装扁鹊（演示 values 在 `demo/bianque/values.yaml`）。告警规则 `BianquePodRestarting`（`demo/monitoring/alert-rules.yaml`）在 `demo-s1` 的容器处于 CrashLoopBackOff 或 2 分钟内重启超过 2 次时触发，Alertmanager 以 Bearer 令牌把告警送到扁鹊的 webhook。`make inject-s1` 以固定的演示身份向本地清单仓提交故障并应用，`make restore-s1` 用 `git revert` 恢复。

chart 的组成：

| 资源 | 说明 |
| --- | --- |
| 后端 Deployment | `replicas: 1`，`strategy: Recreate`；非 root、禁止提权、只读根文件系统 |
| 前端 Deployment | nginx 静态文件与反向代理 |
| PostgreSQL StatefulSet | `pgvector/pgvector:pg16` |
| 知识导入 Job | Helm `post-install`、`post-upgrade` hook，从 ConfigMap 导入运维手册 |
| RBAC | ServiceAccount `bianque-backend`；ClusterRole `bianque-read` 只对 `pods`、`pods/log`、`events`、`deployments`、`replicasets` 授予 `get`、`list`、`watch`；按 `readNamespaces` 为每个命名空间建 RoleBinding，没有 ClusterRoleBinding |
| 清单仓 | 只在演示 values 里以只读 hostPath 卷挂入，供 `change` 域工具读取 |

chart 只按名字引用三个 Secret（数据库口令；演示账号、会话密钥、MCP 客户端与 webhook 令牌；模型密钥），自己不创建，所以口令、令牌与密钥不进 Helm release 记录。演示脚本在安装前创建它们，模型密钥以文件挂入并以 `file:` 引用。

### 14.3 CI

`.github/workflows/ci.yml`：

| 任务 | 内容 |
| --- | --- |
| `backend` | `make lint`（ruff、mypy、import-linter、环境变量前缀检查、Kubernetes 只读静态检查、shellcheck）与 `make test`（pytest，含 PostgreSQL 服务容器上的集成测试） |
| `eval` | `make eval`：`deploy/compose/eval.yaml` 把数据库与评测进程放在 Docker `internal` 网络里，没有外网；`recorded` 模式加向量夹具，不需要模型密钥与模型下载；报告作为构建产物上传 |
| `helm-rbac` | `helm lint`，渲染 chart（默认与演示 values）后由 `backend/tools/check_rbac.py` 检查 RBAC |
| `public-safety` | `tools/check-public-safety.sh` 扫描凭据、私钥、云密钥形态、本机路径与内网地址 |
| `frontend` | eslint、tsc、vitest、生产构建 |
| `e2e` | 以 `scripted` 模式起回放形态，Playwright 端到端测试；只在 PR 与 `main` 上运行 |

## 15. 安全边界

本节描述设计上的边界，不是安全评估。

| 边界 | 保证方式 |
| --- | --- |
| 对集群只读 | 凭据层：RBAC 只有 `get`、`list`、`watch`，只绑定到声明的命名空间（CI 检查渲染结果）。代码层：注册表拒绝非只读工具；`backend/tools/check_k8s_readonly.py` 静态检查 `bianque` 包只调用 Kubernetes 客户端 `read_` 与 `list_` 开头的方法；`git` 只允许 `log`、`show`、`rev-parse`。输出层：证据门的 `forbidden_actions` |
| 模型出口唯一且先脱敏 | 只有 `bianque/llm/gateway.py` 导入提供方层（`backend/tests/test_llm_egress_static.py` 检查）；类型门、运行时检查与发送前复查（第 6 节）。嵌入在本地计算，不经模型服务 |
| 模型文本不直接到达客户端 | 所有模型输出过证据门后才写入业务表；进度事件没有文本字段；被拒的输出只以校验器名出现；模型调用的摘要不经接口返回；追问先校验后推送 |
| 确认不执行 | 确认与驳回只插入一行只追加的确认记录，没有写外部系统的代码路径 |
| 写操作需要登录 | 触发、确认、追问要求有效的会话 Cookie；webhook 与 MCP 各自用 Bearer 令牌 |
| MCP 权限 | 每个客户端一个令牌，只存哈希；工具集合与命名空间范围由执行器检查，拒绝也写调用记录 |
| 凭据不进仓库 | 口令、令牌与密钥运行时生成或由 Secret、密钥文件提供；环境变量统一 `BIANQUE_` 前缀，只在 `bianque.config` 读取 |

## 16. 换一个领域要改哪几处

扁鹊的领域知识集中在下表几处。换到另一个领域（例如数据库或消息队列的告警）时，改这些位置：

| 改动点 | 位置 | 改什么 |
| --- | --- | --- |
| 取证工具与后端 | `backend/bianque/tools/`、`backend/bianque/tools/backends/` | 注册新领域的只读取证工具（证据域、证据来源、身份参数、超时、参数模型），各写真实后端与回放后端；证据包格式与录制命令（`backend/bianque/tools/pack/`） |
| 证据域声明 | `backend/bianque/agents/domains.py` | 域名称、第一轮是否允许、默认计划步骤、充分性规则要求它的根因类别、调用失败是否限制置信度，以及 `POD_STATUS_TOOLS` 对应的「必有证据」 |
| 根因类别 | `backend/bianque/gate/schemas.py` 的 `RootCauseCategory` | 新领域的根因类别枚举；`RootCauseObject` 描述的对象形态 |
| 写类命令模式 | `backend/bianque/gate/forbidden.py` | 新领域的写操作命令与动词 |
| 降级报告的规则提示 | `backend/bianque/gate/degrade.py` | 从结构化证据推出根因类别提示的规则 |
| 提示词 | `backend/bianque/agents/prompts/` | 领域说明与证据纪律中的领域举例 |
| 告警接入 | `backend/bianque/api/alertmanager.py` | 告警证据的文本与保留标识符（`IDENTIFIER_LABELS`）；新的告警来源加自己的字段映射 |
| `scripted` 规则 | `backend/bianque/agents/scripted_rules.py` | 测试用的角色规则读的是 Kubernetes 与 Git 证据 |
| 界面的证据域列表 | `frontend/src/lib/flow.ts`（`LANES`）、`frontend/src/lib/timeline.ts`（`AGENT_ORDER`）、`frontend/src/lib/agents.ts` | 流程图泳道与调用时间线的行 |
| 知识与评测 | `knowledge/runbooks/`、`eval/cases/` | 新领域的运维手册、评测用例、证据包与追问问题；之后重新生成向量夹具（`make knowledge-fixtures`）并重新录制（`make record`） |
| 部署 | `deploy/helm/bianque/templates/rbac.yaml` | 新数据源的只读权限 |

不随领域变化的部分：调查图的结构与路由、取证子 Agent 的实现、模型网关与三种模型模式、脱敏引擎、工具注册表与执行器的规则、七个校验器的逻辑与校验清单机制、存储与迁移机制、进度事件与 SSE、追问流程、人工确认、MCP 服务与评测框架。
