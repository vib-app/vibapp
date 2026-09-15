# RFC 0002: Codex Harness 作为 CodeAgent 执行引擎

- Status: Proposed — 仅批准有界适配验证，不批准整体迁移
- Date: 2026-08-30
- Scope: CodeAgent 会话、事件、审批、取消和执行适配；不改变 VibApp 产品边界
- Decision owner: VibApp architecture council

## 1. 结论

VibApp 不应整体迁移到 Codex Harness，也不应 fork 或 vendor 整个 Codex Rust
workspace。Codex Harness 适合作为 `CodeAgentProvider` 的一个实现，逐步替换当前
Codex provider 的一次性 `codex exec` 调用；它不适合作为 Registry、Builder、Verifier、
AppStore、Launcher、Wasm Runtime 或 RoomHash 通讯层的替代品。

一句话边界：**VibApp 继续做产品和信任链，Codex Harness 只负责 CodeAgent 的智能体循环。**

先做一个可回退的 `CodexHarnessProvider` 适配验证。只有当它通过外部资源隔离、取消后
全后代进程静默、证据绑定和产品权限边界测试后，才可成为默认 Codex provider。当前
`local-live-containment-unavailable` 仍保持 fail-closed；本 RFC 不批准真实 provider 执行。

## 2. “Codex Harness”指什么

OpenAI 没有发布一个独立的 `codex-harness` 产品仓库。官方所称 Harness 是
[`openai/codex`](https://github.com/openai/codex) 中被 CLI、App、IDE 共同使用的开源
智能体执行层，包括上下文、推理、工具、边界、审批和会话连续性。官方给出的三种集成
入口是：一次性任务用 `codex exec`，简单程序化流程用 SDK，产品级持续智能体体验用
App Server。参见 [Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)、
[App Server 文档](https://developers.openai.com/codex/app-server) 和
[Codex 开源组件说明](https://developers.openai.com/codex/open-source)。仓库使用
Apache-2.0 许可证。

本次离线检查使用了本机 `codex-cli 0.151.0`，并从同一二进制生成 App Server v2 JSON
Schema；同时只读检查了官方仓库提交
`28327355b861ab6cc76b01c7248663eb1be440cf`。这些是本次评估的观察样本，不是 VibApp
永久锁定的版本。

## 3. 能力对比

| 能力 | 当前 VibApp | Codex Harness | 决策 |
| --- | --- | --- | --- |
| Agent 上下文与工具循环 | Codex 通过一次性 `codex exec --ephemeral`，VibApp 自管外围状态 | 完整线程、轮次、工具和执行循环 | Codex provider 内复用 Harness |
| 持续对话、恢复、分叉 | 产品侧保存需求/任务，执行器本身不连续 | 原生 `Thread / Turn / Item`，支持 start/resume/fork | 映射到现有任务和历史聊天，不替换产品记录 |
| 流式进度与中断 | 自定义进程输出和状态映射 | 原生事件流、steer、interrupt、审批请求 | 用 App Server stdio 接入 |
| 需求澄清与 NeedSpec | VibApp 专有，LLM 建议仍须确定性校验 | 不理解 VibApp 产品合同 | 保留 VibApp |
| Registry 匹配与可信解释 | 硬过滤、证据绑定、再做语义召回 | 无 VibApp Registry 语义 | 保留 VibApp |
| Builder / Verifier | 编译与验收是独立信任域 | Agent 可调用开发工具，但不是 VibApp 发布质检链 | 严禁 Harness 直接签名、安装、上架 |
| Launcher / AppStore / Runtime | 独立窗口、安装生命周期、Wasm Component host | 不提供 | 保留 VibApp |
| Web / Wasm GUI | Web 与桌面共享 GUI，浏览器经 Bridge 调可信后端 | App Server 是本地/服务端原生进程 | 网站只通过 Bridge 调用，不在浏览器内运行 Harness |
| RoomHash P2P / RTC / 应用数据空间 | VibApp 的传输和协作能力 | 不提供 | 保留 VibApp |
| 多 CodeAgent | 现有 provider 合同覆盖 Codex、Claude、OpenCode、Gemini | Codex Harness 不是其他 CLI agent 的统一兼容层 | 保留多 provider 接口，Harness 只是 Codex 后端 |
| 沙箱和审批 | 有 VibApp 同意、身份、证据与外部隔离门禁 | 有工作区沙箱和命令/文件审批 | 两层叠加，不能互相替代 |
| macOS 资源/后代进程治理 | 当前因完整 containment 不可证明而关闭 live provider | 自带进程组清理，但不能单独证明逃逸后代静默或硬资源上限 | 仍需容器/VM/受控 job 边界 |

## 4. 保持不变的产品链路

```text
用户 -> Receptionist / NeedSpec -> Registry
                              |-> 命中：安装并运行已有 VibApp
                              `-> 未命中：CodexHarnessProvider
                                             |
                                             v
                                  不可信源码交接
                                             |
                                             v
                              Builder -> Verifier -> AppStore
                                                       |
                                                       v
                                              VibApp Runtime
```

Harness 的输出仍只是“不可信源码”。它不得拥有以下权限：

- 改写 Registry 匹配事实或绕过用户同意；
- 直接使用 Builder 的签名/晋级身份；
- 编译后自行安装、发布或更新应用；
- 访问其他应用数据、RoomHash 私有空间或长期凭据；
- 修改 VibApp 的任务事实、验收结论或 Butler goal 状态。

## 5. 推荐接入点

在现有 `CodeAgentProvider` 接口后新增 `CodexHarnessProvider`，由 Orchestrator 的
`CodeAgentStage` 继续调用同一业务合同。桌面和网站只认识 VibApp 的 task、attempt、
consent 和 status，不直接依赖 Codex 协议。

建议映射：

| VibApp 字段 | App Server 字段 |
| --- | --- |
| `task_id` | 持久化业务主键；关联一个或多个 `thread_id` |
| `attempt_id` | 一次 `turn_id` 或明确记录的重试 turn |
| 历史聊天 | `thread/read` / resume 后的 `Item` 投影，但 VibApp 数据库仍是产品记录源 |
| 流式状态 | `turn/*`、`item/*` 通知投影到现有状态机 |
| 用户停止 | `turn/interrupt` 加外部执行域终止与静默验证 |
| 源码产出 | turn 完成后扫描隔离工作区，再生成现有 authority-owned source handoff |

长期入口选择 App Server 而不是只用 SDK，因为 VibApp 需要持续对话、恢复、流式事件、
中断和审批。桌面侧首选 stdio；网站经可信 Bridge/后端转发经过筛选的业务事件。WebSocket
和 App Server 的 experimental API 不进入第一版合同。

## 6. 必须保留的安全边界

App Server 文档明确说明 `thread/shellCommand` 在宿主上无沙箱执行，实验性的
`process/spawn` 也不经过 Codex 沙箱。生成出的协议还包含 VibApp provider 不需要的
`fs/*` 和独立 `command/exec` 面。因此适配器必须采用方法 allowlist，并拒绝整个
`thread/shellCommand`、`process/*`、`fs/*` 和 `command/exec` API 面；客户端也不得把
`turn/start` 的权限升级成 `dangerFullAccess` 或 `externalSandbox`。`config/*`、
`skills/config/write`、动态工具、手工 Guardian 放行及其他未在首版合同声明的方法同样
拒绝。仅关闭 `experimentalApi` 不足以形成边界，因为部分危险入口是稳定 API。

Codex 沙箱和 `turn/interrupt` 能改善控制，但不能替代外部执行容器。官方源码当前在
Unix 上用进程组治理子进程，Linux 另有 parent-death signal；macOS 上不能仅凭这些机制
证明一个主动 `setsid()`、关闭标准流或持续派生的后代已经停止。更直接地，Codex 自己的
[PTY 测试](https://github.com/openai/codex/blob/28327355b861ab6cc76b01c7248663eb1be440cf/codex-rs/utils/pty/src/tests.rs#L894-L967)
会构造一个 `setsid()` 脱组子进程，并验证普通 `terminate()` 后它仍然存活。当前
[macOS fallback](https://github.com/openai/codex/blob/28327355b861ab6cc76b01c7248663eb1be440cf/codex-rs/utils/pty/src/process_group.rs#L149-L223)
按进程组处理，不是递归的后代树收割；`turn/interrupt` 也不等价于全树静默证明。
官方还明确说明 interrupt 不会终止 background terminals。因此 `turn/interrupt` 与
`backgroundTerminals/clean` 只能当作 advisory 信号，不能当成可以释放 backend slot 或
读取源码的完成证据。
Harness 同样没有覆盖 VibApp 所需的 RSS、CPU、PID、磁盘、网络和墙钟时间全部硬上限。
因此：

1. App Server 和它启动的 agent 工具必须整体放进可销毁的外部执行域；
2. 默认禁网，工作区仅可写一次性目录，使用隔离的 `CODEX_HOME`；
3. 执行域必须限制 RSS、CPU、PID、磁盘、输出和墙钟时间；
4. 正常完成、取消、超时、Bridge 断开或 App Server 崩溃后，都先由 Harness 之外的可信
   oracle 证明执行域稳定零成员、无晚到写入；之后才能释放 slot、读取源码或生成 handoff；
5. Registry、consent、provider identity 或 schema 校验失败时，必须保持零 provider
   进程启动；
6. provider 只能产生源码交接，Builder 和 Verifier 的身份及晋级权不下放。
7. 在外部 containment 单独验收前，拒绝客户端选择 `approvalPolicy=never`；即使未来在
   已验收的无人值守容器内放开，也必须由服务器固定策略决定，不能由 GUI/任务输入升级。

## 7. 有界适配验证

### Phase 0：先解决当前资产可追溯性

当前主要 `artifacts/` 实现被 `.gitignore` 排除，尚不是由 Git 跟踪的可恢复工程基线。
在迁移执行层前，先把应保留的源文件从生成物中分类并纳入可审计版本管理，同时完成已
暴露 provider 凭据的轮换。否则所谓迁移只是在不可追溯的地基上换引擎。

### Phase 1：新增可回退 provider

1. 从受信任的 Codex 二进制启动 `codex app-server`，完成 initialize 和能力协商；
2. 从实际安装版本生成/校验 v2 schema，不永久写死单一 CLI 版本；
3. 只实现稳定的 thread/turn/item/approval 子集，关闭 experimental API，并对方法以及
   sandbox、approval、cwd、root、environment、config 等字段做双重 allowlist；
4. 将 thread/turn 身份、事件和最终源码绑定到现有 task/attempt/consent 收据；
5. 保留当前 `codex exec` 适配作为显式回退，不改变其他 provider；
6. 只使用离线 synthetic server / fixture 完成协议测试，不调用真实模型。

### Phase 2：必须通过的启用门槛

- 非法或过期 Registry/consent 输入不会启动 App Server/provider turn；
- schema 漂移、未知事件、重复/乱序事件均 fail-closed；
- 正常完成、interrupt、超时、Bridge 断开或 App Server 崩溃后，包含 `setsid()`、
  double-fork、关闭 stdio、忽略 TERM、竞态派生的后代也全部终止；
- RSS/PID/磁盘/输出/时间超限均能终止，且不会重演 Jetsam 型内存失控；
- 可信 oracle 证明稳定零成员后才释放 slot/读取源码/生成 handoff，且此后工作区不再写入；
- 重试、resume、fork 不会重复构建、混淆 attempt 或复用旧同意；
- 记录实际 Codex 可执行文件摘要、协议 schema 摘要和执行域收据；
- Harness 无编译、签名、安装、发布和 Registry 晋级权限；
- 用 2–3 个小应用与原 `codex exec` 路径做 A/B 验证后再决定默认切换。

只有这些门槛全部通过，才把 `CodexHarnessProvider` 设为 Codex 默认实现。任何一项失败，
继续保留当前 fail-closed 路径，不扩大到十应用或公开测试。

## 8. 版本策略

不采用“永远只允许一个 Codex 版本”，也不采用“任意新版本直接运行”。每次 attempt
记录实际可执行文件摘要和生成 schema 摘要；启动时做协议版本、方法 allowlist、字段和
能力预检。兼容的新版本在离线协议及 containment 回归通过后进入允许集合。这样既避免
脆弱的硬锁，也避免上游 API 变化静默改变安全边界。

## 9. 最终意见

- **整体工程基座迁移：No-Go。** 收益小于重构与上游跟随成本，也会模糊 VibApp 的产品
  和信任边界。
- **Codex CodeAgent 执行层适配：Conditional Go。** App Server 能显著减少自研会话、
  流式事件、审批和恢复协议的工作，且正好落在现有 provider 接口后方。
- **立即启用真实执行：No-Go。** Harness 本身没有消除当前 macOS 后代进程与资源隔离
  阻塞；外部 containment 仍是启用条件。

这是一项“换 CodeAgent 引擎、不换 VibApp 底盘”的渐进改造，不是重写项目。
