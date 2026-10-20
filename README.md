# 扁鹊 Bianque

Bianque is an alert-driven root cause analysis agent for Kubernetes: when an alert fires, it plans an investigation, collects evidence with read-only tools, and writes a report in which every conclusion cites its evidence. It is under development.

扁鹊是告警驱动的 Kubernetes 根因分析 Agent：告警触发后规划调查、用只读工具取证，生成每条结论都引用证据的报告。开发中。

## 演示环境

演示环境是一个本机 kind 集群，里面有演示应用 `s1-app`、Prometheus、Alertmanager 与 kube-state-metrics。演示应用的清单放在本机的一个 Git 仓里，故障都通过提交清单引入。扁鹊自身的部署还在开发中。

前置工具与验证过的版本：

| 工具 | 版本 |
| --- | --- |
| Docker | 29.8 |
| kind | 0.33.0 |
| kubectl | 1.37 |
| Helm | 4.3.1 |
| jq | 任意版本（`make demo-alerts` 使用） |

演示环境起好后，kind 节点容器（集群里的全部 Pod 都在其中）实测占用约 1.0 GiB 内存，注入故障期间约 1.3 GiB。

| 命令 | 作用 |
| --- | --- |
| `make demo` | 创建 kind 集群 `bianque-demo`，在 `.demo/manifests` 初始化清单仓，构建并载入演示应用镜像，安装 Prometheus 与 Alertmanager，部署演示应用。重复执行不会重建已有的部分 |
| `make inject-s1 CASE=a` | 注入故障：`a` 把 `APP_MODE` 改成非法值；`b` 把内存限制改小；`c` 把存活检查指向不存在的路径；`e` 先做 `a`，再提交一次无关的改动 |
| `make restore-s1` | 回退注入的提交，等待演示应用恢复 |
| `make demo-alerts` | 列出 Alertmanager 当前的告警 |
| `make record-pack CASE=a` | 录制一个用例的证据包（`a`、`b`、`c`、`e`）：把 `demo-s1` 重置到干净的基线，注入故障，取 Alertmanager 发出的第一条告警，把命名空间与清单仓的快照写到 `eval/cases/s1-<用例>/pack/`，跑一遍真实集群上的集成测试，最后恢复 |
| `make derive-packs` | 从 S1-a 的证据包派生 S1-d（回放时日志工具超时）与 S1-f（只留告警与 Pod） |
| `make demo-down` | 删除 kind 集群；`.demo/` 保留，要从头开始就另外删除 `.demo/` |

注入后 Pod 反复重启，约 1 分钟内（实测 48 至 76 秒）Alertmanager 出现一条 `BianquePodRestarting` 告警；恢复后 1 至 2 分钟内告警解除。所有命令都指定 `kind-bianque-demo` 这个 kubectl context，建集群后会切回原来的当前 context，不会改动其他集群。

## 回放形态

回放形态不需要 kind 集群：Docker Compose 起数据库、后端与前端（nginx 提供页面，并把 `/api` 代理到后端，页面与接口同源）。后端启动后导入 `knowledge/runbooks/` 的运维手册，并对 `eval/cases/` 下每个有证据包的用例各跑一次调查；已经跑过的用例不再重跑，`make replay-down` 删除数据卷后重新开始。

模型模式由 `LLM_MODE` 选择：

- `recorded`（默认）：用 `eval/recordings/` 里录制的真实模型响应回放，不需要模型密钥。全部用例的录制还在制作中，没有录制时调查以失败结束，原因是查不到录制。
- `scripted`：规则响应器代替模型，用于测试。
- `live`：调用真实模型，追问可以自由提问。`LLM_API_KEY_FILE` 指向保存密钥的文件，密钥不进环境变量与镜像；模型默认 DeepSeek `deepseek-v4-pro`，可用 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_EXTRA_BODY` 改。启动前把本地嵌入模型下载到模型卷，运行时不访问外部的嵌入服务。

| 命令 | 作用 |
| --- | --- |
| `make replay` | 构建镜像并启动，健康检查通过后打印地址（默认 `http://127.0.0.1:8080`，`BIANQUE_REPLAY_PORT` 可改）与演示账号的口令。第一次运行时生成数据库口令，写进被忽略的 `.demo/replay.env` |
| `make replay LLM_MODE=scripted` | 同上，规则响应器代替模型 |
| `make replay LLM_MODE=live LLM_API_KEY_FILE=<密钥文件>` | 同上，调用真实模型 |
| `make replay-down` | 停止并删除容器与数据卷 |
| `make e2e` | 从空数据库起回放形态，加载一份 S1-a 调查夹具，跑 Playwright 端到端测试；宽屏与窄屏、浅色与深色的截图写到 `frontend/e2e/screenshots/` |

演示账号是 `demo`，口令由 `make replay`（与 `make e2e`）每次随机生成，启动后打印，并保存在被忽略的 `.demo/demo-password`；仓里没有默认口令。每次启动也换会话签名密钥，上一次的登录随之失效。查看调查不需要登录；手动触发调查、追问、对修复建议确认或驳回需要登录，决定只记录，不执行任何操作，每条建议只能决定一次。

追问的回答先经证据门校验，通过后才分段推送给页面，校验期间只推送进度；回放（`recorded`）模式下只能按顺序提问用例 `questions.yaml` 里录制过的问题。后端重启时正在进行的调查标为中断，不会续跑；告警再次送达或手动触发会新建调查。

前端开发：`cd frontend && npm ci && npm run dev`，开发服务器把 `/api` 代理到 `BIANQUE_BACKEND_URL`（默认 `http://127.0.0.1:8000`）；后端用 `uvicorn --factory bianque.api.app:create_app` 启动。`make check` 包含前端的 lint、类型检查、单元测试与构建。

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
