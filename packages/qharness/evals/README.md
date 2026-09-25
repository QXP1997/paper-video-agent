# Agent Loop 评测

第八批的可执行评测入口。复用原 ModelBackend、RunService、Planner、Actor、ToolExecutor、验证器、SQLite 账本和 Dulwich 历史，不另建生产 Agent Loop。真实任务执行只走 SRT；沙箱不可用时记录 `blocked_environment`，不执行模型生成的宿主机代码。

## 运行

在仓库根目录、已安装项目依赖的环境中执行：

```powershell
# 固定运行矩阵与配置指纹，不调用模型或执行沙箱命令
.\.venv\Scripts\python.exe -X utf8 -m evals.run --plan

# 最小真实任务试运行；规划探针用量单列
.\.venv\Scripts\python.exe -X utf8 -m evals.run --categories explicit --repeats 1 --probe-model

# 六类开发任务，每个配置独立重复三次
.\.venv\Scripts\python.exe -X utf8 -m evals.run --split dev --variants base all --repeats 3

# 3 个调度对照 + 8 个策略组合
.\.venv\Scripts\python.exe -X utf8 -m evals.run --split dev --variants react fixed_todo full_replan base layered dynamic gaps layered_dynamic layered_gaps dynamic_gaps all --repeats 3

# 冻结策略之后分别运行，结果不与开发集混合
.\.venv\Scripts\python.exe -X utf8 -m evals.run --split regression --variants base all --repeats 3
.\.venv\Scripts\python.exe -X utf8 -m evals.run --split heldout --variants base all --repeats 3
```

配置沿用 `config/model.toml`、`sandbox.toml`、`tool.toml` 和 `loop.example.toml`，前三者也有对应 CLI 路径参数。`--input-price` / `--output-price` 接受同一货币的每百万 Token 单价；缺失价格或不完整用量时费用为 null。此项是模型费用估算，不包括机器或服务账单，不能等同真实账单。

每次创建新的 `.qharness/evals/<batch_id>`，逐次保存 `report.json` 和每个 Run 的工作区、数据库、历史、snapshot、result、独立验收结果。报告 Markdown 与 JSON 汇总分开保留。退出码：0 为计划生成或全部独立验收通过，1 为存在未通过记录，2 为整体沙箱预检阻塞。报告中 `finished` 只表示评测程序运行结束，不表示所有任务成功。中断后已有逐次结果保留，尚未开始的任务不能当成失败或通过；当前没有整批断点续跑命令。

`--probe-model` 单独调用真实 Todo Planner / Stage Planner，检查结构与检查目录覆盖，不运行 Actor 或任何沙箱命令，也不计入任务成功率。它用于在 SRT 不可用时诊断模型协议。默认不执行此额外探针。

## 任务与评分

`coding-micro-v1` 包含 18 个实例：dev、regression、heldout 各六个。

| 类别 | 实际任务与触发 |
|---|---|
| explicit | 修复分页偏移、尾页与空页 |
| investigation | 定位并修复分页实现偷偷排序的问题 |
| cross_module | 修复三层整数变换管线，检查局部接口与组合结果 |
| environment | 修复错误的数据路径配置；加载器和公开数据保持不变 |
| steering | 执行中暂停，追加非法参数必须抛 ValueError 的约束，再恢复 |
| long_running | 八层管线；中途暂停并重建 RunService 后继续 |

它们是可实际执行的微型代码任务，不是生产仓库任务基准。环境场景是应用配置故障；长期场景是多步骤恢复，不能代替几小时任务、真实进程重启和跨多个上下文窗口的压力验收。三个集合共用任务族，只冻结不同数据实例；不是私有未知任务，也不证明跨仓库泛化。后续必须补入用户代表性仓库、外部依赖和更长任务后才能选生产 Profile。

每次运行拥有独立初始工作区、SQLite 数据库和文件历史。在线检查由应用生成命令；终态评分使用额外输入与完整断言，通过 SRT 标准输入提供，不把可修改的测试脚本放进工作区。评分检查受保护文件内容、真实断言和本次随机完成标记；退出码 0 但提前退出、缺少标记或日志截断不能通过。追加约束和暂停恢复必须有实际 applied 输入，不能仅凭 Hook 触发标志算执行过。

独立评分在 RunService 静止后取得同一工作区锁执行；存在未知调用时不评分。评分异常或超时保留不确定所有者标记，需应用核对，不能自动用另一个 Run 接管。评分命令不是对恶意 Python 实现的形式化防伪协议；本集合评估非对抗代码任务，SRT 负责执行隔离。真实恶意篡改评测应增加独立进程/外部行为断言。

## 对照的准确含义

| 名称 | 调度差异 |
|---|---|
| react | 一个固定 Todo / Stage，只运行原 Actor；候选停在 VERIFYING，独立评分决定是否解决。保留原角色协议，是受约束 ReAct 对照，不是论文实现复现 |
| fixed_todo | 固定分解与阶段目标，保留原三层验证及局部反馈；需要重新分解时等待，不偷偷调用 Planner |
| full_replan | 未完成 Todo 的可行动反馈强制走原 REPLAN_TODO，实际重新分解；已通过 Todo 和环境检查重试保留原规则 |
| base | 原三层执行器，三项策略关闭 |
| layered / dynamic / gaps | 单独启用分层反馈 / 动态阶段 / 缺口进展 |
| layered_dynamic / layered_gaps / dynamic_gaps / all | 对应二项或三项组合 |

`full_replan` 必须满足原 TodoPlanPatch 的真实变更及版本/证据校验，不允许仅涨版本。它不是设计文档 B2 的“每次工具调用都重新规划、只看局部结果”复现；目前比较的是失败反馈触发的强制重新分解。所有对照保留原权限、工具、预算、恢复和独立终态评分，不能据此把某个差异解释为单个学术机制的净收益。

调度顺序由固定 seed 打乱，每次重复使用新 Run。所有组读取相同模型设置和原 Loop 预算，角色重试、规划、Actor、Judge（若执行）、内部检查都由共享账本计量。独立评分为各组共同的评测开销，工具额度之外单列，耗时计入总耗时。现有运行入口没有启用语义 Judge，报告不能宣称测量了 Judge 收益。

## 指标与使用边界

- 独立验收率：实际启动的 Run 中，独立断言通过且共享预算未超限的比例。WAITING 但文件正确的情况仍另存 phase，不能当作控制器正确完成。
- 误完成：phase=completed 却被独立断言判为 fail；评分 error/未知结果单列 unverified_completion。ReAct 的 candidate 不伪装为 completed。
- 每任务 pass^2 / pass^3 按 `C(s,k)/C(n,k)` 计算，再在具备足够重复的任务间平均；不把总体平均成功率直接乘方。样本量、原始重复结果和耗时 P50/P90/P95 均保留；样本不足时为 null，不作显著性结论。
- 角色尝试数、已知 Token、未知预留、工具执行、修复次数、Task 返工、调查次数、暂停/恢复/Steering 记录均来自已有账本。没有进展报告时不把“无效调查”填成零；路由次数不等于路由正确率，后者保持 null，待人工结合 Trace 标注。
- 没有模拟用户批准或恢复未知副作用。人工介入实际为零时可记录零，待处理审批和未知调用另报，不能称“无需人工”。脚本注入的暂停/追加约束不是人工作业耗时测量。

配置、代码、任务内容、SRT 配置和模型参数有指纹；记录主机和依赖版本及 Provider 实际返回的模型名。API Key 不进入报告或指纹，端点只存哈希。模型别名仍可能被 Provider 更新，SRT 真实解释器/系统身份须随真实验收一起确认，不能把配置指纹当成完整环境镜像。

Profile 尚不选默认值。开发集用于定出候选；回归集排除误完成和恢复破坏；最后在冻结的保留集及代表性仓库比较成功率、P95、用量和人工介入。interactive 优先交互延迟，coding 比较完整正确性与返工，long_running 额外检查压缩和恢复；不因本次离线检查通过就提高预算或宣称生产参数最优。

评测器的脚本模型及评分回归位于 `tests/evaluation/test_evaluation.py`，沿用原测试 Fixture。测试中只有仓库作者提供的固定代码在临时目录执行；真实模型生成的实现始终经 SRT。
