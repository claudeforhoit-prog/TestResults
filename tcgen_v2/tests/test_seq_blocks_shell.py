# -*- coding: utf-8 -*-
"""
블록 A, P, S 의 붙여넣기 시험 (tcsh 와 sh 가 있는 Linux, macOS 에서만 실행).

의사 터미널(pty)에 띄운 대화형 tcsh 와 bash 에 블록을 붙여넣듯이 보낸다. 줄바꿈을 LF 로 보내는 경우와 CR+LF 로 보내는
경우를 모두 시험한다. ssh, setsid, crontab, vim-cmd, esxcli, ssh-keygen 은 가짜 명령(fakeenv.py)이다.

- A 블록: 파일 7개 설치, sha256 확인(OK), ssh 키 생성과 ESX 에 넣을 한 줄 출력, 비밀번호 입력.
  다시 붙여넣으면 비밀번호를 묻지 않는다.
- P 블록: plan 을 올리고 tcrun check 를 실행한다.
- S 블록: plan 을 올리고 진행을 백그라운드로 시작한다. 터미널(셸)을 끝내도 진행은 계속된다: 잡을 crontab 에 등록하고,
  잡이 적은 시각에 시작하고, 마지막 잡 시작 1분 뒤에 진행이 끝난다(v2.12).
- 판 불일치, 미설치, 블록이 중간에 깨진 경우는 시작하지 않는다.
"""
import datetime as dt
import os
import re
import shutil
import stat
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakeenv  # noqa: E402

GREEN = "\x1b[1;32m"


def plain(text):
    """터미널 출력에서 색 글자(ESC [ ... m)를 뺀 글. v2.15 부터 터미널의 VM 이름은 초록색이다."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
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
        # 생성기는 시작 시각을 이 PC 의 시간대로 초(epoch)로 바꾼다. 가짜 환경의 시계는 KST 이므로 시험 프로세스도 KST 로 맞춘다
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.build()

    def build(self, jdir=False):
        self.e = fakeenv.FakeEnv(install=False, jdir=jdir)
        e = self.e
        e.add_vm("esx01ev01", crontab="30 9 * * 1 /home/tester/existing.sh\n")
        e.add_vm("esx01ev02")
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])
        run = [{"app": "sim", "name": "job_a", "desc": "", "cmd": e.job_cmd("simA", 60), "proc": "simA"}]
        mon = [{"app": "m", "name": "gmon", "desc": "", "where": "VM", "start": e.mon_cmd("start", "g"), "stop": e.mon_cmd("stop", "g")}]
        prof = [{"name": "same", "target": "VM", "text": "numa.nodeAffinity = 0", "desc": ""},
                {"name": "numa", "target": "ESX", "text": "Numa.LocalityWeightActionAffinity = 130", "desc": ""}]
        c = core.new_case("esx01", "", 0)
        c.update(name="paste1", desc="한글 설명", group="그룹", esx_profiles=["numa"], seq_start="2026-11-20 16:00:00",
                 vms=[{"name": "esx01ev01", "job": "job_a", "monitors": ["gmon"], "profiles": ["same"], "end_ref": True},
                      {"name": "esx01ev02", "job": "job_a", "monitors": [], "profiles": [], "end_ref": False}])
        self.gen_args = (c, run, mon, prof)
        self.res = core.generate([c], run, mon, e.settings(), now=GEN, write=False, profiles=prof)
        self.assertEqual(self.res["errors"], [])
        self.rid = self.res["run_id"]

    def make(self, lead_min=30):
        """P 블록과 S 블록을 지금(가상 시계) 기준으로 다시 만든다: 잡 시작은 lead_min 분 뒤. A 블록을 붙여넣는 데 걸리는 시간이
        일정하지 않으므로, 붙여넣은 뒤에 시작 시각을 정한다. A 블록의 내용은 plan 과 무관해 그대로다."""
        c, run, mon, prof = self.gen_args
        now = dt.datetime.fromtimestamp(self.e.now()).replace(microsecond=0)
        at = (now + dt.timedelta(minutes=lead_min + 1)).replace(second=0)
        c["seq_start"] = core.fmt_dt(at)
        setup = self.res["setup"]
        self.res = core.generate([c], run, mon, self.e.settings(), now=now, write=False, profiles=prof)
        self.assertEqual(self.res["errors"], [])
        self.assertEqual(self.res["setup"], setup, "A 블록은 plan 과 무관하다")
        self.rid, self.at = self.res["run_id"], at

    def tearDown(self):
        self.e.close()
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def paste(self, shell, steps, crlf=False, tty=False):
        """tty: tcrun start 의 확인 질문을 터미널(/dev/tty)에서 받는다(TCRUN_TEST_TTY 를 쓰지 않음). 답은 steps 에 넣는다."""
        argv, set_prompt, prompt_re = SHELLS[shell]
        conv = (lambda t: t.replace("\n", "\r\n")) if crlf else (lambda t: t)
        steps = [(w, idle, conv(t)) for w, idle, t in steps]
        last = steps[-1]
        steps[-1] = (last[0], last[1], last[2] + "exit\n")
        env = dict(self.e.env)
        if tty:
            env.pop("TCRUN_TEST_TTY", None)
        return run_pty(argv, env, self.e.jump, [(None, 0, set_prompt), (prompt_re, 0.5, "")] + steps, timeout=180)

    def tfile(self, name):
        return os.path.join(self.e.tcgen, name)

    def read(self, name):
        with open(self.tfile(name)) as f:
            return f.read()

    def install(self, shell, crlf):
        out = self.paste(shell, [(None, 0, self.res["setup"]), (r"VM password: \Z", 0.3, fakeenv.TEST_PW + "\r"),
                                 (r"saved: /", 0.3, "")], crlf)
        for name, text in core.jump_files(self.e.jdir or core.DEFAULT_JUMP_DIR):
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
        # P 블록 (plan 은 지금 만든다: 잡 시작은 가상 시계로 30분 뒤)
        self.make()
        RID = self.rid
        raw = self.paste(shell, [(None, 0, self.res["check"]), (r"saved: /\S+check\.log\r?\n", 0.5, "")], crlf)
        self.assertIn("[ok] vm %sesx01ev01\x1b[0m (esx01ev01): ssh login works" % GREEN, raw, "점검 출력의 VM 이름은 터미널에서 초록색")
        out = plain(raw)
        plan = os.path.join(e.tcgen, "runs", RID, "plan")
        with open(plan, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.res["plan"], "plan 이 그대로 올라감(한글 설명 포함)")
        for word in ("[ok] esx esx01 (esx01): ssh key login works", "[ok] settings and VM power: already as wanted.",
                     "[ok] start times: the jobs are registered for the start times of the plan. tcrun does not change them",
                     "[ok] vm esx01ev01 (esx01ev01): ssh login works", "[ok] vm esx01ev02 (esx01ev02): ssh login works",
                     "CHECK RESULT: OK"):
            self.assertIn(word, out)
        self.assertNotIn("[PROBLEM]", out)
        self.assertEqual(e.state(RID, "esx01"), {})
        self.assertTrue(os.path.exists(os.path.join(e.log, "tcrun-%s-check.log" % RID)))
        # S 블록: 셸을 끝내도(터미널을 닫아도) 진행은 계속된다. 잡을 등록하고, 적은 시각에 잡이 시작하고, 1분 뒤에 진행이 끝난다
        cron = e.start_cron()
        # 붙여넣으면 케이스 목록을 보이고 터미널에서 yes 를 받은 뒤에 시작한다(사용자 요청 2026-11-20 "확인작업필수")
        raw = self.paste(shell, [(None, 0, self.res["run"]), (r"Type yes or no: \Z", 0.3, "yes\r"),
                                 (r"files in /\S+ : tcrun-", 0.5, "")], crlf, tty=True)
        out = plain(raw)
        text = self.at.strftime("%Y-%m-%d %H:%M")
        for word in ("run %s: 1 case. Check them before they are registered in crontab." % RID,
                     "case 1/1  paste1   job start %s" % text, "  vm esx01ev01", "  vm esx01ev02",
                     "Register these cases in crontab? Type yes or no: "):
            self.assertIn(word, out)
        self.assertLess(out.index("case 1/1  paste1"), out.index("Type yes or no: "))
        self.assertIn("  vm %sesx01ev01\x1b[0m" % GREEN, raw, "확인 목록의 VM 이름은 터미널에서 초록색")
        self.assertRegex(out, r"started: run %s esx esx01   log: %s/tcrun-%s-esx01\.log" % (RID, re.escape(e.log), RID))
        self.assertIn("status: sh %s/tcrun status" % e.tcgen, out)
        self.assertIn("tcrun-%s-jobs.csv (which jobs are registered in crontab)" % RID, out)
        self.assertNotIn("====", out.replace("==== tcrun on this jump VM", "").replace("==== plan FAILED", ""))
        e.wait_for(lambda: e.state(RID, "esx01").get("state") in ("done", "paused", "stopped"), seconds=240, what="진행 완료")
        log = e.log_text(RID, "esx01")
        self.assertEqual(e.state(RID, "esx01")["state"], "done", log)
        self.assertIn("==== case paste1 done", log)
        text = self.at.strftime("%Y-%m-%d %H:%M")
        self.assertEqual([(r["case"], r["vm"], r["job_start"], r["crontab"]) for r in e.jobs_rows(RID)],
                         [("paste1", "esx01ev01", text, "registered"), ("paste1", "esx01ev02", text, "registered")], log)
        # 모니터링 시작 1번(잡 시작 1분 전. 시험 설정)과 잡 2번이 적은 시각에 실행됐다. 마지막 케이스라 모니터링 중지 줄은 없다
        job_at = time.mktime(self.at.timetuple())
        self.assertEqual(sorted(t for h, t, line in cron.fired), [job_at - 60, job_at, job_at], cron.fired)
        tab = e.vm_crontab("esx01ev01")
        self.assertEqual([x for x in tab if " # tcgen " not in x], ["30 9 * * 1 /home/tester/existing.sh"], "원래 있던 줄은 그대로")
        self.assertEqual(len([x for x in tab if " # tcgen paste1 esx01ev01" in x]), 2, "등록한 줄은 남는다(다음 실행이나 tcrun stop 이 지움)")
        rep = e.log_text(RID, "esx01", "-perfreport_cases.csv")
        self.assertTrue(rep.startswith("\ufeff테스트 그룹,케이스명,설명,호스트 이름,"))
        self.assertIn("\r\n그룹,paste1,한글 설명,esx01,%s:00," % text, rep)
        if again:
            # 같은 S 블록을 다시 붙여넣어도 다시 시작하지 않는다
            out = self.paste(shell, [(None, 0, self.res["run"]), (r"files in /\S+ : tcrun-", 0.5, "")], crlf, tty=True)
            self.assertIn("==== esx01: run %s was already started on this jump VM. Not started again." % RID, out)
            self.assertNotIn("Type yes or no", out, "이미 시작한 실행은 묻지 않음")
            self.assertEqual(len(cron.fired), 3)

    def test_flow_tcsh_lf(self):
        self.flow("tcsh", False, again=True)

    def test_flow_tcsh_crlf(self):
        self.flow("tcsh", True)

    def test_flow_bash_lf(self):
        self.flow("bash", False)

    def test_flow_bash_crlf(self):
        self.flow("bash", True)

    def test_work_folder_outside_the_home(self):
        """작업 폴더를 홈 밖에 두면 A 블록이 그 폴더에 설치하고, ~/.tcgen 에 있던 ESX 키와 VM 비밀번호를 복사한다(다시 묻지 않음).
        P, S 블록도 그 폴더를 쓴다."""
        self.e.close()
        self.build(jdir=True)
        e = self.e
        old = os.path.join(e.jump, ".tcgen")
        os.makedirs(old)
        for name, text in (("id_esx", "-----BEGIN OLD KEY-----\n"), ("id_esx.pub", "ssh-rsa OLDKEY tester@jump\n"),
                           ("vmpw", fakeenv.TEST_PW + "\n")):
            with open(os.path.join(old, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(old, name), 0o600)
        out = self.paste("tcsh", [(None, 0, self.res["setup"]), (r"To change it: sh %s/setpw\r?\n" % re.escape(e.tcgen), 0.5, "")])
        for name in ("id_esx", "id_esx.pub", "vmpw"):
            self.assertRegex(out, r"copied:  %s/%s -> %s/%s" % (re.escape(old), name, re.escape(e.tcgen), name))
        self.assertEqual(self.read("id_esx.pub"), "ssh-rsa OLDKEY tester@jump\n", "ESX 에 등록한 키를 그대로 씀")
        self.assertNotRegex(out, r"VM password: \r?\n")
        self.assertRegex(out, r"kept:    %s" % re.escape(self.tfile("id_esx")))
        for name, text in core.jump_files(e.jdir):
            self.assertEqual(self.read(name), text, name)
        self.assertIn("sh %s/esxssh <esx host> hostname" % e.tcgen, out)
        self.make()
        RID = self.rid
        out = self.paste("tcsh", [(None, 0, self.res["check"]), (r"saved: /\S+check\.log\r?\n", 0.5, "")])
        self.assertIn("CHECK RESULT: OK", out)
        self.assertTrue(os.path.exists(os.path.join(e.tcgen, "runs", RID, "plan")))
        cron = e.start_cron()
        out = self.paste("tcsh", [(None, 0, self.res["run"]), (r"files in /\S+ : tcrun-", 0.5, "")])
        self.assertIn("status: sh %s/tcrun status" % e.tcgen, out)
        e.wait_for(lambda: e.state(RID, "esx01").get("state") in ("done", "paused", "stopped"), seconds=240, what="진행 완료")
        self.assertEqual(e.state(RID, "esx01")["state"], "done", e.log_text(RID, "esx01"))
        self.assertEqual(len(cron.fired), 3, cron.fired)
        self.assertEqual(sorted(os.listdir(old)), ["id_esx", "id_esx.pub", "vmpw"], "홈의 ~/.tcgen 에는 새로 쓰지 않음")

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
        self.assertRegex(out, r"(?m)^FAILED  %s \(sha256 differs\. Paste block A again\. Check the free space and quota of %s\)\r?$"
                         % (re.escape(self.tfile("tcrun")), re.escape(e.tcgen)))
        self.assertRegex(out, r"OK      %s\r?\n" % re.escape(self.tfile("tcgen_apply.py")))
        self.assertEqual(self.read("tcrun"), old, "깨진 파일로 바꾸지 않음")
        self.assertFalse(os.path.exists(self.tfile("tcrun.new")))
        lines = self.res["run"].split("\n")
        first = next(i for i, x in enumerate(lines) if "<< TCB64" in x)
        lines[first + 2] = lines[first + 2][:-8] + "AAAAAAAA"
        out = self.paste("bash", [(None, 0, "\n".join(lines)), (r"paste this block again\.\r?\n", 0.5, "")], crlf=True)
        self.assertRegex(out, r"(?m)^==== plan FAILED \(sha256 differs\)\. Nothing was started\. Check the free space and quota of %s, "
                              r"then paste this block again\.\r?$" % re.escape(e.tcgen))
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID, "plan")))
        self.assertEqual(e.state(RID, "esx01"), {})


if __name__ == "__main__":
    unittest.main()
