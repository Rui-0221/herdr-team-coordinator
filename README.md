# Herdr 团队协调器 (herdr-team-coordinator)

面向 Windows 平台 [Herdr App](https://github.com/herdrdev/herdr-app) 与 `hc`（`herdr-core >= 0.1.21`）的多 Agent 团队协调器 Mod。通过主控会话拆解目标、协调派工，也可直接与指定成员沟通，共用同一份团队对话、任务记录与验收条件。协调器负责写锁仲裁、版本变更与独立审查，本地恢复面板用于人工核对和高风险审批。

支持协调 **Codex**、**Antigravity (`agy`)** 与 **DSH (`dsh-cli` / `dsh-tui` / `dsh`)** 会话，同时兼容**存量交互终端模式（`legacy`）**与 **Windows 受管原生运行模式（`full_auto` / `approval`）**。

---

## 核心特性与架构

### 双轨执行架构：受管原生回合与存量交互终端

面板与成员进程统一通过后台单实例守护进程 `Coordinator` 交换消息和状态。团队对话、任务、用户直达要求与协议状态均保存在其独占的 SQLite 数据库（`control/coordinator.sqlite3`）中。

```text
存量交互模式 (legacy):
[Agent 终端] ──(1) coord.py 经 Windows 命名管道 (member)──▶ [Coordinator + SQLite]
                                                                │
                          (2) hc events 监听 + hc send (<95B) ◀─┤ (短门铃唤醒)
                                                                ▼
[Agent 终端] ◀──(3) coord status 拉取任务与对话 ────────────────┘

Windows 受管原生模式 (full_auto / approval):
[native_worker.py] ◀── native-next / native-finish (member 管道) ──▶ [Coordinator + SQLite]
       │                                                                      ▲
       ├── Codex ──▶ AppContainer (windows_isolation.py)                      │
       │                └─▶ codex.exe exec --json                             │
       └── DSH / Antigravity ──▶ 低完整性令牌 + 私有非交互桌面 (host_native.py) │
                        ├─▶ dsh-cli (node.exe --profile headless --json)      │
                        ├─▶ agy.exe acp (native_acp.py JSON-RPC stdio)        │
                        └─▶ coord_runtime.py ──▶ 127.0.0.1 鉴权中继 ──────────┘
                                                       │
                                      stdout/stderr ──▶ control/native-evidence/ ──▶ 角色详情页实时回放
```

- **数据面（结构化状态与完整正文）**：成员通过 `coord.py` 经 Windows 命名管道（`member` 通道）提交指令并从 `coord status` 读取 `candidates`、`inbox`、近期团队对话 `conversation` 与当前角色的 `pending_messages`。每次请求校验 `binding + token`、`HC_TERMINAL_ID`、`hc` 实时终端 `instance` 以及任务轮次 `--task / --ticket`，并按目标 `revision` 做幂等防重。
- **空闲会话唤醒**。后台通过 `hc events` 监测终端状态，仅向已绑定、活跃运行中处于 `idle` / `done` 且未发现菜单或草稿的终端推送短提示。投递使用 `SendResult.started` 和发送前轮次核对。若输入框里的提示完整匹配，后台在核对实例和轮次后可补一次回车。成员首次鉴权接入后，主控会收到就绪通知。
- **共享对话与按角色派工**。面板消息和经 `say` 登记的终端消息统一写入 `team_messages`，记录收件角色、引用关系、任务与处理结果。主控可通过 `coord status` 读取成员职责、交付物与当前任务，用 `dispatch --source` 关联原消息并防止重复派工。成员已有执行项时，新任务按轮次排队。
- **任务要求版本化与交接确认**。`amend-task` 更新单项任务的执行要求或验收条件，递增 `task_version` 并保留任务 ID。成员用 `ack-task` 确认新版后可申请写入或提交交接。已交接任务重新开启执行轮次时，已有审查及对应验收问题失效。
- **排他写锁仲裁与阻塞追踪**：`workflow_grants` 与 FIFO `workflow_waits` 确保同一工作树或共享 Git `refs` 同时仅允许一个任务写入；支持通过 `blocked` 登记结构化阻塞依赖并在释放/交接后自动解除并通知等待方。
- **强制独立只读审查与字节级指纹核验**：`_independent` 禁止使用任何曾持有写锁或提交过交接的会话执行审查；`review-start`、`review-submit`、`ask --purpose acceptance` 与 `answer` 均通过 `artifacts.snapshot()` 重新比对路径覆盖与 SHA-256 指纹，文件内容一旦变化即刻作废旧审查和旧问题。
- **目标版本化、紧急暂停与崩溃恢复**：`change-goal` 递增目标 `revision` 并拦截旧版写入，由主控通过 `resolve-revision`（`reuse` / `adjust` / `cancel`）逐项处置旧任务；`pause --emergency` 可请求打断写入终端并冻结后续写权限；会话退出或后台重启后，可在本地面板重连/重绑并提交停写证据回收旧写入资格。
- **权限分层与 Windows 沙箱兼容**：`control/` 目录采用严格属主 ACL 隔离，阻断沙箱子成员直读数据库或连接 `local` 管理管道；同时在 `root` 属主哈希下为 `member` 命名管道及安装目录授权 `CodexSandboxUsers`，保证 Codex `--sandbox read-only` 成员可通过 `coord.py` 正常协作。高风险操作（`delete`、`permission`、`provider`）与可选本地验收必须经 Windows 原生确认框批准。

---

## 仓库结构

| 文件 | 说明 |
| --- | --- |
| `mod.json` | Herdr 浮层 Mod 元数据定义（入口 `main.js` 与窗口尺寸） |
| `main.js` | GPUI 前端控制与恢复面板（团队对话、逐角色详情页与实时回放、双通道转向、投递核对、高风险决定、写入回收、团队与权限档位配置） |
| `coord.py` | 成员/本地 CLI 客户端与 `serve` 守护服务入口，支持 `--json -` 标准输入传参、请求防重暂存日志与凭据传递 |
| `coordinator.py` | 协调器核心（SQLite 事务与迁移、受管原生回合调度 `native_turn`、逐角色详情/输出接口、终端监控、`hc events` 唤醒、本地审批接口） |
| `native_worker.py` | 受管原生引擎工作循环（轮询 `native-next`、按引擎分发 AppContainer 或宿主低完整性运行、提交 `native-finish` 回执） |
| `host_native.py` | Windows 宿主低完整性引擎运行器（为 DSH / Antigravity 准备受保护依赖快照、凭据桥接与单回合 `127.0.0.1` 鉴权中继服务） |
| `native_acp.py` | Antigravity ACP（stdio JSON-RPC 2.0）回合客户端，处理会话初始化、模式/模型设置与权限回调 |
| `native_guest.py` | 隔离角色侧受限 `coord` 客户端（`coord_runtime.py`）与 AppContainer 内置工作者入口 |
| `windows_isolation.py` | Win32 原生隔离层（AppContainer 沙箱、Low Integrity 受限令牌、私有非交互窗口站/桌面、`WinSta0` 防护与 Job Object） |
| `role_observability.py` | 逐角色原生回合持久化（`role_turns`）、JSONL 事件流解析、字节游标增量分页、敏感凭据脱敏与跨回合上下文摘要 |
| `team_chat.py` | SQLite 团队对话记录（角色收件、参与者筛选、引用与任务关联、历史分页、消息防重与处理结果） |
| `workflow.py` | 工作流状态机（任务级要求版本、用户直达指令 `workflow_user_directives`、写锁 FIFO 仲裁、停写交接、目标变更、独立审查快照） |
| `artifacts.py` | Git 工作树识别、范围重叠检测与基于 `git diff` / `ls-files` / 文件哈希的确定性成果快照 |
| `platform_runtime.py` | 本机 CLI（含 `dsh-cli` / `dsh-tui`）与模型探测、`Hc` 适配封装、Windows 命名管道服务端/客户端与 ACL 安全描述符管理 |
| `config_store.py` | 角色、团队、执行权限档位（`legacy` / `approval` / `full_auto`）与工具路径配置校验、持久化及旧版配置导入 |
| `supervisor.py` | Windows 无窗口守护进程与 Job Object 监管重启控制 |
| `common.py` | 公共异常 `DomainError`、时间戳、规范化 JSON 序列化与默认数据目录定位 |
| `test_coordinator.py` | 共享对话、任务变更、双通道转向、角色详情回放、协调器调度、自动投递及实管道 CLI 集成测试 |
| `test_native_runtime.py` | 原生引擎参数、Windows 隔离边界、ACP/DSH 回合回执与 `role_observability` 单元测试 |
| `test_workflow.py` | 工作流写锁、阻塞、快照失效、版本复用、用户直达要求保留及验收状态机单测 |
| `test_platform_runtime.py` | Windows 命名管道隔离、并发锁、`Hc.send` 回执、`»` 折行提示识别与 `Hc.launch` 启动参数单测 |
| `README.md` | 项目说明、架构图解、安装使用指南、权限边界与版本更新文档 |

---

## 运行环境与安装

### 环境要求
- Windows 10 / 11
- Python 3.10 或更高版本
- 本机已安装 **Herdr App**（`herdr-app.exe`）与 **Herdr Core**（`hc.exe >= 0.1.21`）
- 至少安装一个支持的 Agent CLI：`codex`、`agy`（Antigravity）或 `dsh` / `dst`

### 安装与打开

1. 在 Herdr App 的 `mods` 目录下创建 `herdr-team-coordinator`。默认路径为 `%USERPROFILE%\.config\herdr-app\mods\herdr-team-coordinator\`。设置了 `HERDR_APP_HOME` 时，使用该目录下的 `mods`。
2. 将仓库中的 `mod.json`、`main.js` 以及全部运行时 `.py` 模块（`coord.py`、`coordinator.py`、`supervisor.py`、`common.py`、`config_store.py`、`platform_runtime.py`、`team_chat.py`、`workflow.py`、`artifacts.py`、`native_worker.py`、`host_native.py`、`native_acp.py`、`native_guest.py`、`windows_isolation.py`、`role_observability.py`）复制到该目录。升级前，先用已安装的 `coord.py local stop` 停止旧守护进程，再覆盖源码。
3. 如需指定解释器，在 Mod 目录手动创建 `python-path.json`，内容形如 `{"python":"C:/Python314/python.exe"}`。默认依次尝试 `py.exe -3`、`python.exe` 和 `python3.exe`。
4. 用 `herdr-app check <Mod 目录>` 检查文件，再运行 `herdr-app do mod.open --name herdr-team-coordinator` 打开面板。

如果 Codex 的只读沙箱无法读取 Mod 目录，可为本机 `CodexSandboxUsers` 组授予 Mod 目录及其文件的读取和执行权限。`control/` 的访问权限由协调器单独管理。

默认运行数据位于 `%USERPROFILE%\HerdrTeamCoordinator\`：
- `control\coordinator.sqlite3`：中央状态数据库（严格属主隔离）
- `requests\<binding_id>\`：可写成员的本地待提交请求暂存目录
- `service.log`：守护与监管进程日志

初始配置包含 `lead`（主控）、`developer`（开发）与 `reviewer`（独立审查）。所有角色默认处于协议只读状态，需在面板「团队设置」启用开发者的 `write_allowed` 并经 Windows 本地确认后方可在后续运行申请写入资格。也可通过面板「预览导入」从 `%USERPROFILE%\HerdrTeamManager\config.json` 导入旧团队及模型配置（写入权限仍须逐项确认）。

---

## 使用流程与 `coord` 命令参考

### 启动一次团队协作
1. 打开面板（`herdr-app do mod.open --name herdr-team-coordinator`），在「启动团队」选择**独立项目工作目录**、团队配置及验收方式（`chat` 或 `local`）。
2. 选择已有终端会话或由协调器自动新建会话；手工绑定已有会话需将对应角色的接入指令粘贴至该终端运行一次 `coord status`。
3. 首次在新项目目录拉起 Codex / Antigravity 交互会话时，若 CLI 弹出一次性「信任此文件夹」确认，请在对应终端标签页确认一次。
4. 在运行页「团队对话」中向主控提出目标或补充条件，也可点选负责该任务的成员。通过「引用回复」沿用已有消息及其关联任务。
5. 最终验收通过后，本轮全部活跃绑定会自动解绑（`active = 0`），终端可立即复用于下一轮任务。

### 与主控或指定成员对话

运行页的「团队对话」默认发给主控。点选成员后，消息进入该成员的待处理列表。「引用回复」会带上原消息及其任务，上下翻阅历史不会改变任务状态。用户也可以打开原生终端直接说话。接入提示要求 Agent 用 `say` 登记影响工作的原话，再用返回的消息 ID 处理。各角色都能读到这段团队记录。

主控根据 `status` 中的职责、交付物和当前任务选择执行者。新任务通过 `dispatch --source 消息ID` 派发；忙碌成员的新任务按轮次排队，前一个任务交接或撤回后自动开放接单。面对“刚才那个也要支持键盘”，主控或任务所属成员可以对同一任务追加要求：

```text
coord amend-task --task-id ID --version 1 --source 消息ID --instructions "支持键盘操作" --completion "Tab 可聚焦，Enter 可导出"
coord ack-task --task ID --ticket 凭据 --version 2
coord handoff --task ID --ticket 凭据 --version 2 --report "已验证键盘操作" --paths 导出文件
```

`task_version` 表示任务要求版本，`ack_version` 表示成员已确认的版本。旧任务默认从 1 开始；已修改的任务在申请写入和交接时均须带上已确认的版本。成员仍可随时用 `yield` 交还写入资格。已交接任务再次调整时，保留任务 ID 和旧交接记录，开启新执行轮次，已有审查和依赖它的验收问题随之失效。主控使用 `change-goal` 处理影响整轮运行的目标变化。

`say --source 消息ID --text 答复` 保存答复；增加 `--to 角色ID` 可将问题转给另一个角色。状态中包含最近对话和本人待处理的消息；`history --before 序号` 可查更早记录。`history --task-id ID --version N` 可复核指定版本的执行要求和验收条件。聊天内容中的批准仍须经过相应的既有决定流程。

### 常用 `coord` 命令

> 运行 `coord help` 可查看最新格式。在未配写入权限的只读沙箱中调用任务命令时，请在命令中附带 `coord status` 返回的 `--task` 与 `--ticket`。

| 阶段 | 命令与核对要点 |
| --- | --- |
| 查询状态 / 请求 | `coord status`（查询近期对话、待处理消息、执行项与写锁）、`coord requests`（查询历史请求编号及入库结果） |
| 对话 / 历史 | `coord say --text ... [--to 角色ID] [--source 消息ID]` 登记消息、答复或转交，`coord history [--before 序号]` 分页查询 |
| 调整单项任务 | `coord amend-task --task-id ID --version N [--instructions ...] [--completion ...]` 更新要求，成员使用 `coord ack-task --task ID --ticket 凭据 --version N` 确认当前版本 |
| 派工 / 撤回 | 主控使用 `coord dispatch --role <id> --title ... --instructions ... --completion ... --mode read\|write [--optional]`；派错用 `coord cancel-task --task-id ID --reason ...` |
| 接单与受阻 | 成员使用 `coord accept --task ID --ticket 凭据`；遇依赖阻塞执行 `coord blocked --reason ... [--depends-on-role \| --depends-on-task \| --depends-on-resource]` |
| 申请写锁与交棒 | `coord request-write [--path 目录] [--repository-refs]`；停止写入后用 `coord yield`（交回资格但保留未完任务）或 `coord handoff --paths ... --json '{"evidence":...}'`（正式交接） |
| 暂停与恢复 | `coord pause --reason ... [--emergency]` 冻结写入并按需请求打断写入终端；`coord resume [--task-id ID] [--json '{"evidence":...}']` 核对现场后恢复 |
| 变更团队目标 | `coord change-goal --goal "新目标"` 递增目标版本；主控用 `coord resolve-revision --task-id ID --decision reuse\|adjust\|cancel --carried ...` 处理旧任务 |
| 独立审查 | `coord review-start --role-id reviewer --paths ...` 对目标版本拍快照；审查员执行 `coord review-submit --review-id ID --verdict pass\|fail --report ...` |
| 决定与验收 | `coord ask --purpose integration\|acceptance\|delete\|permission\|provider --text ...`；聊天验收下主控核实用户明确同意后执行 `coord answer --question-id ID --response yes\|no` |

---

## 投递、权限与安全边界说明

- 团队成员可在设置中逐角色选择「受限沙箱」或「Windows 隔离 · 完全放权」。显式选择后使用 Windows 原生驱动器。存量配置保留交互终端传输，已运行的交互终端不能改绑为隔离角色。
- 隔离角色由 Herdr Core 启动监控进程。驱动器领取 SQLite 出站记录，通过引擎原生标准输入提交回合，记录引擎回执并更新 OSC 7501 状态。项目、受控角色客户端和引擎运行时是显式授权范围。角色无法通过协调器桥接调用本地管理动作。
- 新建受管运行会在确认窗口披露项目权限变更。有写入角色时项目目录使用低完整性标签，任务写入资格由 `coord request-write` 仲裁。
- DSH 和 Antigravity 通过保留当前登录会话的低完整性受限令牌启动。协调器将插件与控制数据标为禁止低完整性读写，受管角色的限制 SID 无法打开交互窗口站。原生进程由关闭即回收的 Job Object 管理，角色只能通过每回合的鉴权接口请求协调器操作。Codex 仍使用 AppContainer 驱动器。
- 完全放权时使用 Codex `--dangerously-bypass-approvals-and-sandbox`、DSH `danger-full-access` / `never`、Antigravity `--dangerously-skip-permissions`。受限档位传入相应 CLI 的沙箱和审批参数。受管驱动器尚未接入引擎的逐项审批回调。协调器自身的高风险操作由本地面板确认。
- DSH 和 Antigravity 保留宿主 `HOME`、`USERPROFILE`、`APPDATA` 和 `LOCALAPPDATA`。Antigravity 使用已安装的 `agy.exe acp` 与当前登录会话的 Credential Manager。DSH 优先解析 `%LOCALAPPDATA%/dsh-cli/dsh.cmd`，兼容调用同一安装的 `dsh-tui.cmd` 包装器。交互入口用 `dsh-tui` profile，协调器使用 `headless` profile，并从原有 `DSH_HOME` 读取 `dsh-tui` 的模型覆盖与 `.credentials.yaml`。两个引擎的临时会话数据置于各自运行目录，协调器将真实 CLI 的命令行、PID 与原始回执记在受保护的控制目录。

- 存量终端默认由本地面板确认后投递。换绑时可启用「配对后自动推送」，生效前须在对应会话运行接入指令完成鉴权。暂停运行会保留有效的待办，恢复后可继续投递。成员通过 `status` 读取通知后，后台会核销对应的唤醒记录，业务操作仍由 `accept`、`ack-task` 或回复登记。真不确定的结果可在恢复面板核对。
- 当受管启动无法核实、启动失败或原终端已离线时，可从恢复面板选择「创建新的受管会话」。后台先核对原实例、启动标签及未归还的写入资格，再轮换角色凭据。关联执行项需凭新票据重新接单。暂停中的运行恢复后才会启动新会话。切换 Codex 登录由用户在本机完成，协调器接续结构化任务和交接。
- 如果后台状态显示「依附启动进程」，Windows Job 拒绝了脱离。应从 Herdr 面板启动后台，隔离的命令运行器退出时可能同时关闭其 Job。后台恢复时按启动标签接回 Core 已创建的会话。
- **健康追踪与本地恢复**：主控或任一活跃成员异常退出或阻塞时会被实时记录与通知；主控不可用时暂停派工与协调决定。在恢复页重连或重绑同一 CLI 类型的有效会话，并逐项提交停写证据后可安全回收原写入资格。
- **高风险审批边界**：涉及 `delete`、`permission`、`provider` 的请求必须在 `--json` 中提供 `scope`（`target` / `extent` / `impact`），且只能通过本地面板 + Windows 原生弹窗登记同意决定；协调器只登记删除审批，自身不执行任何删除动作。
- Codex 受限沙箱档位按角色写入资格选择 `read-only` 或 `workspace-write`。工作树级排他写入由 `coord request-write` 仲裁。Windows 项目目录需要让协调器属主和审查者能读取沙箱创建的成果文件。存量交互模式中的 DSH 通过绑定已有会话接入。

## 角色详情与对话

- 在「运行与恢复 → 会话」打开任一角色。角色页显示任务正文与验收条件、版本和确认状态、写锁与交接、该角色参与的消息，以及当前和最近的引擎回合。原生回合的提示与输出会持久化。面板按游标读取最近输出，隐藏常见凭据与引擎的内部推理。存量交互角色显示当前绑定的终端画面。
- 团队对话默认把消息发给主控。主控可用消息编号作为 `--source` 派工、调整任务、变更目标或回复。角色页的「发送消息」直接进入该角色信箱。忙碌时消息可以在成员主动查询状态时看到，原生回合完成后也可通过 Outbox 唤醒该角色。
- 「记录为任务要求」使用面板显示的版本作为前提，同时保存用户原文并递增任务版本。主控和成员可以整理普通任务说明，用户要求会继续显示并随明确沿用的任务一起保留。成员交接前必须确认最新版。

---

## 本地开发自检与后台控制

```powershell
# 运行全部单元测试与真实命名管道 CLI 集成测试（-B 避免生成 __pycache__）
python -B -X utf8 -m unittest discover -s . -v

# 检查已复制到默认安装目录的 Mod
$mod = Join-Path $env:USERPROFILE '.config\herdr-app\mods\herdr-team-coordinator'
herdr-app check $mod

# 管理本机协调器后台进程（可追加 --root <目录> 指向独立测试数据目录）
$coord = Join-Path $mod 'coord.py'
python -B -X utf8 $coord local status
python -B -X utf8 $coord local start_daemon
python -B -X utf8 $coord local stop
python -B -X utf8 $coord local restart
```

`herdr-app check` 仅检查 Mod 加载，`coord.py local status` 查询后台状态。守护进程空闲 5 分钟且无待处理任务/未完投递后自动有序退出，再次打开面板或收到有效 `coord` 请求时按需唤醒。

---

## 版本更新记录

### v0.2.0 (2026-11-22)

- **Windows 受管原生运行时与隔离架构**：新增 `native_worker.py`、`host_native.py`、`native_acp.py`、`native_guest.py` 与 `windows_isolation.py`，支持以 `codex exec --json`、`dsh-cli` (`headless --json`) 及 `agy.exe acp` 直接驱动任务回合，避免 ConPTY 输入框提示滞留。Codex 运行于 AppContainer，DSH 与 Antigravity 通过保留宿主凭据的 Low Integrity 受限令牌与私有非交互桌面运行，统一经 `127.0.0.1` 鉴权中继调用 `coord`。
- **可配置执行权限档位**：在 `config_store.py` 与前端面板支持按角色选择 `legacy`、`approval` 或 `full_auto` 模式。
- **逐角色详情页与实时可观测性**：新增 `role_observability.py` 及 `role-inspector` / `role-output` 接口，在 `main.js` 支持点击任意角色查看当前/历史任务版本、写锁状态、回合提示与脱敏后的实时工具/输出流。
- **双通道任务转向**：新增 `steer-task` 与 `workflow_user_directives` 表，支持通过主控统筹调整或直接在角色详情页「记录为任务要求」并递增任务版本；用户原文在后续主控修订或任务承接时保持保留。
- **调度与状态机修复**：修复 `»` 及窄终端折行提示识别、`stale_delivery` 结清竞态、`ack-task` / `chat_id` 核销、普通暂停保留写锁等待队列、`dsh-cli` / `dsh-tui.cmd` 路径探测与 `--json -` 标准输入传参。
- **已知边界**：原生模式下的 `approval` 档位尚未在面板接入逐项授权交互回调（需要逐项人工审批请选用 `legacy` 存量交互模式）；桌面客户端真实窗口内的角色详情页点击布局仍待进一步人工视觉确认。
