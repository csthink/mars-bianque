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
| `make demo-down` | 删除 kind 集群；`.demo/` 保留，要从头开始就另外删除 `.demo/` |

注入后 Pod 反复重启，约 1 分钟内（实测 48 至 76 秒）Alertmanager 出现一条 `BianquePodRestarting` 告警；恢复后 1 至 2 分钟内告警解除。所有命令都指定 `kind-bianque-demo` 这个 kubectl context，建集群后会切回原来的当前 context，不会改动其他集群。

## 许可证

[Apache License 2.0](LICENSE)
