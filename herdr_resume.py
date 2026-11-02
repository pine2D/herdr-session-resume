#!/usr/bin/env python3
"""herdr-resume：记录 herdr 终端里的 agent 会话，终端被恢复后在原位置续上。

快照（snapshot）由定时器周期运行：读 `hc ls/get` 找出前台是 Claude Code / Codex / Pi
的终端，解析会话 ID，每个终端写一份条目。终端 shell 还活着而 agent 已不在前台
（用户主动退出）时删除条目；shell 换了（core 重启、WSL 关机后 herdr 给终端新 shell）
时保留条目，等新 shell 第一次出提示符时认领（claim）并恢复。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
PENDING_TTL = 7 * 86400
CLAIM_GRACE = 120
LOG_LIMIT = 1 << 20
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 每个 agent 允许带回的启动参数。未列出的参数一律丢弃（含提示词等位置参数）；
# danger_* 只有设置 HERDR_RESUME_ALLOW_DANGEROUS=1 才保留。
ARG_SPECS = {
    "claude": {
        "value": {"--model", "--permission-mode", "--effort", "--add-dir", "--agent",
                  "--settings", "--mcp-config", "--fallback-model", "--setting-sources",
                  "--plugin-dir", "--append-system-prompt", "--system-prompt"},
        "flag": {"--strict-mcp-config", "--verbose", "--ide", "--chrome", "--no-chrome"},
        "danger_flag": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"},
        "danger_value": {("--permission-mode", "bypassPermissions")},
        "skip_value": {"--resume", "-r", "--session-id", "--name", "-n"},
    },
    "codex": {
        "value": {"-m", "--model", "-p", "--profile", "-s", "--sandbox", "-a",
                  "--ask-for-approval", "-c", "--config", "--add-dir", "--enable",
                  "--disable", "--local-provider"},
        "flag": {"--search", "--oss", "--no-alt-screen", "--strict-config"},
        "danger_flag": {"--dangerously-bypass-approvals-and-sandbox", "--yolo",
                        "--approve-for-me", "--dangerously-bypass-hook-trust"},
        "danger_value": {("-s", "danger-full-access"), ("--sandbox", "danger-full-access")},
        "skip_value": {"-C", "--cd", "-i", "--image", "--remote", "--remote-auth-token-env"},
    },
    "pi": {
        "value": {"--provider", "--model", "--thinking", "--models", "--session-dir"},
        "flag": set(),
        "danger_flag": set(),
        "danger_value": set(),
        "skip_value": {"--session", "--session-id", "--fork", "--name", "-n", "--export"},
    },
}


class HcError(RuntimeError):
    pass


# ---------- 环境与进程 ----------

def state_dir() -> Path:
    root = os.environ.get("HERDR_RESUME_STATE") or str(Path.home() / ".local/state/herdr-resume")
    return Path(root) / (os.environ.get("HC_SESSION") or "default")


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def proc_start(pid) -> int | None:
    """进程启动时刻（开机以来的时钟滴答，/proc/<pid>/stat 第 22 字段）；进程不存在返回 None。"""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(stat[stat.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def proc_start_epoch_ms(pid) -> int | None:
    ticks = proc_start(pid)
    if ticks is None:
        return None
    try:
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
    except (OSError, StopIteration, ValueError):
        return None
    return int((btime + ticks / os.sysconf("SC_CLK_TCK")) * 1000)


def alive(pid, start, boot) -> bool:
    return bool(pid) and boot == boot_id() and start is not None and proc_start(pid) == start


def core_socket() -> Path:
    if os.environ.get("HC_SOCKET"):
        return Path(os.environ["HC_SOCKET"])
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "herdr-core" / f"{os.environ.get('HC_SESSION') or 'default'}.sock"


def core_running() -> bool:
    """只探测 socket，不经 hc：hc 的任何命令都会自动拉起已停止的 core。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            sock.connect(str(core_socket()))
        except OSError:
            return False
    return True


def hc(*args):
    try:
        out = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HcError(f"hc {' '.join(args)}: {exc}") from exc
    try:
        data = json.loads(out.stdout or "null")
    except json.JSONDecodeError as exc:
        raise HcError(f"hc {' '.join(args)}: 输出不是 JSON") from exc
    if out.returncode != 0 or not isinstance(data, dict) or "error" in data:
        msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
        raise HcError(f"hc {' '.join(args)}: {msg or out.stderr.strip() or out.returncode}")
    return data


@contextlib.contextmanager
def locked(sd: Path):
    sd.mkdir(parents=True, exist_ok=True)
    with open(sd / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def load(path: Path):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


def write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # 一行一个键，zsh 片段靠 `"shell_pid": <pid>,` 判断条目归属，不必解析 JSON。
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def log(msg: str) -> None:
    path = state_dir().parent / "herdr-resume.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            data = path.read_bytes()
            path.write_bytes(data[len(data) // 2:].split(b"\n", 1)[-1])
        with open(path, "a") as fh:
            fh.write(f"{time.strftime('%F %T')} [{os.environ.get('HC_SESSION') or 'default'}] {msg}\n")
    except OSError:
        pass


# ---------- 参数过滤 ----------

def classify(fg) -> str | None:
    if not isinstance(fg, dict):
        return None
    argv = fg.get("argv") or []
    names = {fg.get("name") or "", os.path.basename(argv[0]) if argv else ""}
    for agent in ARG_SPECS:
        if agent in names:
            return agent
    return None


def filter_args(agent: str, argv: list[str], allow_dangerous: bool = False):
    """返回 (保留的参数, 被丢弃的参数, 解析出的选项值字典)。"""
    spec = ARG_SPECS[agent]
    kept, dropped, values = [], [], {}
    args = list(argv[1:])
    if agent == "codex" and args[:1] == ["resume"]:
        args = args[1:]
        if args and not args[0].startswith("-"):
            values["resume_id"] = args.pop(0)
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        if not arg.startswith("-") or arg == "-":
            dropped.append(arg)
            i += 1
            continue
        if name in spec["value"] or name in spec["skip_value"]:
            if eq:
                val, step = inline, 1
            elif i + 1 < len(args):
                val, step = args[i + 1], 2
            else:
                dropped.append(arg)
                break
            values[name] = val
            pair = [arg] if eq else [arg, val]
            if name in spec["skip_value"]:
                pass
            elif (name, val) in spec["danger_value"] and not allow_dangerous:
                dropped.extend(pair)
            else:
                kept.extend(pair)
            i += step
            continue
        if name in spec["flag"] or (name in spec["danger_flag"] and allow_dangerous):
            kept.append(arg)
        else:
            dropped.append(arg)
        i += 1
    return kept, dropped, values


# ---------- 会话解析 ----------

def resolve_claude(fg, values):
    pid = fg["pid"]
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    data = None
    try:
        data = json.loads((base / "sessions" / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if str(data.get("procStart")) != str(proc_start(pid)):
        return None
    sid = data.get("sessionId")
    if not sid:
        return None
    return {"session_id": sid, "cwd": data.get("cwd") or fg.get("cwd")}


def codex_state_db() -> Path | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    dbs = sorted(home.glob("state_*.sqlite"),
                 key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    return dbs[-1] if dbs else None


def codex_cwd(fg, values) -> str:
    cwd = fg.get("cwd") or ""
    target = values.get("-C") or values.get("--cd")
    return os.path.normpath(os.path.join(cwd, target)) if target else cwd


def resolve_codex(fg, values):
    cwd = codex_cwd(fg, values)
    start_ms = proc_start_epoch_ms(fg["pid"]) or 0
    db = codex_state_db()
    rows = []
    if db:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                rows = con.execute(
                    "SELECT id FROM threads WHERE cwd = ? AND archived = 0"
                    " AND source NOT LIKE '%subagent%'"
                    " AND COALESCE(updated_at_ms, updated_at * 1000) >= ?"
                    " ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC LIMIT 5",
                    (cwd, start_ms - 5000)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            rows = []
    if rows:
        return {"session_id": rows[0][0], "cwd": cwd}
    rid = values.get("resume_id")
    if rid and UUID_RE.match(rid):
        return {"session_id": rid, "cwd": cwd}
    return None


def resolve_pi(fg, values):
    base = values.get("--session-dir")
    root = Path(base) if base else Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi/agent") / "sessions"
    cwd = fg.get("cwd") or ""
    start = (proc_start_epoch_ms(fg["pid"]) or 0) / 1000 - 5
    best = None
    for f in root.glob("**/*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime < start or (best and mtime <= best[0]):
            continue
        try:
            with open(f) as fh:
                head = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError):
            continue
        if head.get("type") == "session" and head.get("cwd") == cwd and head.get("id"):
            best = (mtime, head["id"])
    return {"session_id": best[1], "cwd": cwd} if best else None


RESOLVERS = {"claude": resolve_claude, "codex": resolve_codex, "pi": resolve_pi}


def resume_command(agent, sid, kept, ambiguous):
    if agent == "claude":
        return ["claude", "--resume", sid, *kept]
    if agent == "codex":
        return ["codex", "resume", *([] if ambiguous else [sid]), *kept]
    return ["pi", "--session", sid, *kept]


def safe_fields(session_id, cwd, args) -> bool:
    """会话 ID 只允许常规字符；目录和参数不得含控制字符（防止换行错位后被 zsh 执行）。"""
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        return False
    return not any(CONTROL_RE.search(s) for s in [cwd or "", *args])


def dropped_names(dropped):
    """只记录被丢弃的选项名，不落盘取值和位置参数（可能是提示词正文）。"""
    names = sorted({a.partition("=")[0] for a in dropped if a.startswith("-")})
    if any(not a.startswith("-") for a in dropped):
        names.append("<位置参数/选项值>")
    return names


def build_entry(terminal, detail, codex_cwds):
    fg = detail.get("foreground")
    agent = classify(fg)
    if not agent or not fg.get("pid") or fg.get("pid") == detail.get("pid"):
        return None
    allow = os.environ.get("HERDR_RESUME_ALLOW_DANGEROUS") == "1"
    kept, dropped, values = filter_args(agent, fg.get("argv") or [], allow)
    resolved = RESOLVERS[agent](fg, values)
    if not resolved:
        return None
    if not safe_fields(resolved["session_id"], resolved["cwd"], kept):
        log(f"skip {terminal} {agent}: 会话 ID、目录或参数含非法字符")
        return None
    ambiguous = agent == "codex" and codex_cwds.get(resolved["cwd"], 0) > 1
    shell_pid = detail.get("pid")
    return {
        "version": VERSION,
        "terminal": terminal,
        "agent": agent,
        "session_id": resolved["session_id"],
        "ambiguous": ambiguous,
        "cwd": resolved["cwd"],
        "command": resume_command(agent, resolved["session_id"], kept, ambiguous),
        "dropped": dropped_names(dropped),
        "shell_pid": shell_pid,
        "shell_start": proc_start(shell_pid),
        "agent_pid": fg["pid"],
        "agent_start": proc_start(fg["pid"]),
        "boot_id": boot_id(),
        "where": (detail.get("labels") or {}).get("where", ""),
        "updated_at": int(time.time()),
    }


def inherit_entry(old, detail):
    """agent 在前台但解析不出会话（如恢复后的 Pi 尚未写会话文件）：沿用本终端同 agent、
    同目录的旧条目，只更新进程身份。"""
    fg = detail.get("foreground") or {}
    agent = classify(fg)
    if not old or old.get("agent") != agent or not fg.get("pid") or fg.get("pid") == detail.get("pid"):
        return None
    if agent != "codex" and old.get("cwd") != fg.get("cwd"):
        return None
    shell_pid = detail.get("pid")
    return {**old, "shell_pid": shell_pid, "shell_start": proc_start(shell_pid),
            "agent_pid": fg["pid"], "agent_start": proc_start(fg["pid"]), "boot_id": boot_id(),
            "claimed": False, "updated_at": int(time.time())}


def same_shell(entry, shell_pid) -> bool:
    return (entry.get("shell_pid") == shell_pid and entry.get("boot_id") == boot_id()
            and entry.get("shell_start") == proc_start(shell_pid))


# ---------- 命令 ----------

def cmd_snapshot(args) -> int:
    sd = state_dir()
    try:
        # core 未运行时不调用 hc，免得开机后抢在 Herdr App 之前拉起 core 和全部会话
        if not core_running():
            raise HcError(f"core 未运行（{core_socket()}）")
        live = {t["address"]: t for t in hc("ls")["terminals"] if t.get("state") == "live"}
        details = {addr: hc("get", addr) for addr in live}
    except HcError as exc:
        # core 不可达或正在重启时不动任何条目
        if not getattr(args, "quiet", False):
            print(f"herdr-resume: {exc}", file=sys.stderr)
        return 1
    codex_cwds = {}
    for d in details.values():
        fg = d.get("foreground") or {}
        if classify(fg) == "codex":
            _, _, values = filter_args("codex", fg.get("argv") or [])
            cwd = codex_cwd(fg, values)
            codex_cwds[cwd] = codex_cwds.get(cwd, 0) + 1
    now = int(time.time())
    actions = []
    with locked(sd):
        for addr, d in details.items():
            path = sd / f"{addr}.json"
            old = load(path)
            if classify(d.get("foreground")):
                entry = build_entry(addr, d, codex_cwds) or inherit_entry(old, d)
                if entry:
                    if not old or {k: v for k, v in old.items() if k != "updated_at"} != {
                            k: v for k, v in entry.items() if k != "updated_at"}:
                        actions.append(("write", path, entry))
                continue
            if not old:
                continue
            if old.get("claimed") and now - int(old.get("updated_at") or 0) < CLAIM_GRACE:
                continue
            if same_shell(old, d.get("pid")):
                actions.append(("drop-exited", path, old))
            elif now - int(old.get("updated_at") or 0) > PENDING_TTL:
                actions.append(("drop-expired", path, old))
        for path in sd.glob("t*.json"):
            if path.stem not in live:
                actions.append(("drop-gone", path, load(path) or {}))
        for kind, path, entry in actions:
            desc = f"{kind} {path.stem} {entry.get('agent', '?')} {entry.get('session_id', '')}"
            if args.dry_run:
                print(desc)
                continue
            if kind == "write":
                write_atomic(path, entry)
            else:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            log(desc)
    return 0


def cmd_watch(args) -> int:
    """常驻循环（用户服务）：每 interval 秒快照一次；core 不可达只在状态变化时报一次。"""
    last_err = None
    while True:
        err = None
        try:
            rc = cmd_snapshot(argparse.Namespace(dry_run=False, quiet=True))
            err = None if rc == 0 else "core 不可达"
        except Exception as exc:  # 单轮失败不退出循环
            err = f"{type(exc).__name__}: {exc}"
        if err != last_err:
            print(f"herdr-resume: {err or '恢复正常'}", file=sys.stderr, flush=True)
            last_err = err
        time.sleep(args.interval)


def cmd_claim(args) -> int:
    sd = state_dir()
    path = sd / f"{args.terminal}.json"
    if not path.exists():
        return 1
    with locked(sd):
        entry = load(path)
        if not entry:
            return 1
        if same_shell(entry, args.shell_pid):
            return 1
        if alive(entry.get("agent_pid"), entry.get("agent_start"), entry.get("boot_id")):
            return 1
        try:
            detail = hc("get", args.terminal)
        except HcError:
            return 1
        # 只让终端的根 shell 认领；agent 内部或嵌套的交互 shell 不碰条目
        if detail.get("pid") != args.shell_pid:
            return 1
        if not args.dry_run:
            # 条目改归新 shell：zsh 片段在恢复出的 agent 退出（或用户跳过）后删除它；
            # agent 刚恢复、尚无新会话记录时，快照据此沿用会话 ID。
            write_atomic(path, {**entry, "shell_pid": args.shell_pid,
                                "shell_start": proc_start(args.shell_pid), "boot_id": boot_id(),
                                "claimed": True, "updated_at": int(time.time())})
            log(f"claim {args.terminal} {entry['agent']} {entry['session_id']}")
    if not safe_fields(entry.get("session_id"), entry.get("cwd"), entry.get("command") or []):
        return 1
    where = CONTROL_RE.sub("", f"（{entry['where']}）") if entry.get("where") else ""
    desc = f"{entry['agent']} 会话 {entry['session_id'][:8]}{where}"
    if entry.get("ambiguous"):
        desc += "：同目录有多个 Codex，需在列表里选"
    # NUL 分隔：zsh 侧用 ${(0)...} 拆分，任何字段都无法伪造出额外的一段
    sys.stdout.write("\0".join([desc, entry["cwd"], shlex.join(entry["command"]),
                                "confirm" if entry.get("ambiguous") else "auto"]))
    return 0


def cmd_status(args) -> int:
    sd = state_dir()
    entries = [load(p) or {"terminal": p.stem, "agent": "?"} for p in sorted(sd.glob("t*.json"))]
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=1))
        return 0
    print(f"状态目录：{sd}")
    if not entries:
        print("（无条目）")
    for e in entries:
        state = "运行中" if alive(e.get("agent_pid"), e.get("agent_start"), e.get("boot_id")) else "待恢复"
        print(f"{e.get('terminal')}\t{e.get('agent')}\t{state}\t{e.get('cwd', '')}\t"
              f"{shlex.join(e.get('command') or [])}")
        if e.get("dropped"):
            print(f"\t已丢弃参数：{shlex.join(e['dropped'])}")
    return 0


HELP = """\
herdr-resume — herdr 终端恢复后自动续上原 Claude Code / Codex / Pi 会话

用法：
  herdr-resume snapshot [--dry-run]   记录一次当前各终端的 agent 会话
  herdr-resume watch [--interval 15]  常驻循环快照（herdr-resume.service 调用）
  herdr-resume status [--json]        查看已记录的条目
  herdr-resume claim --terminal tN --shell-pid PID [--dry-run]
                                         由 zsh 片段在新 shell 首个提示符前调用
  herdr-resume --help

环境变量：
  HERDR_RESUME_STATE            状态根目录（默认 ~/.local/state/herdr-resume）
  HC_SESSION                    herdr-core 会话名（默认 default）
  HERDR_RESUME_ALLOW_DANGEROUS  设为 1 时保留跳过权限/沙箱类参数（默认丢弃）
  HERDR_RESUME_MODE             zsh 侧：auto（默认，倒计时后执行）| confirm | off
  HERDR_RESUME_DELAY            zsh 侧：倒计时秒数（默认 3）

示例：
  herdr-resume status
  herdr-resume snapshot --dry-run

副作用：
  snapshot/watch 调用 hc ls/get，只读 Claude/Codex/Pi 会话元数据，写状态目录与日志；
  claim 把条目改归调用它的 shell。--dry-run、status、--help 不写任何文件。

退出状态：
  0  成功（claim：有可恢复会话并已输出）；1  core 不可达或无可恢复会话；2  参数错误。
"""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    if not argv or argv[0] in ("-h", "--help"):
        if len(argv) > 1:
            print("herdr-resume: --help 不接受其他参数", file=sys.stderr)
            return 2
        print(HELP, end="", file=sys.stdout if argv else sys.stderr)
        return 0 if argv else 2
    parser = argparse.ArgumentParser(prog="herdr-resume", add_help=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("claim")
    p.add_argument("--terminal", required=True)
    p.add_argument("--shell-pid", required=True, type=int)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=float, default=15)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    return {"snapshot": cmd_snapshot, "claim": cmd_claim, "status": cmd_status,
            "watch": cmd_watch}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
