# -*- coding: utf-8 -*-
"""
순서 실행 블록(A, P, S)의 붙여넣기 시험 (tcsh 와 sh 가 있는 Linux, macOS 에서만 실행).

의사 터미널(pty)에 띄운 대화형 tcsh 와 bash 에 블록을 붙여넣듯이 보낸다. 줄바꿈을 LF 로 보내는 경우와 CR+LF 로 보내는
경우를 모두 시험한다. ssh, setsid, crontab, vim-cmd, esxcli, ssh-keygen 은 가짜 명령(fakeenv.py)이다.

- A 블록: 파일 7개 설치, sha256 확인(OK), ssh 키 생성과 ESX 에 넣을 한 줄 출력, 비밀번호 입력.
  다시 붙여넣으면 비밀번호를 묻지 않는다.
- P 블록: plan 을 올리고 tcrun check 를 실행한다.
- S 블록: plan 을 올리고 진행을 백그라운드로 시작한다. 터미널(셸)을 끝내도 진행은 계속되어 케이스가 끝난다.
- 판 불일치, 미설치, 블록이 중간에 깨진 경우는 시작하지 않는다.
"""
import datetime as dt
import os
import re
import shutil
import stat
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakeenv  # noqa: E402
from fakeenv import core  # noqa: E402
from test_blocks_shell import run_pty  # noqa: E402

HAVE = bool(fakeenv.HAVE and shutil.which("tcsh"))
GEN = dt.datetime(2026, 11, 20, 10, 0, 0)
RID = "20261120-100000"
SHELLS = {
    "tcsh": ([shutil.which("tcsh") or "/bin/tcsh", "-f"], "set prompt='TCSH> '\n", "TCSH> "),
    "bash": (["/bin/bash", "--norc", "--noprofile", "-i"], "PS1='BASH$ '; PS2='> '\n", r"BASH\$ "),
}


@unittest.skipUnless(HAVE, "tcsh, perl, sh 가 있는 Linux/macOS 에서만 실행")
class SeqBlockTests(unittest.TestCase):
    def setUp(self):
        self.e = fakeenv.FakeEnv(install=False)
        e = self.e
        e.add_vm("esx01ev01", crontab="30 9 * * 1 /home/tester/existing.sh\n")
        e.add_vm("esx01ev02")
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])
        run = [{"app": "sim", "name": "job_a", "desc": "", "cmd": e.job_cmd("simA", 60), "proc": "simA"}]
        mon = [{"app": "m", "name": "gmon", "desc": "", "where": "VM", "start": e.mon_cmd("start", "g"), "stop": e.mon_cmd("stop", "g")}]
        prof = [{"name": "same", "target": "VM", "text": "numa.nodeAffinity = 0", "desc": ""},
                {"name": "numa", "target": "ESX", "text": "Numa.LocalityWeightActionAffinity = 130", "desc": ""}]
        c = core.new_case("esx01", "", 0)
        c.update(name="paste1", desc="한글 설명", group="그룹", esx_profiles=["numa"],
                 vms=[{"name": "esx01ev01", "job": "job_a", "monitors": ["gmon"], "profiles": ["same"], "end_ref": True},
                      {"name": "esx01ev02", "job": "job_a", "monitors": [], "profiles": [], "end_ref": False}])
        self.res = core.generate([c], run, mon, e.settings(), now=GEN, write=False, profiles=prof)
        self.assertEqual(self.res["errors"], [])

    def tearDown(self):
        self.e.close()

    def paste(self, shell, steps, crlf=False):
        argv, set_prompt, prompt_re = SHELLS[shell]
        conv = (lambda t: t.replace("\n", "\r\n")) if crlf else (lambda t: t)
        steps = [(w, idle, conv(t)) for w, idle, t in steps]
        last = steps[-1]
        steps[-1] = (last[0], last[1], last[2] + "exit\n")
        env = dict(self.e.env)
        return run_pty(argv, env, self.e.jump, [(None, 0, set_prompt), (prompt_re, 0.5, "")] + steps, timeout=180)

    def tfile(self, name):
        return os.path.join(self.e.tcgen, name)

    def read(self, name):
        with open(self.tfile(name)) as f:
            return f.read()

    def install(self, shell, crlf):
        out = self.paste(shell, [(None, 0, self.res["setup"]), (r"VM password: \Z", 0.3, fakeenv.TEST_PW + "\r"),
                                 (r"saved: /", 0.3, "")], crlf)
        for name, text in core.jump_files():
            self.assertEqual(self.read(name), text, "%s 의 내용이 다름\n%s" % (name, out[-3000:]))
        for name in ("askpass", "vmssh", "setpw", "esxssh", "postsetup", "tcrun"):
            self.assertEqual(stat.S_IMODE(os.stat(self.tfile(name)).st_mode), 0o700, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.tfile("tcgen_apply.py")).st_mode), 0o600)
        self.assertEqual(self.read("vmpw"), fakeenv.TEST_PW + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.tfile("vmpw")).st_mode), 0o600)
        self.assertNotIn(fakeenv.TEST_PW, out, "비밀번호가 화면에 보이면 안 됨")
        self.assertEqual(len(re.findall(r"VM password: \r?\n", out)), 1)
        self.assertRegex(out, r"OK      %s\r?\n" % re.escape(self.tfile("tcrun")))
        self.assertRegex(out, r"OK      %s\r?\n" % re.escape(self.tfile("tcgen_apply.py")))
        self.assertNotIn("FAILED  /", out)
        self.assertRegex(out, r"created: %s \(ssh key for ESX hosts\)" % re.escape(self.tfile("id_esx")))
        pub = self.read("id_esx.pub").strip()
        self.assertRegex(out, r"(?m)^echo '%s' >> /etc/ssh/keys-root/authorized_keys\r?$" % re.escape(pub))
        self.assertIn("sh ~/.tcgen/esxssh <esx host> hostname", out)
        self.assertFalse(os.path.exists(self.tfile("tcrun.new")))
        self.assertTrue(os.path.isdir(self.tfile("runs")))
        return out

    def flow(self, shell, crlf, again=False):
        """A 블록 설치, P 블록, S 블록(케이스가 끝날 때까지). again 이면 A 와 S 를 다시 붙여넣는 경우도 본다
        (A 블록 한 번에 1분쯤 걸려서 한 가지 셸에서만 본다)."""
        e = self.e
        self.install(shell, crlf)
        if again:
            # A 블록을 다시 붙여넣으면 비밀번호를 묻지 않고 키도 그대로 둔다
            key = self.read("id_esx")
            out = self.paste(shell, [(None, 0, self.res["setup"]), (r"To change it: sh ~/.tcgen/setpw\r?\n", 0.5, "")], crlf)
            self.assertNotRegex(out, r"VM password: \r?\n")
            self.assertRegex(out, r"== VM password: already saved in %s\." % re.escape(self.tfile("vmpw")))
            self.assertRegex(out, r"kept:    %s" % re.escape(self.tfile("id_esx")))
            self.assertEqual(self.read("id_esx"), key)
            self.assertEqual(self.read("vmpw"), fakeenv.TEST_PW + "\n")
            self.assertEqual(self.read("tcrun"), dict(core.jump_files())["tcrun"])
        # P 블록
        out = self.paste(shell, [(None, 0, self.res["check"]), (r"saved: /\S+check\.log\r?\n", 0.5, "")], crlf)
        plan = os.path.join(e.tcgen, "runs", RID, "plan")
        with open(plan, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.res["plan"], "plan 이 그대로 올라감(한글 설명 포함)")
        for word in ("[ok] esx esx01 (esx01): ssh key login works", "[ok] settings: every value is already as wanted.",
                     "[ok] vm esx01ev01 (esx01ev01): ssh login works", "[ok] vm esx01ev02 (esx01ev02): ssh login works",
                     "CHECK RESULT: OK"):
            self.assertIn(word, out)
        self.assertNotIn("[PROBLEM]", out)
        self.assertEqual(e.state(RID, "esx01"), {})
        self.assertTrue(os.path.exists(os.path.join(e.log, "tcrun-%s-check.log" % RID)))
        # S 블록: 셸을 끝내도(터미널을 닫아도) 진행은 계속되어 케이스가 끝난다
        cron = e.start_cron()
        out = self.paste(shell, [(None, 0, self.res["run"]), (r"files in /\S+ : tcrun-", 0.5, "")], crlf)
        self.assertRegex(out, r"started: run %s esx esx01   log: %s/tcrun-%s-esx01\.log" % (RID, re.escape(e.log), RID))
        self.assertIn("status: sh ~/.tcgen/tcrun status", out)
        self.assertNotIn("====", out.replace("==== tcrun on this jump VM", "").replace("==== plan FAILED", ""))
        e.wait_for(lambda: e.state(RID, "esx01").get("state") in ("done", "paused", "stopped"), seconds=180, what="진행 완료")
        log = e.log_text(RID, "esx01")
        self.assertEqual(e.state(RID, "esx01")["state"], "done", log)
        self.assertIn("==== case paste1 done", log)
        self.assertEqual(len(cron.fired), 4, cron.fired)
        self.assertEqual(e.vm_crontab("esx01ev01"), ["30 9 * * 1 /home/tester/existing.sh"])
        rep = e.log_text(RID, "esx01", "-perfreport_cases.csv")
        self.assertTrue(rep.startswith("﻿테스트 그룹,케이스명,설명,호스트 이름,"))
        self.assertIn("\r\n그룹,paste1,한글 설명,esx01,2026-11-20 ", rep)
        if again:
            # 같은 S 블록을 다시 붙여넣어도 다시 시작하지 않는다
            out = self.paste(shell, [(None, 0, self.res["run"]), (r"files in /\S+ : tcrun-", 0.5, "")], crlf)
            self.assertIn("==== esx01: run %s was already started on this jump VM. Not started again." % RID, out)
            self.assertEqual(len(cron.fired), 4)

    def test_flow_tcsh_lf(self):
        self.flow("tcsh", False, again=True)

    def test_flow_tcsh_crlf(self):
        self.flow("tcsh", True)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "bash 없음")
    def test_flow_bash_lf(self):
        self.flow("bash", False)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "bash 없음")
    def test_flow_bash_crlf(self):
        self.flow("bash", True)

    def test_version_mismatch_and_not_installed(self):
        """(j) 설치된 tcrun 의 판이 생성기와 다르거나 tcrun 이 없으면 시작하지 않고 A 블록을 다시 붙여넣으라고 알린다."""
        e = self.e
        for block in ("run", "check"):
            out = self.paste("tcsh", [(None, 0, self.res[block]), (r"Paste block A again\.\r?\n", 0.5, "")])
            self.assertRegex(out, r"(?m)^==== tcrun on this jump VM is not installed, tcgen is %s\. Nothing was started\. "
                                  r"Paste block A again\.\r?$" % re.escape(core.VERSION))
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID)))
        e.install_jump()
        text = self.read("tcrun").replace("\nTCRUN_VERSION=%s\n" % core.VERSION, "\nTCRUN_VERSION=1.9\n")
        with open(self.tfile("tcrun"), "w") as f:
            f.write(text)
        cron = e.start_cron()
        for shell in ("tcsh", "bash"):
            out = self.paste(shell, [(None, 0, self.res["run"]), (r"Paste block A again\.\r?\n", 0.5, "")], crlf=(shell == "bash"))
            self.assertRegex(out, r"(?m)^==== tcrun on this jump VM is 1\.9, tcgen is %s\. Nothing was started\. "
                                  r"Paste block A again\.\r?$" % re.escape(core.VERSION))
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID)))
        self.assertFalse(os.path.exists(os.path.join(e.root, "ssh_calls.txt")))
        self.assertEqual(cron.fired, [])

    def test_damaged_blocks_install_and_start_nothing(self):
        """블록이 붙여넣는 중에 깨지면(sha256 이 다르면) 설치하지 않고, 시작하지 않는다."""
        e = self.e
        e.install_jump()
        old = self.read("tcrun")
        lines = self.res["setup"].split("\n")
        first = next(i for i, x in enumerate(lines) if "<< TCB64" in x)
        del lines[first + 3]                                   # tcrun 의 base64 줄 하나가 빠진 경우
        out = self.paste("tcsh", [(None, 0, "\n".join(lines)), (r"To change it: sh ~/.tcgen/setpw\r?\n", 0.5, "")])
        self.assertRegex(out, r"(?m)^FAILED  %s \(sha256 differs\. Paste block A again\)\r?$" % re.escape(self.tfile("tcrun")))
        self.assertRegex(out, r"OK      %s\r?\n" % re.escape(self.tfile("tcgen_apply.py")))
        self.assertEqual(self.read("tcrun"), old, "깨진 파일로 바꾸지 않음")
        self.assertFalse(os.path.exists(self.tfile("tcrun.new")))
        lines = self.res["run"].split("\n")
        first = next(i for i, x in enumerate(lines) if "<< TCB64" in x)
        lines[first + 2] = lines[first + 2][:-8] + "AAAAAAAA"
        out = self.paste("bash", [(None, 0, "\n".join(lines)), (r"Paste this block again\.\r?\n", 0.5, "")], crlf=True)
        self.assertRegex(out, r"(?m)^==== plan FAILED \(sha256 differs\)\. Nothing was started\. Paste this block again\.\r?$")
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID, "plan")))
        self.assertEqual(e.state(RID, "esx01"), {})


if __name__ == "__main__":
    unittest.main()
