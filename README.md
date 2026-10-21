# 扁鹊 Bianque

Bianque is an alert-driven root cause analysis agent for Kubernetes: when an alert fires, it plans an investigation, collects evidence with read-only tools, and writes a report in which every conclusion cites its evidence. It is under development.

扁鹊是告警驱动的 Kubernetes 根因分析 Agent：告警触发后规划调查、用只读工具取证，生成每条结论都引用证据的报告。开发中。

## 演示形态

演示形态是一个本机 kind 集群：演示应用 `s1-app`、Prometheus、Alertmanager、kube-state-metrics，以及用 Helm chart（`deploy/helm/bianque`）装进集群的扁鹊（后端、前端、PostgreSQL）。演示应用的清单放在本机的一个 Git 仓里，故障都通过提交清单引入；Alertmanager 把告警推给扁鹊，扁鹊读真实的集群与清单仓，调用真实模型。

前置工具与验证过的版本：

| 工具 | 版本 |
| --- | --- |
| Docker | 29.9 |
| kind | 0.33.0 |
| kubectl | 1.37 |
| Helm | 4.3 |
| jq | 任意版本（`make demo-alerts` 与 `make record-pack` 使用） |

另需一个模型密钥文件（默认 DeepSeek，模型与地址同回放形态的 `live` 模式，可用 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_EXTRA_BODY` 改）。首次 `make demo` 要联网：后端镜像在构建时下载本地嵌入模型并核对 SHA-256，运行时不访问外部的嵌入服务。

```bash
make demo LLM_API_KEY_FILE=<密钥文件>
```

完成后打印页面地址 `http://127.0.0.1:8180`（`BIANQUE_DEMO_PORT` 可改，建集群时生效，只监听本机回环地址）、演示账号 `demo` 的口令，以及 Claude Code 接入 MCP 的命令。口令、会话密钥、MCP 令牌、数据库口令与 Alertmanager 的 webhook 令牌都在本机生成，放在被忽略的 `.demo/kind/`，以 Secret 交给集群，不进 Helm 的 release 记录；模型密钥同样以 Secret 只读挂成文件，不进环境变量与镜像。

| 命令 | 作用 |
| --- | --- |
| `make demo LLM_API_KEY_FILE=<密钥文件>` | 起演示环境（下一行），再构建并载入扁鹊的后端与前端镜像，创建 Secret 与运维手册 ConfigMap，安装或升级 chart，导入运维手册。重复执行只更新有变化的部分，数据库保留 |
| `make demo-env` | 只起演示环境，不装扁鹊：创建 kind 集群 `bianque-demo`，在 `.demo/manifests` 初始化清单仓，构建并载入演示应用镜像，安装 Prometheus 与 Alertmanager，部署演示应用 |
| `make inject-s1 CASE=a` | 注入故障：`a` 把 `APP_MODE` 改成非法值；`b` 把内存限制改小；`c` 把存活检查指向不存在的路径；`e` 先做 `a`，再提交一次无关的改动 |
| `make restore-s1` | 回退注入的提交，等待演示应用恢复 |
| `make demo-alerts` | 列出 Alertmanager 当前的告警 |
| `make record-pack CASE=a` | 录制一个用例的证据包（`a`、`b`、`c`、`e`），只在 `make demo-env` 起的、没有扁鹊的环境里运行：把 `demo-s1` 重置到干净的基线，注入故障，用替身接收器取 Alertmanager 发出的第一条告警，把命名空间与清单仓的快照写到 `eval/cases/s1-<用例>/pack/`，跑一遍真实集群上的集成测试，最后恢复 |
| `make derive-packs` | 从 S1-a 的证据包派生 S1-d（回放时日志工具超时）与 S1-f（只留告警与 Pod） |
| `make helm-rbac` | 检查 chart：`helm lint`，并确认渲染出的 RBAC 只有 get、list、watch（CI 的 `helm-rbac` 任务） |
| `make demo-down` | 删除 kind 集群与 `.demo/kind/`；`.demo/` 的其余内容保留，要从头开始就另外删除 `.demo/` |

注入 S1-a 后约 1 分钟（实测 48 至 76 秒）Alertmanager 发出 `BianquePodRestarting` 告警，扁鹊随即开始调查，页面上能看到实时进度与流程图；实测一次 S1-a 调查约 70 秒、40 余次调用、约 10 万 token。告警规则只看 `demo-s1`；扁鹊的 ServiceAccount 只能在 `demo-s1` 里 get、list、watch Pod、Pod 日志、事件、Deployment 与 ReplicaSet，读不到其他命名空间。所有命令都指定 `kind-bianque-demo` 这个 kubectl context，建集群后会切回原来的当前 context，不会改动其他集群。

内存：kind 节点容器（集群里的全部 Pod 都在其中）在 `make demo` 完成后实测约 1.5 至 1.9 GiB，一次调查之后约 1.7 至 2.1 GiB；其中扁鹊后端加载嵌入模型后约 320 MiB。给 Docker 分 4 GiB 以上即可。

## 回放形态

回放形态不需要 kind 集群：Docker Compose 起数据库、后端与前端（nginx 提供页面，并把 `/api` 代理到后端，页面与接口同源）。后端启动后导入 `knowledge/runbooks/` 的运维手册，并对 `eval/cases/` 下每个有证据包的用例各跑一次调查；已经跑过的用例不再重跑，`make replay-down` 删除数据卷后重新开始。

模型模式由 `LLM_MODE` 选择：

- `recorded`（默认）：用 `eval/recordings/` 里录制的真实模型响应回放，不需要模型密钥；模型名与附加请求体取自录制清单 `eval/recordings/manifest.json`。
- `scripted`：规则响应器代替模型，用于测试。
- `live`：调用真实模型，追问可以自由提问。`LLM_API_KEY_FILE` 指向保存密钥的文件，密钥不进环境变量与镜像；模型默认 DeepSeek `deepseek-v4-pro`，可用 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_EXTRA_BODY` 改。启动前把本地嵌入模型下载到模型卷，运行时不访问外部的嵌入服务。

| 命令 | 作用 |
| --- | --- |
| `make replay` | 构建镜像并启动，健康检查通过后打印地址（默认 `http://127.0.0.1:8080`，`BIANQUE_REPLAY_PORT` 可改）与演示账号的口令。第一次运行时生成数据库口令，写进被忽略的 `.demo/replay.env` |
| `make replay LLM_MODE=scripted` | 同上，规则响应器代替模型 |
| `make replay LLM_MODE=live LLM_API_KEY_FILE=<密钥文件>` | 同上，调用真实模型 |
| `make replay-down` | 停止并删除容器与数据卷 |
| `make e2e` | 从空数据库以 `scripted` 模式起回放形态，对启动时现跑的调查与测试中新触发的调查跑 Playwright 端到端测试；宽屏与窄屏、浅色与深色的截图写到 `frontend/e2e/screenshots/` |

演示账号是 `demo`，口令由 `make replay`（与 `make e2e`）每次随机生成，启动后打印，并保存在被忽略的 `.demo/demo-password`；仓里没有默认口令。每次启动也换会话签名密钥，上一次的登录随之失效。查看调查不需要登录；手动触发调查、追问、对修复建议确认或驳回需要登录，决定只记录，不执行任何操作，每条建议只能决定一次。

追问的回答先经证据门校验，通过后才分段推送给页面，校验期间只推送进度；回放（`recorded`）模式下只能按顺序提问用例 `questions.yaml` 里录制过的问题。后端重启时正在进行的调查标为中断，不会续跑；告警再次送达或手动触发会新建调查。

前端开发：`cd frontend && npm ci && npm run dev`，开发服务器把 `/api` 代理到 `BIANQUE_BACKEND_URL`（默认 `http://127.0.0.1:8000`）；后端用 `uvicorn --factory bianque.api.app:create_app` 启动。`make check` 包含前端的 lint、类型检查、单元测试与构建。

## MCP 服务

扁鹊把自己的只读工具经 MCP（Streamable HTTP）提供给 Claude Code 等客户端，地址是 `/mcp`。外部客户端与扁鹊的取证子 Agent 用的是同一个工具注册表与执行器，超时、重试、脱敏与调用记录都相同；MCP 调用不属于任何调查，不产生证据。

| 工具 | 作用 |
| --- | --- |
| `k8s_list_pods`、`k8s_get_pod_status`、`k8s_list_events`、`k8s_get_deployment` | 读 Kubernetes 现状 |
| `logs_get_container_logs` | 读容器当前或上一次的日志 |
| `change_get_rollout_history`、`change_list_commits`、`change_show_commit` | 读 Deployment 修订历史与清单仓的提交 |
| `knowledge_search` | 检索运维手册，结果是参考资料，不是证据 |
| `investigation_list`、`investigation_get_report` | 列出一个命名空间最近的调查，读取一次调查的报告 |

每个客户端持有一个 Bearer 令牌，令牌绑定允许的工具与命名空间（`BIANQUE_MCP_CLIENTS`，只配置令牌的 SHA-256）。没有令牌或令牌不对返回 401；令牌不允许的工具不出现在工具列表里，直接调用返回错误；命名空间范围作用于所有带 `namespace` 参数的工具。被拒的调用与失败的调用都写调用记录，记录调用方；失败时客户端收到结构化错误 `{"error": {"code": "timeout", "message": "..."}}`。调用记录写不进数据库这类意外只写服务端日志，客户端收到 `failed`，不含异常内容。

`make replay`（与 `make e2e`）每次启动生成一个随机令牌，保存在被忽略的 `.demo/mcp-token`，并配置客户端 `claude-code`：全部工具，命名空间 `demo-s1`。在仓库根目录用下面的命令把它加入 Claude Code；命令从文件读出令牌，命令本身不含令牌，Claude Code 会把它保存在本机配置里：

```bash
claude mcp add --transport http bianque http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer $(cat .demo/mcp-token)"
```

之后在 Claude Code 里可以问「用 bianque 列出 demo-s1 最近的调查，读最近一次的报告，根因是什么」。重新启动回放形态会换令牌，要重新加一次。回放形态下工具读证据包（默认用例 S1-a，`BIANQUE_PACK_CASE` 可改），`knowledge_search` 的自由查询只走全文检索。

演示形态下地址是 `http://127.0.0.1:8180/mcp`，令牌在 `.demo/kind/mcp-token`，`make demo` 完成时打印对应的命令；工具读的是 kind 里的真实集群与清单仓。

## 证据包

证据包是故障发生时整个演示命名空间（Pod、事件、Deployment、ReplicaSet、全部容器日志）与清单 Git 仓的快照，外加触发调查的那条告警。扁鹊的每个取证工具都有两种后端：读真实集群与清单仓的真实后端，和读证据包的回放后端；两者接口相同，同一查询返回相同的内容。所以没有 kind 集群也能用证据包重跑调查与评测，回放时把录制时间当作当前时间。

录制时去掉对象的 `managedFields` 与 `last-applied-configuration` 注解，私有地址泛化为 `10.244.x.x` 的形式，写完再用脱敏规则检查全部文件；清单仓用 `git fast-export` 文本保存。目录结构：

```text
eval/cases/s1-a/pack/
├── meta.json          格式版本、用例、命名空间、录制时间、组件版本
├── alert.json         Alertmanager 的 webhook 请求体
├── k8s/               pods.json、events.json、deployments.json、replicasets.json
├── logs/              <pod>/<container>.log 与 <pod>/<container>.previous.log
├── repo.fast-export   清单仓
└── faults.yaml        可选：回放时注入的故障，例如日志工具超时
```

## 许可证

[Apache License 2.0](LICENSE)
