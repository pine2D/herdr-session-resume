# herdr-session-resume 开发约定

面向用户的说明在 README.md（英文）和 README.zh-CN.md（中文）。本文件写给维护者和 AI 编码助手：结构、约束、实测事实、验证方法。本机部署细节在 `CLAUDE.local.md`（不入库）。

## 结构

| 文件 | 作用 |
|---|---|
| `herdr_resume.py` | 全部逻辑：snapshot / watch / claim / status，Python 3.8+ 标准库 |
| `herdr-resume` | Bash 入口，解析符号链接后 exec Python |
| `herdr-resume.zsh` | zsh 片段：首个提示符前 claim 并恢复；之后每次提示符清理本 shell 的条目 |
| `herdr-resume.service` | systemd 用户服务，`watch --interval 15` 常驻 |
| `install.sh` | 只建符号链接和启用服务，**不改用户 shell 配置** |
| `tests/test_herdr_resume.py` | 单元测试，假 hc / 假进程表，不碰真实 core 和会话 |

状态：`~/.local/state/herdr-resume/<HC_SESSION>/<tN>.json`，日志 `herdr-resume.log`（超 1 MiB 减半）。

## 硬约束

- **只用标准库**，不引入第三方包；Bash 入口保持 `set -euo pipefail`。
- **zsh 片段要轻**：没有条目时不能 fork（只做 `[[ -e ]]`、`$(<file)`）；非交互 shell、非 herdr 终端必须直接 return；重复 source 无副作用。
- **core 未运行时不得调用 hc**：hc 的任何子命令（含 `server status`、`ls`）都会自动拉起已停止的 core。先用 `core_running()` 探测 socket。
- **claim 输出用 NUL 分隔 4 段**（描述、目录、命令、auto/confirm），zsh 侧用 `${(@0)...}` 并要求恰好 4 段；不要退回按行协议。
- **写入前校验**：会话 ID 只允许 `[A-Za-z0-9._-]{1,128}`；目录和参数不得含控制字符；claim 时再校验一次。
- **不落盘敏感值**：`dropped` 只记选项名；位置参数（可能是提示词）一律不存。进程 umask 077，状态文件 600。
- **危险参数默认丢弃**：跳过权限/沙箱类参数只在 `HERDR_RESUME_ALLOW_DANGEROUS=1` 时带回。新增 agent 参数先归入 `ARG_SPECS`，拿不准就不放行。
- **只读对方数据**：Claude/Codex/Pi 的会话文件和数据库只读打开（sqlite 用 `mode=ro`），不修改 agent 配置、不装 hook。

## 条目生命周期

1. snapshot：前台是 agent 且能解析出会话 → 写条目（记录 shell_pid/start、agent_pid/start、boot_id）。解析不出会话时，沿用同终端、同 agent、同目录的旧条目（`inherit_entry`，用于恢复后尚未产生新记录的 Pi）。
2. 前台不是 agent：条目属于当前这个 shell（pid、启动时刻、boot_id 都一致）→ 删除，视为用户主动退出；shell 已经换了 → 保留，等待恢复；超过 7 天 → 删除。
3. 终端已不存在于 `hc ls` → 删除。hc 出错时整轮不做任何改动。
4. claim（新 shell 首个提示符前）：条目不属于当前 shell、原 agent 不在了、调用者是该终端的根 shell（`hc get` 的 pid）→ 把条目改归当前 shell 并标记 claimed，输出恢复命令。claimed 之后 120 秒内快照不会删除它。
5. zsh 每次提示符回来：条目里的 `"shell_pid": $$,` 和自己一致就删除。这依赖 `write_atomic` 的输出格式（`indent=1, sort_keys`），改格式要同步改 zsh。

## 实测事实（herdr-core 0.1.16，2026-11）

- `hc server restart`：holder 托管着 pty，程序不中断。
- `hc server stop` 后 start，或 SIGKILL core + holder + 全部子进程（等同 WSL 关机）：开了 keep 的终端地址、`HC_TERMINAL_ID`、labels 都不变，core 在终端**初始** cwd 起一个新 shell。
- `hc get <t>`：顶层 `pid` 是终端的根 shell；`foreground` 给出前台进程组 leader 的 argv、cwd、pid；顶层 `cwd` 是终端创建时的目录。
- Claude Code：`~/.claude/sessions/<pid>.json` 里有 `sessionId`、`cwd`、`procStart`（等于 `/proc/<pid>/stat` 第 22 字段）。`--resume` 后会话 ID 不变。该目录不会清理旧文件，pid 会被复用，必须核对 procStart。
- Codex 0.166：TUI 进程不持有 rollout 文件，文件由共享的 app-server 守护进程持有。会话只能查 `$CODEX_HOME/state_*.sqlite` 的 `threads` 表：cwd 相同、未归档、source 不是 subagent、`updated_at_ms` 不早于进程启动时间。Codex 的 hook 在守护进程里运行，拿不到 `HC_TERMINAL_ID`。
- Pi：进程名会被改写为 `pi`，argv 不可见。会话文件在 `~/.pi/agent/sessions/<编码后的 cwd>/<时间>_<id>.jsonl`，首行 `{"type":"session","id":…,"cwd":…}`，发出第一条消息之后才会生成。
- agent 升级后，上面这些来源可能会变。出现解析失败时，先用 `herdr-resume snapshot --dry-run` 对照真实会话核实。

## 验证

```bash
bash -n herdr-resume install.sh && zsh -n herdr-resume.zsh
python3 -B tests/test_herdr_resume.py
./herdr-resume --help && ! ./herdr-resume --help extra    # 帮助须含 用法/示例/副作用/退出状态；多余参数返回 2
herdr-resume snapshot --dry-run                            # 对真实 core 只读演练
```

端到端测试必须用隔离的 core，**绝不能对 default core 执行 stop 或 kill**：

```bash
export HC_SESSION=hrtest XDG_RUNTIME_DIR=/tmp/hrt \
       XDG_STATE_HOME=<临时目录>/state XDG_CONFIG_HOME=<临时目录>/config \
       HERDR_RESUME_STATE=<临时目录>/rstate ZDOTDIR=<临时目录>/zdot
unset HC_TERMINAL_ID HC_TERMINAL_SOCKET HC_SCOPE HC_SOCKET CLAUDECODE CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID
hc server start && hc server status   # 核对 core id 不是 default 的那个
```

- socket 路径不能超过 107 字节，所以 `XDG_RUNTIME_DIR` 要短。
- 如果在 Claude Code 会话里做测试，必须 unset 上面那些 `CLAUDE*` 变量，否则测试里启动的 Claude 不保存 transcript，无法恢复。
- `hc run` 的选项必须写在程序名之前（`hc run --json --bg --keep … /usr/bin/zsh`），写在后面会被当成程序参数。
- 测试用的 agent 只发极短的消息，不要带危险参数启动。
- 测试结束后执行 `hc server stop`，并删除临时目录。

## 设计取舍

- **轮询而非 hook**：Codex 的 hook 跑在共享守护进程里，拿不到终端身份；Pi 没有对应的 hook；装 hook 还要改各 agent 的配置。轮询统一覆盖三种 agent，代价是最多 15 秒的空窗。
- **用常驻循环而非 timer**：每 15 秒一次的 oneshot 会刷满 journal。
- **zsh 首个提示符前恢复，经 zle accept-line 提交**：命令会进历史、触发 preexec（终端标题、herdr 状态），和手敲的一样。
- **claim 改归属而非删除**：恢复后的 agent 在产生新记录之前，快照可以沿用旧会话 ID；用户跳过或退出时由 zsh 删除。

## 兼容版本

以下版本经过端到端实测。对应工具升级后，先跑 `herdr-resume snapshot --dry-run` 对照真实会话核实，再更新本表：

| 组件 | 验证版本 | 依赖的接口 |
|---|---|---|
| herdr-core / hc | 0.1.16 | `hc ls/get --json`、keep 恢复、socket 路径 |
| Claude Code | 2.1.316 | `~/.claude/sessions/<pid>.json`、`--resume` |
| Codex CLI | 0.166.0 | `state_*.sqlite` 的 `threads` 表、`codex resume` |
| Pi | 1.4.0 | `~/.pi/agent/sessions/**.jsonl` 首行、`--session` |

如果某个工具的会话来源变了，在解析函数里兼容新旧两种格式；做不到兼容时，在 CHANGELOG 里写明最低支持版本。

## 新增 agent 的步骤

1. 实测会话 ID 存在哪里、怎样和进程对应（pid、启动时刻、cwd），并确认能不能从 argv 看到启动参数。
2. 在 `ARG_SPECS` 里登记参数：`value` 和 `flag` 是白名单；`skip_value` 用于让我们自己的恢复参数（如 `--resume`）不被重复带上；危险参数放进 `danger_*`。
3. 写 `resolve_<agent>`，注册到 `RESOLVERS`；在 `resume_command` 里加上恢复命令。
4. 单元测试：覆盖参数过滤和解析的成功、失败路径。
5. 在隔离 core 里做端到端测试：启动、发一条短消息、SIGKILL、恢复，核对恢复出的是原对话。
6. 更新两份 README 的支持表和已知限制，本文件的「实测事实」「兼容版本」，以及 CHANGELOG。

## 版本与变更记录

- 遵循语义化版本，tag 写作 `vX.Y.Z`（annotated）。`0.y.z` 阶段：新增功能或不兼容改动升 minor，修复升 patch；不兼容改动必须在 CHANGELOG 里标 `**破坏性变更：**` 并写明迁移方法。
- 视为公共接口的有：命令行子命令和参数、环境变量、`~/.zshrc` 的 source 方式、状态目录位置、`install.sh` 的行为。状态文件格式变化时同步提高 `VERSION` 常量；读不了的旧条目要能安全忽略。
- `CHANGELOG.md` 用**英文**，遵循 Keep a Changelog（Added / Changed / Deprecated / Removed / Fixed / Security）。用户能感知的变化合入时就写进 `Unreleased`；纯内部重构、测试和 CI 改动不写。
- 发版：把 `Unreleased` 晋升为版本段、更新底部链接 → 提交 → 打 tag → 推送 → 从版本段派生 GitHub Release 正文。tag、推送和发布都要先经维护者确认。

## 提交约定

- 使用 Conventional Commits，摘要用中文，例如 `feat(codex): 支持 --profile 参数带回`、`fix(zsh): …`。
- scope 按模块取：`core`（herdr_resume.py）、`zsh`、`install`、`service`、`docs`、`test`，或具体 agent 名（`claude`、`codex`、`pi`）。
- 一个提交只做一件事；行为变化和 CHANGELOG、README 的对应更新放在同一个提交里。
- 提交前跑完「验证」一节的全部检查；不提交 `CLAUDE.local.md`、运行时状态、日志和个人路径。

## 许可证

MIT（见 `LICENSE`）。引入外部代码或参考实现前，确认其许可证与 MIT 兼容，并在提交说明里注明出处。

## 文档

- README 写给使用者：解决什么、效果、安装、设置、限制、排查。不写实现细节和开发流程。
- README 维护两种语言：`README.md` 为英文（默认），`README.zh-CN.md` 为简体中文，顶部互链。两份事实、命令、表格行必须一一对应，任何改动在同一个提交里同步；各自按本语言习惯行文，不逐句直译。
- 行为、参数白名单或支持的 agent 有变化时，同步更新两份 README 的「Supported agents / 支持的 agent」「Known limitations / 已知限制」，以及本文件的「实测事实」「兼容版本」和 CHANGELOG。
- 工具在终端里打印的提示目前只有中文；如果改成双语，README 里的输出示例要一并更新。
- CLAUDE.md 是维护者文档，用中文。
