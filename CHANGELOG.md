# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First release: when a Herdr terminal is restored after herdr-core stops, WSL shuts down, or the system reboots, the agent session that was running in it resumes automatically.

### Added

- Resume the original Claude Code, Codex CLI, or Pi session in a restored terminal, in its original working directory.
- Show a countdown before resuming: press Enter to resume now, or `n`/`Esc` to skip. A skipped resume command stays in your shell history.
- Add `HERDR_RESUME_MODE=confirm` to put the resume command on the command line without running it; `off` disables resuming.
- Carry over allowlisted launch options, such as `--model` and `--permission-mode` for Claude Code, and `-m` and `-c` for Codex.
- Sessions you quit yourself (`/exit`, Ctrl-D) are not brought back after a restart.
- Add `herdr-resume status` and `herdr-resume snapshot --dry-run` to inspect recorded sessions and preview what would be recorded.
- Add `install.sh` to link the command and enable the user service, with `--dry-run` and `--uninstall`; it never edits shell configuration.

### Security

- Options that skip permission prompts or the sandbox are dropped by default; set `HERDR_RESUME_ALLOW_DANGEROUS=1` to carry them over.
- Record files are readable and writable only by the current user; prompts and the values of dropped options are never written to disk.
- Sessions whose ID, directory, or options contain control characters are neither recorded nor resumed, which prevents injected commands.

[Unreleased]: https://github.com/pine2D/herdr-session-resume/commits/main
