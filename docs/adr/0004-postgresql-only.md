# 0004 · 存储只用 PostgreSQL

## 状态

已采纳

## 背景

扁鹊需要持久化几类数据：

- 业务数据：告警、调查、计划、步骤、判断、证据、报告、修复建议、确认记录、调用记录、追问会话与问答。
- LangGraph 的检查点。
- 运维手册的分块、向量与全文索引。
- 调查的进度事件，页面通过 SSE 实时接收，断线后要能续传。

调查上下文与追问会话在进程重启后必须仍然可用。演示形态要在一台笔记本的 kind 集群里运行全部组件，回放形态与 CI 评测用 Docker Compose 运行，每多一个有状态组件，部署、内存占用与测试环境都更复杂。

常见的组合是关系数据库存业务数据、向量数据库存向量、Redis 存运行态、检查点与事件流。在扁鹊的数据规模下（知识只有数百个分块，每次调查数十个节点），这些需求 PostgreSQL 都能满足。

## 决定

1. 业务数据、LangGraph 检查点、知识向量与全文索引都放在 PostgreSQL 16（`pgvector/pgvector:pg16`）。检查点由 `langgraph-checkpoint-postgres` 管理，放在独立的 `langgraph` schema：迁移创建该 schema，检查点使用单独的连接池并把 `search_path` 设为 `langgraph`（`backend/bianque/agents/runner.py`），Alembic 只管理默认 schema。知识分块在同一张表里保存 384 维向量（pgvector，精确余弦距离）与带权重的 `tsvector` 生成列（GIN 索引）。
2. 调查的进度事件是 `investigation_events` 表的行，每个调查内的序号连续递增。SSE 断线重连按序号续传：从 `Last-Event-ID` 与查询参数 `after` 中较大的一个之后读起。同一进程内的实时推送由 `EventNotifier`（`backend/bianque/agents/events.py`）完成：事件提交后唤醒该调查的读者，读者被唤醒或等待超时后都重新读表，数据库是事件的唯一来源。
3. 后端单副本运行。调查与追问在 API 进程内以 asyncio 后台任务执行（`backend/bianque/agents/service.py`），Helm chart 的后端 Deployment 为 `replicas: 1`、`strategy: Recreate`。进程启动时把上一个进程留下的 `pending` 与 `running` 调查标为 `interrupted`，不从检查点续跑。

## 后果

正面：

- 各种形态都只有一个有状态组件，Compose 与 Helm 的配置更简单，内存占用更小，CI 只需要一个 PostgreSQL 服务容器。
- 追问的记忆就是数据库里的调查数据与会话问答，进程重启后从数据库重建上下文，不依赖缓存或进程内状态。
- 业务写入与事件写入使用同一种事务语义；模型调用记录与 token 累加在同一个事务里完成，预算检查读到的总是已提交的数据。
- 进度事件可以完整重放：页面重新加载或断线后，从任意序号继续读取。
- 知识检索的向量与全文两路在同一个数据库里查询，排序以知识编号决胜，结果可复现。

负面：

- 没有跨副本的事件广播。`EventNotifier` 只唤醒本进程的读者，多副本时需要改用 PostgreSQL 的 `LISTEN`/`NOTIFY` 或引入消息组件；调查的后台任务也需要改为由独立的工作进程领取。
- 单副本意味着后端重启期间服务不可用，进行中的调查被标为 `interrupted`，需要重新触发。
- 检查点在每个节点后写入，写入量随调查规模增长；在当前规模下不构成问题，规模扩大时可以只保留最后一个检查点或定期清理。
- 向量检索使用精确距离而不建近似索引，分块数量大幅增长后需要加 HNSW 一类的索引，并接受排序的近似性。
