#!/usr/bin/env python3
"""herdr_resume 回归测试：参数过滤与条目生命周期。不连接真实 herdr-core，不读真实会话。"""

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herdr_resume as hr  # noqa: E402

BOOT = "boot-1"


class FilterArgsTest(unittest.TestCase):
    def test_claude_keeps_whitelist_and_drops_prompt_and_resume(self):
        kept, dropped, _ = hr.filter_args("claude", [
            "claude", "--model", "opus", "--permission-mode=auto", "-r", "old", "fix bug", "-c"])
        self.assertEqual(kept, ["--model", "opus", "--permission-mode=auto"])
        self.assertIn("fix bug", dropped)
        self.assertIn("-c", dropped)
        self.assertNotIn("old", kept)

    def test_dangerous_dropped_unless_allowed(self):
        argv = ["claude", "--dangerously-skip-permissions", "--permission-mode", "bypassPermissions"]
        kept, dropped, _ = hr.filter_args("claude", argv)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, argv[1:])
        kept, _, _ = hr.filter_args("claude", argv, allow_dangerous=True)
        self.assertEqual(kept, argv[1:])
        kept, dropped, _ = hr.filter_args("codex", ["codex", "--yolo", "-s", "danger-full-access", "-m", "x"])
        self.assertEqual(kept, ["-m", "x"])

    def test_codex_resume_subcommand_and_cd(self):
        sid = "01a19d1b-7d4c-79d2-80c8-6014046f639d"
        kept, _, values = hr.filter_args("codex", ["codex", "resume", sid, "-C", "sub", "-c", "a=1"])
        self.assertEqual(kept, ["-c", "a=1"])
        self.assertEqual(values["resume_id"], sid)
        self.assertEqual(hr.codex_cwd({"cwd": "/w"}, values), "/w/sub")

    def test_resume_commands(self):
        self.assertEqual(hr.resume_command("claude", "S", ["--model", "m"], False),
                         ["claude", "--resume", "S", "--model", "m"])
        self.assertEqual(hr.resume_command("codex", "S", [], True), ["codex", "resume"])
        self.assertEqual(hr.resume_command("pi", "S", [], False), ["pi", "--session", "S"])


class Fake:
    """可变的假 herdr 世界：terminals[addr] = {pid, fg: {...} | None}；procs[pid] = start。"""

    def __init__(self):
        self.terminals = {}
        self.procs = {}

    def hc(self, *args):
        if args[0] == "ls":
            return {"terminals": [{"address": a, "state": "live"} for a in self.terminals]}
        t = self.terminals[args[1]]
        fg = t["fg"] or {"argv": ["/usr/bin/zsh"], "name": "zsh", "pid": t["pid"], "cwd": "/w"}
        return {"address": args[1], "pid": t["pid"], "foreground": fg, "labels": {}}

    def proc_start(self, pid):
        return self.procs.get(pid)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Fake()
        patches = [
            mock.patch.dict(os.environ, {"HERDR_RESUME_STATE": self.tmp.name, "HC_SESSION": "test"}),
            mock.patch.object(hr, "hc", self.fake.hc),
            mock.patch.object(hr, "core_running", lambda: True),
            mock.patch.object(hr, "proc_start", self.fake.proc_start),
            mock.patch.object(hr, "boot_id", lambda: BOOT),
            mock.patch.dict(hr.RESOLVERS, {"claude": self.resolve}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sessions = {}

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, fg, values):
        sid = self.sessions.get(fg["pid"])
        return {"session_id": sid, "cwd": "/proj"} if sid else None

    def path(self, addr="t1"):
        return Path(self.tmp.name) / "test" / f"{addr}.json"

    def snapshot(self):
        self.assertEqual(hr.cmd_snapshot(argparse.Namespace(dry_run=False)), 0)

    def claim(self, shell_pid, addr="t1"):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = hr.cmd_claim(argparse.Namespace(terminal=addr, shell_pid=shell_pid, dry_run=False))
        return rc, out.getvalue().split("\0") if out.getvalue() else []

    def start_agent(self, shell=100, agent=200, sid="S1", argv=("claude", "--model", "opus")):
        self.fake.procs.update({shell: 1, agent: 2})
        self.fake.terminals["t1"] = {"pid": shell, "fg": {
            "argv": list(argv), "name": "claude", "pid": agent, "cwd": "/proj"}}
        self.sessions[agent] = sid

    def crash_and_recover(self, new_shell=300):
        self.fake.procs.clear()
        self.fake.procs[new_shell] = 9
        self.fake.terminals["t1"] = {"pid": new_shell, "fg": None}

    def test_user_exit_removes_entry(self):
        self.start_agent()
        self.snapshot()
        self.assertTrue(self.path().exists())
        self.fake.terminals["t1"]["fg"] = None
        del self.fake.procs[200]
        self.snapshot()
        self.assertFalse(self.path().exists())

    def test_recovery_is_claimed_once_and_survives_snapshot(self):
        self.start_agent()
        self.snapshot()
        self.crash_and_recover()
        self.snapshot()
        self.assertTrue(self.path().exists(), "新 shell 未认领前不能删除条目")
        rc, lines = self.claim(300)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[1:], ["/proj", "claude --resume S1 --model opus", "auto"])
        self.assertEqual(self.claim(300)[0], 1, "同一 shell 不能重复认领")
        self.snapshot()
        self.assertTrue(self.path().exists(), "认领后宽限期内快照不能删除")
        entry = json.loads(self.path().read_text())
        self.assertIn('"shell_pid": 300,', self.path().read_text())
        self.assertTrue(entry["claimed"])

    def test_control_chars_and_bad_session_id_not_recorded(self):
        self.start_agent()
        with mock.patch.dict(hr.RESOLVERS, {"claude": lambda fg, v: {
                "session_id": "S1", "cwd": "/x\nrm -rf ~ #"}}):
            self.snapshot()
        self.assertFalse(self.path().exists())
        self.sessions[200] = "bad;id"
        self.snapshot()
        self.assertFalse(self.path().exists())

    def test_tampered_entry_not_claimed(self):
        self.start_agent()
        self.snapshot()
        entry = json.loads(self.path().read_text())
        entry["cwd"] = "/x\nevil"
        hr.write_atomic(self.path(), entry)
        self.crash_and_recover()
        self.assertEqual(self.claim(300)[0], 1)

    def test_dropped_values_not_persisted_and_private(self):
        self.start_agent(argv=("claude", "--model", "opus", "secret prompt", "--foo=bar"))
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        hr.main(["snapshot"])
        text = self.path().read_text()
        self.assertNotIn("secret prompt", text)
        self.assertNotIn("bar", text)
        self.assertEqual(self.path().stat().st_mode & 0o077, 0)

    def test_nested_shell_cannot_claim(self):
        self.start_agent()
        self.snapshot()
        self.crash_and_recover()
        self.assertEqual(self.claim(999)[0], 1)
        self.assertTrue(self.path().exists())

    def test_live_agent_is_not_claimed(self):
        self.start_agent()
        self.snapshot()
        self.fake.procs[555] = 7
        self.fake.terminals["t1"]["pid"] = 555
        self.assertEqual(self.claim(555)[0], 1)

    def test_unresolved_agent_inherits_entry(self):
        self.start_agent()
        self.snapshot()
        self.crash_and_recover()
        self.claim(300)
        self.fake.procs[400] = 10
        self.fake.terminals["t1"]["fg"] = {"argv": ["claude"], "name": "claude", "pid": 400, "cwd": "/proj"}
        self.snapshot()
        entry = json.loads(self.path().read_text())
        self.assertEqual((entry["session_id"], entry["agent_pid"], entry["claimed"]), ("S1", 400, False))

    def test_gone_terminal_removed(self):
        self.start_agent()
        self.snapshot()
        del self.fake.terminals["t1"]
        self.snapshot()
        self.assertFalse(self.path().exists())

    def test_core_unreachable_changes_nothing(self):
        self.start_agent()
        self.snapshot()
        before = self.path().read_text()

        def boom(*_):
            raise hr.HcError("down")
        with mock.patch.object(hr, "hc", boom):
            rc = hr.cmd_snapshot(argparse.Namespace(dry_run=False, quiet=True))
        self.assertEqual(rc, 1)
        self.assertEqual(self.path().read_text(), before)

    def test_stopped_core_is_not_started(self):
        self.start_agent()
        self.snapshot()
        calls = []
        with mock.patch.object(hr, "core_running", lambda: False), \
                mock.patch.object(hr, "hc", lambda *a: calls.append(a)):
            rc = hr.cmd_snapshot(argparse.Namespace(dry_run=False, quiet=True))
        self.assertEqual((rc, calls), (1, []))
        self.assertTrue(self.path().exists())


class ZshSnippetTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_description_is_not_expanded(self):
        """描述里的 $(...)、%F 等在 PROMPT_SUBST 下也不得展开或执行。"""
        import shutil
        import subprocess
        if not shutil.which("zsh"):
            self.skipTest("zsh 不可用")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            marker = tmp / "pwned"
            (tmp / "state" / "default").mkdir(parents=True)
            (tmp / "state" / "default" / "t1.json").write_text("{}")
            fake = tmp / "fake-claim"
            desc = f"x $(touch {marker}) %F{{red}}"
            fake.write_text("#!/bin/sh\nprintf '%s\\0%s\\0%s\\0%s' "
                            f"'{desc}' '{tmp}' 'claude --resume S1' confirm\n")
            fake.chmod(0o755)
            script = (f"setopt prompt_subst; source {self.ROOT / 'herdr-resume.zsh'}; "
                      "_herdr_resume_precmd")
            env = {**os.environ, "HC_TERMINAL_ID": "t1", "HERDR_RESUME_STATE": str(tmp / "state"),
                   "HERDR_RESUME_BIN": str(fake)}
            env.pop("HC_SESSION", None)
            out = subprocess.run(["zsh", "-f", "-i", "-c", script], capture_output=True,
                                 text=True, env=env, timeout=20)
            self.assertFalse(marker.exists(), "描述中的命令替换被执行了")
            self.assertIn("%F{red}", out.stdout + out.stderr)


class CliHelpTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def run_cli(self, *argv):
        import subprocess
        return subprocess.run(["bash", *argv], cwd=self.ROOT, capture_output=True, text=True,
                              env={**os.environ, "HOME": tempfile.gettempdir() + "/hr-nohome"})

    def test_entry_and_installer_help_contract(self):
        for script in ("herdr-resume", "install.sh"):
            ok = self.run_cli(script, "--help")
            self.assertEqual(ok.returncode, 0, script)
            for needle in ("用法：", "示例：", "副作用：", "退出状态："):
                self.assertIn(needle, ok.stdout, script)
            self.assertEqual(self.run_cli(script, "--help", "extra").returncode, 2, script)


class HelpTest(unittest.TestCase):
    def test_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hr.main(["--help"]), 0)
        for needle in ("用法：", "示例：", "副作用：", "退出状态："):
            self.assertIn(needle, out.getvalue())


if __name__ == "__main__":
    unittest.main()
