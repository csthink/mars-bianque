# 扁鹊能力与代码对照

本文按功能领域列出扁鹊的能力：每一节先说明这一领域做什么，再列出实现它的主要模块与对应的测试。路径都相对于仓库根目录。整体结构见 [`architecture.md`](architecture.md)，运行方式见 [`README.md`](../README.md)。

## 规划与执行

调查 Agent 是一张 LangGraph 状态图：告警进来后检索运维手册、制定计划（之前可以先加载 skill，见下文「Skill」一节）、按证据域分派步骤、判断证据是否充分、不充分时补充计划，最后生成报告并通过证据门发布。判断由模型给出，再由确定性规则复核：没有 Pod 状态证据，或假设的根因类别（如 `config_error`、`resource_limit`）需要 `change` 域证据而还没有时，一律判为不充分。计划被证据门拒绝且重试仍不通过时退回不经模型的默认计划。补充计划轮数、子 Agent 每步的模型轮数、报告重试次数与 token 预算都可配置；后端重启时未结束的调查标为中断，不从检查点续跑。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/agents/graph.py` | 调查图的节点与边 |
| `backend/bianque/agents/nodes.py` | 除取证子 Agent 外的全部节点：intake、retrieve_knowledge、plan、dispatch、assess、replan、report、gate、degrade、publish |
| `backend/bianque/agents/state.py` | 图状态：只含编号与少量控制值，业务数据在数据库 |
| `backend/bianque/agents/default_plan.py` | 默认计划 |
| `backend/bianque/agents/gated.py` | 生成结构化输出、过证据门、被拒后带违规信息重试一次 |
| `backend/bianque/agents/runner.py` | 运行一次调查，检查点写进 PostgreSQL 的 `langgraph` schema |
| `backend/bianque/agents/service.py` | 后台执行调查，启动时把未结束的调查标为中断 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/agents/test_investigation_s1.py` | 各证据包上的完整调查：计划版本、轮次、证据、调用记录，检查点在独立的 schema |
| `backend/tests/agents/test_investigation_failures.py` | 失败与退回路径：默认计划、确定性规则、降级报告 |
| `backend/tests/agents/test_agent_units.py` | 第一轮范围检查、提示词小节、告警映射、充分性规则 |
| `backend/tests/gate/test_default_plan.py` | 默认计划与它依据的证据域声明 |
| `backend/tests/api/test_api_interrupt.py` | 调查进行中后端被终止，下次启动标为中断 |

## 多源只读取证

取证工具分属三个证据域：`kubernetes`（Pod 列表、Pod 状态、事件、Deployment）、`logs`（容器当前与上一次的日志）、`change`（Deployment 修订历史、清单仓提交列表、单个提交的差异），共 8 个。每个工具在注册表中声明只读、证据域、参数模型、超时与身份参数；工具执行器负责权限检查、超时与重试、结果脱敏，并把调查中成功的结果存为证据。每个工具都有读真实集群与清单仓的后端和读证据包的回放后端，回放后端用证据包里的快照回答同样的查询。只读由三层保证：Kubernetes 后端只调用 `read_` 与 `list_` 方法并由静态检查确认，Git 后端只允许 `log`、`show`、`rev-parse`，Helm chart 的 RBAC 只授予 get、list、watch。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/tools/registry.py` | 工具注册表与声明规则 |
| `backend/bianque/tools/executor.py` | 工具执行器：调用工具的唯一入口 |
| `backend/bianque/tools/kubernetes.py` | `kubernetes` 域的取证工具 |
| `backend/bianque/tools/logs.py` | `logs` 域的取证工具 |
| `backend/bianque/tools/change.py` | `change` 域的取证工具 |
| `backend/bianque/tools/backends/kubernetes_live.py` | 真实 Kubernetes 后端 |
| `backend/bianque/tools/backends/git.py` | Git 命令行后端，真实后端与回放后端共用 |
| `backend/bianque/tools/backends/pack.py` | 回放后端：从证据包回答查询 |
| `backend/bianque/tools/pack/` | 证据包的格式、录制与派生 |
| `backend/tools/check_k8s_readonly.py` | 静态检查 Kubernetes 只读调用 |
| `backend/tools/check_rbac.py` | 检查 chart 渲染出的 RBAC |
| `deploy/helm/bianque/templates/rbac.yaml` | 扁鹊的 ServiceAccount 权限 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/tools/test_tool_registry.py` | 注册表拒绝不安全或不一致的声明 |
| `backend/tests/tools/test_tool_executor.py` | 权限检查、超时与重试、脱敏、证据写入 |
| `backend/tests/tools/test_tool_logs.py` | 日志折叠并截断到有界长度 |
| `backend/tests/tools/test_tool_pack.py` | 证据包的加载、版本、故障注入、派生与录制 |
| `backend/tests/tools/test_tool_pack_isolation.py` | 回放后端不需要集群与网络 |
| `backend/tests/tools/test_tool_cases.py` | 每个证据包都能提供用例要求的证据 |
| `backend/tests/tools/test_tool_s1a_chain.py` | S1-a 的工具链在脱敏后仍保留下一步需要的名称 |
| `backend/tests/tools/test_tool_kind.py` | 注入故障时对 kind 集群的集成测试 |
| `backend/tests/storage/test_tool_store.py` | 工具调用写入调用记录与证据 |
| `backend/tests/k8s_readonly_negative/test_k8s_readonly_negative.py` | 只读检查对写调用确实报错 |
| `backend/tests/k8s_rbac/test_check_rbac.py` | RBAC 检查拒绝每一种越权授予 |

## 多智能体

调查 Agent 负责计划、判断与报告；`kubernetes`、`logs`、`change` 三个取证子 Agent 是同一份实现，只取注册表里本证据域的工具。同一轮的子 Agent 由 LangGraph `Send` 分派，在同一个 superstep 内并行；每个步骤是一段 ReAct 对话，最后以函数 `submit_findings` 提交带证据编号的发现。子 Agent 自己捕获异常，一个子 Agent 失败只把它的步骤标为失败，其他子 Agent 的结果照常保留。证据域只在一处声明，第一轮范围检查、分组、默认计划、充分性规则与置信度上限都读这份声明。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/agents/subagent.py` | 取证子 Agent |
| `backend/bianque/agents/domains.py` | 证据域声明 |
| `backend/bianque/agents/snapshot.py` | 模型调用前从数据库读回的证据与发现 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/agents/test_investigation_concurrency.py` | 同一轮的子 Agent 并行执行，完成顺序不影响请求键 |
| `backend/tests/agents/test_investigation_failures.py` | 子 Agent 失败时调查照常结束 |
| `backend/tests/agents/test_investigation_s1.py` | 各轮分派的证据域与调用记录里的 Agent 名称 |

## 记忆与追问

报告发布后可以开一个追问会话提问。每个问题执行一次短流程：从数据库读出调查的证据、报告、修复建议与本会话最近的问答，检索相关运维手册，以函数调用生成分段回答，通过证据门后写入数据库，再经 SSE 分段推送；校验期间只推送进度。追问不调用工具，只用已有证据。会话记忆就是数据库里的问答行，后端重启后用同样的上下文回答。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/agents/followup.py` | 追问流程 |
| `backend/bianque/api/sessions.py` | 读取会话与提问接口，回答以 SSE 返回 |
| `backend/bianque/api/sse.py` | SSE 帧与流式响应 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/agents/test_followup_flow.py` | 追问流程、证据门拒绝与重试 |
| `backend/tests/api/test_api_sessions.py` | 手动触发、追问会话与回答流 |
| `backend/tests/api/test_api_restart.py` | 重启后的会话记忆与回放形态的启动 |

## 证据门

证据门对每个模型输出做确定性校验，通过后才写入业务表或展示。校验器共 7 个：`schema`、`citation_integrity`、`knowledge_boundary`、`unknown_discipline`、`missing_evidence_consistency`、`forbidden_actions`、`redaction_residue`；计划、判断、发现、报告与追问回答各有自己的校验清单。被拒时只把校验器名称、JSON 路径与违规代码反馈给模型重试；报告重试用尽后发布降级报告，降级报告的根因为未知，内容是证据汇总与规则提示，同样要通过报告的校验清单。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/gate/checklists.py` | 各输出类型的校验清单与唯一入口 `validate` |
| `backend/bianque/gate/validators.py` | 7 个校验器 |
| `backend/bianque/gate/schemas.py` | 计划、判断、发现、报告与追问回答的 schema，含根因类别枚举 |
| `backend/bianque/gate/forbidden.py` | 识别写类命令 |
| `backend/bianque/gate/degrade.py` | 降级报告与规则提示 |
| `backend/bianque/gate/context.py` | 校验所需的调查上下文 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/gate/test_gate_cases.py` | 样例集：每个样例从 `validate` 得到预期结果（样例在 `backend/tests/gate/cases/`） |
| `backend/tests/gate/test_gate_checklists.py` | 校验清单、入口与结果可以暴露的内容 |
| `backend/tests/gate/test_gate_schemas.py` | 输出 schema |
| `backend/tests/gate/test_gate_export.py` | 导出的 JSON Schema 与 pydantic schema 一致 |
| `backend/tests/gate/test_gate_forbidden.py` | 写类命令识别 |
| `backend/tests/gate/test_gate_degrade.py` | 降级报告能通过证据门 |

## 知识检索（RAG）

运维手册是 `knowledge/runbooks/` 下的 Markdown 文件，按 H2 与 H3 标题分块，用本地运行的 `BAAI/bge-small-en-v1.5` 生成向量，存进 PostgreSQL（pgvector）。检索把向量距离排序与全文检索排序用 RRF 融合；向量查询失败时退回只用全文检索并标明降级。知识只作参考资料：调查与追问直接调用知识模块，不经工具执行器，证据门禁止把知识编号当证据引用。评测用预先计算的嵌入向量固定数据，保证不同机器上检索结果一致。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/knowledge/chunking.py` | 按标题分块 |
| `backend/bianque/knowledge/embedding.py` | 本地嵌入模型与固定数据两种模式 |
| `backend/bianque/knowledge/fixtures.py` | 嵌入向量固定数据 |
| `backend/bianque/knowledge/ingest.py` | 导入运维手册目录 |
| `backend/bianque/knowledge/query.py` | 由告警生成检索查询 |
| `backend/bianque/knowledge/search.py` | 混合检索与 RRF 融合 |
| `backend/bianque/evals/retrieval.py` | 检索评测：hit@k 与 MRR |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/knowledge/test_knowledge_chunking.py` | 分块规则与长度上限 |
| `backend/tests/knowledge/test_knowledge_rrf.py` | RRF 公式 |
| `backend/tests/knowledge/test_knowledge_store.py` | 在真实 PostgreSQL 上导入与检索 |
| `backend/tests/knowledge/test_knowledge_fixtures.py` | 固定数据的读写与校验 |
| `backend/tests/knowledge/test_knowledge_corpus.py` | 运维手册、检索查询与固定数据相互匹配 |
| `backend/tests/knowledge/test_knowledge_eval.py` | 检索评测的指标与命令 |
| `backend/tests/agents/test_investigation_knowledge.py` | 调查中的知识检索与引用边界 |

## Skill

扁鹊怎么查一类故障写成 [Agent Skills](https://agentskills.io/specification) 格式的 skill，放在仓库根目录的 `.agents/skills/`（`investigate-pod-restarts`、`diagnose-config-error`、`diagnose-oomkilled`、`diagnose-probe-failure`、`attribute-gitops-change`），`.claude/skills` 是指向它的相对符号链接。skill 按证据域列出要用的工具、期望的证据与证据对应的根因类别，需要背景时写运维手册的主题名。启动时严格校验格式，不合规即启动失败。生成计划与补充计划时，系统提示词只列目录；模型调用 `activate_skill` 加载正文，名称限定为目录名称的枚举，同一次调查不重复注入，每次计划生成的激活次数有上限，到上限后只能提交计划；没有 skill 时不提供激活函数也不列目录。skill 不是证据也不是知识：证据门拒绝引用与参考资料里的 skill 名称。每次激活写一条调用记录，每版计划记录生成时加载过的 skill；`recorded` 模式启动时把每个 `SKILL.md` 的 SHA-256 与录制清单比对。取证子 Agent、判断、报告与追问不加载 skill。

| 模块 | 作用 |
| --- | --- |
| `.agents/skills/` | skill 文件 |
| `backend/bianque/skills/loader.py` | 读入目录并严格校验 |
| `backend/bianque/skills/activation.py` | 目录的渲染、`activate_skill` 的参数 schema 与结果 |
| `backend/bianque/agents/skill_loop.py` | 计划前的激活循环与调用记录 |
| `backend/bianque/agents/startup.py` | 启动时加载 skill，`recorded` 模式下比对提示词与 skill 的哈希 |
| `backend/bianque/agents/prompts/skills.md` | 计划提示词里的 skill 说明与目录 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/skills/test_skill_loader.py` | 格式的每条规则各一个反例；目录排序；激活的首次加载、已加载与被拒 |
| `backend/tests/skills/test_skill_files.py` | 仓库里的 skill 通过严格校验与 `skills-ref validate`；符号链接；没有脚本；正文里形如工具名的词都在注册表里 |
| `backend/tests/agents/test_plan_skills.py` | 提示词只有目录没有正文、枚举、已加载、上限、空目录、补充计划的 Skills 一节、改 skill 后请求键改变 |
| `backend/tests/gate/cases/` | `skill_cited` 的样例 |
| `backend/tests/test_llm_recorded_mode.py` | 提示词或 skill 与录制清单不一致时启动失败并指出文件 |

## MCP 服务

扁鹊在 `/mcp` 以 Streamable HTTP 提供 MCP 服务。工具说明来自同一个注册表，调用经过同一个工具执行器，超时、重试、脱敏与调用记录都与取证子 Agent 相同；另有 `knowledge_search`、`investigation_list`、`investigation_get_report` 三个辅助工具，结果不是证据。每个客户端的 Bearer 令牌绑定允许的工具与命名空间，配置里只存令牌的 SHA-256；被拒与失败的调用都写调用记录。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/mcp/server.py` | MCP 服务 |
| `backend/bianque/mcp/clients.py` | 客户端、令牌与授权范围 |
| `backend/bianque/mcp/sources.py` | 辅助工具读取已存的调查 |
| `backend/bianque/tools/knowledge.py` | 辅助工具 `knowledge_search` |
| `backend/bianque/tools/investigations.py` | 辅助工具 `investigation_list`、`investigation_get_report` |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/api/test_api_mcp.py` | 用 MCP Python SDK 客户端经 HTTP 调用 |
| `backend/tests/mcp/test_mcp_clients.py` | 客户端配置校验、工具模式展开、令牌匹配 |
| `backend/tests/mcp/test_mcp_server.py` | 工具结果与失败到 MCP 结果的转换 |
| `backend/tests/tools/test_tool_investigations.py` | 辅助工具按命名空间限定范围 |
| `backend/tests/tools/test_tool_knowledge.py` | `knowledge_search` 的结果不是证据 |

## A2A 服务

扁鹊是一个 A2A 服务端：别的 Agent 用 A2A 协议（JSON-RPC 绑定，协议版本 1.0）把一条告警交给扁鹊调查，收到任务状态与报告。Agent Card 在 `/.well-known/agent-card.json`，读取不需要令牌；`/a2a` 需要客户端的 Bearer 令牌，令牌绑定命名空间，配置里只存令牌的 SHA-256。演示形态接受一条 firing 的 Alertmanager 告警，与 webhook 走同一路径与指纹去重；回放形态接受一个用例编号；其他类型的片段返回内容类型不支持，其余无法服务的请求以 `REJECTED` 结束并写明原因代码。任务状态由调查现算，状态消息由代码按进度事件生成，不含模型文本；报告作为 Artifact 返回，与详情接口的报告相同。客户端只能读到自己的任务；推送通知、取消、多轮消息与扩展 Agent Card 都不提供，修复建议只能在扁鹊页面上确认。A2A 发起的调查与其他方式发起的调查发出相同的模型请求。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/a2a/card.py` | Agent Card |
| `backend/bianque/a2a/clients.py` | 客户端与调用上下文 |
| `backend/bianque/a2a/intake.py` | 发起：读 `data` 片段，判断是告警、用例还是要拒绝的请求 |
| `backend/bianque/a2a/tasks.py` | 由调查现算任务、状态消息与 Artifact；按调用方过滤的任务存储 |
| `backend/bianque/a2a/handler.py` | A2A 各操作：发起、流式、续传、读取、列表与不提供的操作 |
| `backend/bianque/a2a/server.py` | `/a2a` 端点：Bearer 认证与 SDK 的 JSON-RPC 分发 |
| `backend/bianque/auth/bearer.py` | 与 MCP 共用的 Bearer 令牌校验 |
| `backend/bianque/agents/records.py` 的 `follow_events` | 读进度事件，调查的 SSE 与 A2A 任务流共用 |
| `backend/migrations/versions/0005_a2a_tasks.py` | `a2a_tasks` 表与触发方式 `a2a` |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/api/test_api_a2a.py` | 用官方 A2A SDK 客户端经 HTTP 调用：流式到报告、报告与详情接口一致、认证、归属、每种拒绝原因、告警去重、断线续传、重启后的状态、不提供的操作、状态消息不含模型文本、请求键与经回放发起相同、`recorded` 模式全部命中录制 |
| `backend/tests/a2a/test_a2a_units.py` | Agent Card、客户端配置、发起判断、状态消息与报告摘要 |
| `backend/tests/agents/test_investigation_recorded.py` | 同一用例在 `recorded` 模式下再次调查全部命中录制 |
| `backend/e2e/test_e2e_a2a.py` | `make e2e`：经 nginx 用官方 SDK 客户端读 Card、发起、流式、续传、认证、命名空间、归属与不提供的操作 |
| `frontend/e2e/a2a.spec.ts` | `make e2e`：A2A 发起的调查在详情页显示「A2A」与调用方 |

## 人工确认

报告里的修复建议分为 `read_only` 与 `change` 两个风险级别，会改动系统的命令只能出现在 `change` 级建议里。登录用户可以对每条建议确认或驳回一次，系统记录决定、操作人与时间，不执行任何命令。决定表只追加：数据库触发器拒绝更新与删除，请求体里没有表示操作人的字段，操作人取自登录会话。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/api/investigations.py` | 读接口、手动触发、追问会话与决定接口 |
| `backend/bianque/api/auth.py` | 演示账号登录、登出与会话 |
| `backend/bianque/auth/` | 口令哈希与签名会话令牌 |
| `backend/bianque/storage/models.py` | `suggestions` 与 `confirmations` 表 |
| `backend/migrations/versions/0001_initial.py` | `confirmations` 只追加的触发器 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/api/test_api_decisions.py` | 确认与驳回、重复决定、未登录 |
| `backend/tests/api/test_api_auth.py` | 登录、登出与会话接口 |
| `backend/tests/auth/test_auth_units.py` | 口令与会话令牌 |
| `backend/tests/storage/test_repositories.py` | 仓储层，含决定表拒绝更新与删除 |
| `frontend/e2e/decisions.spec.ts` | 页面上确认与驳回修复建议 |

## 前端

前端是 React 与 TypeScript 单页应用，有登录、调查列表与调查详情三个页面。详情页包括告警卡片、按子 Agent 分泳道的流程图、实时进度、计划各版本与每轮判断、按 Agent 分组的调用时间线、证据列表、报告（引用可跳转到证据）、修复建议与确认、追问面板，以及调用记录表与 token、耗时合计。进度事件用 `EventSource` 接收，追问回答从 POST 响应的流里按 SSE 文法解析。

| 模块 | 作用 |
| --- | --- |
| `frontend/src/pages/` | 登录、调查列表、调查详情页面 |
| `frontend/src/components/` | 详情页的各个区块 |
| `frontend/src/api/` | 接口客户端、事件与追问流 |
| `frontend/src/lib/flow.ts` | 流程图布局 |
| `frontend/src/lib/sse.ts` | SSE 解析 |
| `frontend/src/lib/timeline.ts` | 调用时间线 |

| 测试 | 覆盖 |
| --- | --- |
| `frontend/tests/unit/components.test.tsx` | 组件渲染 |
| `frontend/tests/unit/interaction.test.tsx` | 登录、决定与追问的交互 |
| `frontend/tests/unit/flow.test.ts` | 流程图布局 |
| `frontend/tests/unit/sse.test.ts` | SSE 解析 |
| `frontend/tests/unit/timeline.test.ts` | 调用时间线 |
| `frontend/tests/unit/events.test.ts` | 进度事件的显示 |
| `frontend/e2e/detail.spec.ts` | 调查详情页 |
| `frontend/e2e/live.spec.ts` | 新触发调查的实时进度 |
| `frontend/e2e/followup.spec.ts` | 追问 |
| `frontend/e2e/auth.spec.ts` | 登录与登出 |
| `frontend/e2e/screenshots.spec.ts` | 宽屏与窄屏、浅色与深色截图 |

## 可观测性

每次模型、工具、知识与 skill 调用写一条调用记录：所属 Agent、名称、状态、错误、重试次数、开始时间、耗时、输入与输出 token、证据门结果；模型调用的 token 同时累加到调查的总量，用于预算检查。调查的进度事件写进数据库，经 SSE 推送，客户端带 `Last-Event-ID` 重连时从断点续读。事件载荷只含编号、状态、计数与校验器名称，不含模型文本。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/agents/records.py` | 模型调用记录与进度事件的写入 |
| `backend/bianque/agents/events.py` | 进度事件载荷与进程内通知 |
| `backend/bianque/tools/store.py` | 工具调用记录与证据的写入 |
| `backend/bianque/api/investigations.py` | 调用记录与进度事件流接口 |
| `frontend/src/components/CallTable.tsx` | 调用记录表与合计 |
| `frontend/src/components/CallTimeline.tsx` | 按 Agent 分组的调用时间线 |
| `frontend/src/components/PlanPanel.tsx` | 计划各版本、加载过的 skill 与每轮判断 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/api/test_api_events.py` | 事件流的实时推送、续读与结束 |
| `backend/tests/agents/test_event_payloads.py` | 事件载荷不含可携带模型文本的字段 |
| `backend/tests/test_llm_gateway_calls.py` | 模型网关的调用记录 |
| `backend/tests/api/test_api_investigations.py` | 调查详情与调用记录接口 |

## 评测

评测对每个用例回放证据包里的告警、按顺序提问用例的追问，计算根因准确率、引用完整性、必需证据覆盖、追问结果、证据门首次通过率与检索指标，并检查确定性的硬性门槛。模型响应来自用真实模型录制的录制响应，按请求键查找，查不到就报错；嵌入向量取自固定数据。`make eval` 在没有外网路由的 Docker 网络里运行；`make record` 用真实模型重新录制，每次运行的报告都保留，回放比对一致后才替换录制响应。报告逐用例列出加载过的 skill，`bianque eval diff` 把两份报告逐用例并排对比。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/evals/run.py` | 用例评测与硬性门槛 |
| `backend/bianque/evals/metrics.py` | 指标定义 |
| `backend/bianque/evals/cases.py` | 用例标注与追问文件 |
| `backend/bianque/evals/report.py` | `report.json` 与 `report.md` |
| `backend/bianque/llm/recording.py` | 录制响应的格式、写入与查找 |
| `backend/bianque/llm/request.py` | 请求键 |
| `backend/tools/record.sh` | 重新录制并回放比对 |
| `deploy/compose/eval.yaml` | 隔离网络里的评测 |
| `eval/cases/` | 用例、追问与证据包 |
| `eval/recordings/` | 录制响应与录制清单 |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/evals/test_eval_run.py` | `scripted` 模式下的端到端评测 |
| `backend/tests/evals/test_metrics.py` | 各指标的定义 |
| `backend/tests/evals/test_cases.py` | 用例文件的加载与报错 |
| `backend/tests/test_llm_recorded_mode.py` | 录制与按请求键回放 |
| `backend/tests/test_llm_request_key.py` | 请求键的组成 |

## 工程化

模型网关是访问模型服务的唯一出口，支持 `recorded`、`scripted`、`live` 三种模式，切换只改配置；它只接受脱敏后的输入，并在类型检查、运行时与发送前三处把关。import-linter 约束模块边界（例如只有 `bianque.api` 包导入 FastAPI，证据门不依赖模型网关，skill 包不导入扁鹊的其他包），环境变量统一以 `BIANQUE_` 为前缀且只在设置模块读取，这些静态检查都有反例测试证明它们会报错。部署有 Docker Compose（回放、live 与评测）与 Helm chart（演示形态），CI 有 6 个任务。

| 模块 | 作用 |
| --- | --- |
| `backend/bianque/llm/gateway.py` | 模型网关 |
| `backend/bianque/llm/providers/` | `openai_compat`、`recorded`、`scripted` 三种提供方 |
| `backend/bianque/redaction/` | 脱敏引擎：规则与高熵兜底 |
| `backend/bianque/config/settings.py` | 唯一读取环境变量的设置模块 |
| `backend/bianque/storage/` | ORM 模型、编号与仓储 |
| `backend/migrations/` | Alembic 迁移 |
| `backend/pyproject.toml` | 依赖、ruff、mypy 与 import-linter 契约 |
| `backend/tools/check_env_prefix.py` | 环境变量前缀检查 |
| `deploy/compose/` | 回放、live 与评测的 Compose 文件 |
| `deploy/helm/bianque/` | Helm chart |
| `.github/workflows/ci.yml` | CI |

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/test_llm_egress_static.py` | 模型服务只有一个出口，设置只有一个入口 |
| `backend/tests/test_llm_type_gate.py` | 未脱敏的输入无法通过类型检查 |
| `backend/tests/test_llm_redaction_gate.py` | 运行时检查与发送前复查 |
| `backend/tests/test_llm_modes.py` | 切换模型模式只改配置 |
| `backend/tests/test_llm_live_mode.py` | `live` 模式对 OpenAI 兼容端点 |
| `backend/tests/test_redaction_corpus.py` | 脱敏规则的回归样例 |
| `backend/tests/test_redaction_preserve.py` | 保留标识符不被高熵兜底改写 |
| `backend/tests/contracts_negative/test_contracts_negative.py` | import-linter 契约在被违反时报错 |
| `backend/tests/env_prefix_negative/test_env_prefix_negative.py` | 环境变量前缀检查在反例上报错 |
| `backend/tests/storage/test_migrations.py` | 迁移后的数据库与 ORM 模型一致 |
| `backend/tests/test_config.py` | 设置的前缀约定与默认值 |
