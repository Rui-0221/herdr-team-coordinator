# Herdr 团队协调器 (herdr-team-coordinator)

面向 Windows 平台 [Herdr App](https://github.com/herdr) 与 `hc`（`herdr-core >= 0.1.21`）的多 Agent 团队协调器 Mod。在一个主控会话中完成目标拆解、派工、写锁仲裁、独立审查、版本变更与验收，并配合本地恢复面板处理人工核对和高风险审批。

支持协调 **Codex**、**Antigravity (`agy`)** 与 **DSH (`dsh` / `dst`)** 终端会话。

---

## 核心特性与架构

### 星型中枢与“数据面 / 唤醒面”分离

所有成员都不直接互相发消息，全部协议状态由后台单实例守护进程 `Coordinator` 及其独占 SQLite 数据库（`control/coordinator.sqlite3`）统一维护：

```text
[Agent A 终端] ──(1) coord.py 经 Windows 命名管道 (member)──▶ [Coordinator + SQLite]
                                                                    │
                              (2) hc events 监听 + hc send (<95B) ◀─┤ (短门铃唤醒)
                                                                    ▼
[Agent B 终端] ◀──(3) coord status 经命名管道拉取完整任务与 inbox ──┘
       │
       └──(4) 成果留在项目 Git 工作区，由 artifacts.snapshot() 做 SHA-256 指纹校验
```

- **数据面（结构化状态与完整正文）**：成员通过 `coord.py` 经 Windows 命名管道（`member` 通道）提交指令并从 `coord status` 读取完整 `candidates` 与未截断 `inbox`。每次请求校验 `binding + token`、`HC_TERMINAL_ID`、`hc` 实时终端 `instance` 以及任务轮次 `--task / --ticket`，并按目标 `revision` 做幂等防重。
- **唤醒面（空闲会话低侵入叫醒）**：后台通过 `hc events` 监测终端状态；对插件创建并绑定的会话（`created_by_plugin=1` 且子成员启用 `--no-notify`），仅在终端处于 `idle` / `done` 且画面无弹窗、无草稿阻塞时发送不超过 95 字节的短唤醒提示（避免 Windows ConPTY 长文本粘贴丢回车），并结合 `SendResult.started`、状态轮询与精确前缀核验下的单次 `hc keys enter` 兜底确认；存量/手动绑定会话则保留在本地面板点击确认后再投递。
- **排他写锁仲裁与阻塞追踪**：`workflow_grants` 与 FIFO `workflow_waits` 确保同一工作树或共享 Git `refs` 同时仅允许一个任务写入；支持通过 `blocked` 登记结构化阻塞依赖并在释放/交接后自动解除并通知等待方。
- **强制独立只读审查与字节级指纹核验**：`_independent` 禁止使用任何曾持有写锁或提交过交接的会话执行审查；`review-start`、`review-submit`、`ask --purpose acceptance` 与 `answer` 均通过 `artifacts.snapshot()` 重新比对路径覆盖与 SHA-256 指纹，文件内容一旦变化即刻作废旧审查和旧问题。
- **目标版本化、紧急暂停与崩溃恢复**：`change-goal` 递增目标 `revision` 并拦截旧版写入，由主控通过 `resolve-revision`（`reuse` / `adjust` / `cancel`）逐项处置旧任务；`pause --emergency` 可请求打断写入终端并冻结后续写权限；会话退出或后台重启后，可在本地面板重连/重绑并提交停写证据回收旧写入资格。
- **权限分层与 Windows 沙箱兼容**：`control/` 目录采用严格属主 ACL 隔离，阻断沙箱子成员直读数据库或连接 `local` 管理管道；同时在 `root` 属主哈希下为 `member` 命名管道及安装目录授权 `CodexSandboxUsers`，保证 Codex `--sandbox read-only` 成员可通过 `coord.py` 正常协作。高风险操作（`delete`、`permission`、`provider`）与可选本地验收必须经 Windows 原生确认框批准。

---

## 仓库结构

| 文件 | 说明 |
| --- | --- |
| `mod.json` | Herdr 浮层 Mod 元数据定义（入口 `main.js` 与窗口尺寸） |
| `main.js` | GPUI 前端控制与恢复面板（运行概览、投递核对、高风险决定、写入回收、团队与角色配置） |
| `install.ps1` | Windows 安装与静态检查脚本（校验 `herdr-app check`、写入 `python-path.json`、有序切换安装目录并配置沙箱读取 ACL） |
| `coord.ps1` | PowerShell 包装入口，转发参数至 `coord.py` |
| `coord.py` | 成员/本地 CLI 客户端与 `serve` 守护服务入口，包含请求防重暂存日志与凭据传递 |
| `coordinator.py` | 协调器核心（SQLite 事务与迁移、终端绑定监控、`hc events` 唤醒、屏幕阻塞检测与安全自动投递、本地审批接口） |
| `workflow.py` | 工作流状态机（写锁 FIFO 仲裁、停写交接、版本变更、独立审查快照、`ask` / `answer` 决定与验收解绑） |
| `artifacts.py` | Git 工作树识别、范围重叠检测与基于 `git diff` / `ls-files` / 文件哈希的确定性成果快照 |
| `platform_runtime.py` | 本机 CLI/模型/思考深度探测、`Hc` 适配封装、Windows 命名管道服务端/客户端与 ACL 安全描述符管理 |
| `config_store.py` | 角色、团队与工具路径配置校验、持久化及旧版 `HerdrTeamManager` 配置预览/导入 |
| `supervisor.py` | Windows 无窗口守护进程与 Job Object 监管重启控制 |
| `common.py` | 公共异常 `DomainError`、时间戳、规范化 JSON 序列化与默认数据目录定位 |
| `test_coordinator.py` | 协调器调度、自动投递、屏幕守卫、重绑复用及实管道 CLI 集成测试 |
| `test_workflow.py` | 工作流写锁、阻塞、快照失效、版本复用、高风险授权及验收状态机单测 |
| `test_platform_runtime.py` | Windows 命名管道隔离、并发锁、`Hc.send` 回执与 `Hc.launch` 启动参数单测 |
| `README.md` | 项目说明、安装使用指南与权限边界文档 |

---

## 运行环境与安装

### 环境要求
- Windows 10 / 11
- Python 3.10 或更高版本
- 本机已安装 **Herdr App**（`herdr-app.exe`）与 **Herdr Core**（`hc.exe >= 0.1.21`）
- 至少安装一个支持的 Agent CLI：`codex`、`agy`（Antigravity）或 `dsh` / `dst`

### 安装与打开

在仓库根目录执行安装脚本（安装文件将复制到 `%USERPROFILE%\.config\herdr-app\mods\herdr-team-coordinator\`，升级前会先有序停止旧守护进程并将上一版本备份至安装暂存目录）：

```powershell
.\install.ps1
herdr-app do mod.open --name herdr-team-coordinator
```

可选安装参数：
- `-Python 'C:\路径\python.exe'`：显式指定 Python 3.10+ 解释器路径。
- `-AppHome 'C:\自定义\herdr-app'`：覆盖默认 `%USERPROFILE%\.config\herdr-app`（也支持环境变量 `HERDR_APP_HOME`）。
- `-CheckOnly`：仅复制非测试运行文件到临时目录并运行 `herdr-app check`，不改动正式安装目录也不启动守护进程。

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
3. 首次在新项目目录拉起 Codex / Antigravity 交互会话时，若 CLI 弹出一次性「信任此文件夹」确认，请在对应终端标签页确认一次；随后主控会自动派工并推进整轮协作。
4. 最终验收通过后，本轮全部活跃绑定会自动解绑（`active = 0`），终端可立即复用于下一轮任务。

### 常用 `coord` 命令

> 运行 `coord help` 可查看最新格式。在未配写入权限的只读沙箱中调用任务命令时，请在命令中附带 `coord status` 返回的 `--task` 与 `--ticket`。

| 阶段 | 命令与核对要点 |
| --- | --- |
| 查询状态 / 请求 | `coord status`（查询待办、写锁与完整 `inbox`）；`coord requests`（查询历史请求编号及入库结果） |
| 派工 / 撤回 | 主控使用 `coord dispatch --role <id> --title ... --instructions ... --completion ... --mode read\|write [--optional]`；派错用 `coord cancel-task --task-id ID --reason ...` |
| 接单与受阻 | 成员使用 `coord accept --task ID --ticket 凭据`；遇依赖阻塞执行 `coord blocked --reason ... [--depends-on-role \| --depends-on-task \| --depends-on-resource]` |
| 申请写锁与交棒 | `coord request-write [--path 目录] [--repository-refs]`；停止写入后用 `coord yield`（交回资格但保留未完任务）或 `coord handoff --paths ... --json '{"evidence":...}'`（正式交接） |
| 暂停与恢复 | `coord pause --reason ... [--emergency]` 冻结写入并按需请求打断写入终端；`coord resume [--task-id ID] [--json '{"evidence":...}']` 核对现场后恢复 |
| 变更目标 | `coord change-goal --goal "新目标"` 递增目标版本；主控用 `coord resolve-revision --task-id ID --decision reuse\|adjust\|cancel --carried ...` 处理旧任务 |
| 独立审查 | `coord review-start --role-id reviewer --paths ...` 对目标版本拍快照；审查员执行 `coord review-submit --review-id ID --verdict pass\|fail --report ...` |
| 决定与验收 | `coord ask --purpose integration\|acceptance\|delete\|permission\|provider --text ...`；聊天验收下主控核实用户明确同意后执行 `coord answer --question-id ID --response yes\|no` |

---

## 投递、权限与安全边界说明

- **自动投递 vs 人工确认**：插件新建并绑定的独立会话启用子成员 `--no-notify`，在 `idle` / `done` 且画面无待选菜单/未提交草稿时自动投递 `<= 95` 字节唤醒通知，完整消息通过命名管道 `inbox` 读取；人工绑定的存量终端需在本地面板核对草稿并确认后才推送，投递超时或结果 `unknown` 绝不自动盲目重发。
- **健康追踪与本地恢复**：主控或任一活跃成员异常退出或阻塞时会被实时记录与通知；主控不可用时暂停派工与协调决定。在恢复页重连或重绑同一 CLI 类型的有效会话，并逐项提交停写证据后可安全回收原写入资格。
- **高风险审批边界**：涉及 `delete`、`permission`、`provider` 的请求必须在 `--json` 中提供 `scope`（`target` / `extent` / `impact`），且只能通过本地面板 + Windows 原生弹窗登记同意决定；协调器只登记删除审批，自身不执行任何删除动作。
- **写入仲裁边界与已知限制**：对于开启系统沙箱的只读角色，受 OS 沙箱约束无法写工作区；同一 Windows 用户下的非沙箱可写进程拥有用户实际文件系统权限，排他写锁依赖 `coord request-write` 协议配合。DSH 当前通过绑定已有会话接入；多账号额度分类与自动切号暂未接入。

---

## 本地开发自检与后台控制

```powershell
# 运行全部 36 项单元测试与真实命名管道 CLI 集成测试（-B 避免生成 __pycache__）
python -B -X utf8 -m unittest discover -s . -v

# 仅执行安装前打包与宿主静态检查（不修改已安装目录）
.\install.ps1 -CheckOnly
herdr-app check "%USERPROFILE%\.config\herdr-app\mods\herdr-team-coordinator"

# 管理本机协调器后台进程（可追加 --root <目录> 指向独立测试数据目录）
$coord = Join-Path $env:USERPROFILE '.config\herdr-app\mods\herdr-team-coordinator\coord.ps1'
& $coord local status
& $coord local start_daemon
& $coord local stop
& $coord local restart
```

`install.ps1 -CheckOnly` 与 `coord.ps1 local status` 均不会触发守护启动或初始化运行目录。守护进程空闲 5 分钟且无待处理任务/未完投递后自动有序退出，再次打开面板或收到有效 `coord` 请求时按需唤醒。
