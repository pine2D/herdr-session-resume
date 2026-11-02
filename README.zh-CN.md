# herdr-session-resume

[English](README.md) | 简体中文

重启之后，让 [Herdr](https://herdr.dev) 终端里的 Claude Code、Codex、Pi 会话回到原位。

Herdr 重启后会恢复空间、标签页和分屏，但每个终端里原来在跑的 agent 都没了，只剩一个空 shell，要一个个手动 `--resume`。herdr-resume 会记住每个终端在跑哪个会话；终端恢复后，自动进入原来的目录、带上原来的启动参数，接上原来的对话：

```text
[herdr-resume] claude 会话 e9636db5（work / api） —— 3 秒后恢复（Enter 立即，n/Esc 跳过）
$ claude --resume e9636db5-3a61-4787-b2be-9d2bfb84d9b7 --model opus --permission-mode auto
```

## 适用场景

| 情况 | 结果 |
|---|---|
| herdr-core 停止后重新启动 | 自动恢复 |
| `wsl --shutdown`、Windows 重启、Linux 重启 | 自动恢复 |
| `hc server restart`（更新 core） | 不需要恢复，程序本来就没中断 |
| 你自己退出了 agent（`/exit`、Ctrl-D） | 不会被拉起 |
| 倒计时里按 `n` / `Esc` | 跳过，恢复命令留在 shell 历史里 |

它只依赖 herdr-core 所在的那台机器，与 Herdr App 跑在哪里无关。App 在 Windows 或 macOS、core 和 agent 在 WSL 或远程 Linux 的用法同样适用。

## 支持的 agent

| agent | 恢复命令 | 带回的启动参数 |
|---|---|---|
| Claude Code | `claude --resume <id>` | `--model`、`--permission-mode`、`--effort`、`--add-dir`、`--agent`、`--settings`、`--mcp-config` 等 |
| Codex CLI | `codex resume <id>` | `-m`、`-p`、`-s`、`-a`、`-c`、`--add-dir`、`--search` 等 |
| Pi | `pi --session <id>` | 模型和思考级别由 Pi 的会话文件自己恢复 |

跳过权限确认或沙箱的参数（如 `--dangerously-skip-permissions`、`--yolo`）默认**不会**被带回。

## 要求

- herdr-core（`hc` 命令），已在 0.1.16 上测试
- 终端开启了 keep。Herdr App 新建的终端默认开启；用 `hc run` 自己开的终端需要加 `--keep`
- zsh
- Python 3.8+，只用标准库
- systemd 用户会话。WSL 需要在 `/etc/wsl.conf` 里设置 `[boot] systemd=true`

## 安装

```bash
git clone https://github.com/pine2D/herdr-session-resume.git ~/projects/herdr-session-resume
cd ~/projects/herdr-session-resume
./install.sh
```

`install.sh` 会创建 `~/.local/bin/herdr-resume` 命令，并启用 `herdr-resume.service` 用户服务。最后按它的提示，在 `~/.zshrc` 末尾加一行：

```zsh
[[ -r ~/projects/herdr-session-resume/herdr-resume.zsh ]] && source ~/projects/herdr-session-resume/herdr-resume.zsh
```

然后检查一下：

```bash
herdr-resume status
```

这里应该列出当前开着 agent 的终端。新开的 herdr 终端才会加载 zsh 那一段；服务会立刻开始记录所有终端。

## 设置

在 `~/.zshrc` 里 source 那一行之前设置这些环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `HERDR_RESUME_MODE` | `auto` | `auto`：倒计时后自动执行；`confirm`：只把命令填进命令行，按 Enter 才执行；`off`：关闭 |
| `HERDR_RESUME_DELAY` | `3` | 倒计时秒数 |
| `HERDR_RESUME_ALLOW_DANGEROUS` | 未设置 | 设为 `1` 时连跳过权限、沙箱的参数也一起带回。必须写进服务环境（`systemctl --user edit herdr-resume.service`）才生效 |

## 已知限制

- **最多漏掉 15 秒**：服务每 15 秒记录一次，在崩溃前十几秒内才开始的会话可能来不及记下。
- **同一目录多个 Codex**：同一个目录里同时开着多个 Codex 时分不清对应关系，恢复时会打开 `codex resume` 的会话列表让你选，并且不会自动执行。
- **Pi 的启动参数**：Pi 会改写自己的进程名，启动参数读不到，只能靠它的会话文件恢复模型和思考级别。
- **只支持 zsh**：bash 和 fish 暂不支持。

## 排查

```bash
herdr-resume status                       # 已记录的会话：运行中 / 待恢复
herdr-resume snapshot --dry-run           # 现在会记录什么（不写文件）
systemctl --user status herdr-resume      # 服务状态
tail ~/.local/state/herdr-resume/herdr-resume.log
```

## 隐私

记录只保存在本机 `~/.local/state/herdr-resume/`，只有你自己能读写。内容是会话 ID、目录和允许带回的参数。提示词之类的位置参数和被丢弃参数的值都不会存。它只读取各 agent 自己的会话元数据，不读对话内容，也不联网。

## 卸载

```bash
./install.sh --uninstall
```

然后从 `~/.zshrc` 删掉 source 那一行。

## 和其他社区方案比较

| 方案 | 不同之处 |
|---|---|
| herdr-revive | 只记录本机终端；App 和 core 不在同一台机器时无效 |
| herdr-agent-resume-plus | 要求 App 本身跑在 Linux 上 |
| herdr-agent-resume | 快捷键在 App 所在的机器执行；自动模式的安装器会改动多处 shell 和 agent 配置 |
| herdr-claude-resume | 只支持 macOS 和 Claude |

herdr-resume 全程跑在 core 那一侧，不改 agent 的配置，也不装 hook。只需要一个用户服务，外加 `~/.zshrc` 里的一行。

## 工作原理

1. 用户服务每 15 秒通过 `hc` 查看每个终端前台在跑什么，然后从 Claude Code、Codex、Pi 各自的会话记录里找出当前的会话 ID。
2. herdr 恢复的终端编号不变，但里面是一个新 shell。新 shell 第一次出提示符之前，如果发现这个终端有一条记录、对应的 agent 已经不在了，而且自己就是这个终端的 shell，就认领这条记录，并执行恢复命令。
3. 你主动退出 agent、提示符回来的那一刻，这条记录就会被删除，所以退出之后再重启也不会被拉起。
4. core 没在运行时，服务不会调用 `hc`，因为 `hc` 会自动拉起 core。

## 许可证

[MIT](LICENSE)。变更记录见 [CHANGELOG.md](CHANGELOG.md)（英文）。
