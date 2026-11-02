# herdr-session-resume

English | [简体中文](README.zh-CN.md)

Bring your Claude Code, Codex, and Pi sessions back after a [Herdr](https://herdr.dev) restart.

When Herdr restarts, it restores your spaces, tabs, and splits, but every terminal comes back as an empty shell: the agent that was running there is gone, and you have to `--resume` each one by hand. herdr-resume remembers which session each terminal was running. When the terminal is restored, it changes to the original directory and resumes the same conversation with the original launch options:

```text
[herdr-resume] claude session e9636db5 (work / api) — resuming in 3s (Enter: now, n/Esc: skip)
$ claude --resume e9636db5-3a61-4787-b2be-9d2bfb84d9b7 --model opus --permission-mode auto
```

Messages are in English, or in Chinese when your locale is Chinese.

## When it applies

| Situation | Result |
|---|---|
| herdr-core is stopped and started again | Resumed automatically |
| `wsl --shutdown`, Windows reboot, Linux reboot | Resumed automatically |
| `hc server restart` (for example, after a core update) | Nothing to resume; programs keep running |
| You quit the agent yourself (`/exit`, Ctrl-D) | Not brought back |
| You press `n` or `Esc` during the countdown | Skipped; the resume command stays in your shell history |

Only the machine running herdr-core matters, not where the Herdr App runs. It works the same when the App runs on Windows or macOS and the core and agents run in WSL or on a remote Linux host.

## Supported agents

| Agent | Resume command | Launch options carried over |
|---|---|---|
| Claude Code | `claude --resume <id>` | `--model`, `--permission-mode`, `--effort`, `--add-dir`, `--agent`, `--settings`, `--mcp-config`, and others |
| Codex CLI | `codex resume <id>` | `-m`, `-p`, `-s`, `-a`, `-c`, `--add-dir`, `--search`, and others |
| Pi | `pi --session <id>` | Pi restores the model and thinking level from its own session file |

Options that skip permission prompts or the sandbox (such as `--dangerously-skip-permissions` or `--yolo`) are **not** carried over by default.

## Requirements

- herdr-core (the `hc` command); tested with 0.1.16
- Terminals with keep enabled. Terminals created by the Herdr App have it on by default; for terminals you start with `hc run`, add `--keep`
- zsh
- Python 3.8 or later (standard library only)
- A systemd user session. On WSL, set `[boot] systemd=true` in `/etc/wsl.conf`

## Install

```bash
git clone https://github.com/pine2D/herdr-session-resume.git ~/projects/herdr-session-resume
cd ~/projects/herdr-session-resume
./install.sh
```

`install.sh` creates the `~/.local/bin/herdr-resume` command and enables the `herdr-resume.service` user service. It does not edit your shell configuration. As the installer prompts, add this line to the end of `~/.zshrc`:

```zsh
[[ -r ~/projects/herdr-session-resume/herdr-resume.zsh ]] && source ~/projects/herdr-session-resume/herdr-resume.zsh
```

Then check that it works:

```bash
herdr-resume status
```

It should list the terminals that are currently running an agent. The service starts recording all terminals right away; the zsh part takes effect in newly opened Herdr terminals.

## Settings

Set these environment variables in `~/.zshrc` before the `source` line:

| Variable | Default | Effect |
|---|---|---|
| `HERDR_RESUME_MODE` | `auto` | `auto`: run the resume command after the countdown. `confirm`: only put the command on the command line; press Enter to run it. `off`: disable resuming |
| `HERDR_RESUME_DELAY` | `3` | Countdown length in seconds (1–60) |
| `HERDR_RESUME_LANG` | from locale | `en` or `zh`. By default, Chinese is used when `LC_ALL`, `LC_MESSAGES`, or `LANG` starts with `zh`; otherwise English |
| `HERDR_RESUME_ALLOW_DANGEROUS` | unset | Set to `1` to also carry over options that skip permission prompts or the sandbox. It only takes effect in the service environment (`systemctl --user edit herdr-resume.service`) |

## Known limitations

- **Up to 15 seconds can be missed.** The service records every 15 seconds, so a session started in the last few seconds before a crash may not be recorded.
- **Several Codex sessions in one directory.** herdr-resume cannot tell them apart. Instead of resuming automatically, it opens the `codex resume` session picker so you can choose.
- **Pi launch options.** Pi rewrites its process name, so its launch options cannot be read; the model and thinking level come back only through Pi's session file.
- **zsh only.** bash and fish are not supported yet.

## Troubleshooting

```bash
herdr-resume status                       # recorded sessions: running / waiting to resume
herdr-resume snapshot --dry-run           # what would be recorded now (writes nothing)
systemctl --user status herdr-resume      # service status
tail ~/.local/state/herdr-resume/herdr-resume.log
```

## Privacy

Records stay on your machine in `~/.local/state/herdr-resume/`, readable and writable only by you. They contain session IDs, directories, and the launch options that may be carried over. Prompts and other positional arguments, and the values of dropped options, are never stored. herdr-resume reads only each agent's session metadata, never conversation content, and makes no network requests.

## Uninstall

```bash
./install.sh --uninstall
```

Then remove the `source` line from `~/.zshrc`.

## Compared with other community mods

| Mod | Difference |
|---|---|
| herdr-revive | Records only terminals on the App's own machine; does not work when the App and the core run on different machines |
| herdr-agent-resume-plus | Requires the App itself to run on Linux |
| herdr-agent-resume | Its shortcut runs on the machine where the App runs; its automatic mode's installer edits several shell and agent configuration files |
| herdr-claude-resume | macOS and Claude only |

herdr-resume runs entirely on the core side. It does not change any agent configuration or install hooks; it needs one user service and one line in `~/.zshrc`.

## How it works

1. Every 15 seconds, the user service uses `hc` to see what each terminal is running in the foreground, then looks up the current session ID in the session records of Claude Code, Codex, or Pi.
2. A restored Herdr terminal keeps its terminal ID but starts a new shell. Before that shell shows its first prompt, it checks for a record for this terminal. If the recorded agent is no longer running and this shell is the terminal's own shell, it claims the record and runs the resume command.
3. When you quit an agent and your prompt returns, the record is deleted immediately, so a later restart does not bring it back.
4. While the core is not running, the service does not call `hc`, because any `hc` command starts the core.

## License

[MIT](LICENSE). See [CHANGELOG.md](CHANGELOG.md) for release notes.
