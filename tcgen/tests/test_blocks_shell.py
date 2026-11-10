# -*- coding: utf-8 -*-
"""
붙여넣기 블록 시험 (tcsh 와 sh 가 있는 Linux, macOS 에서만 실행. Windows 에서는 건너뛴다).

- 점프 VM: 의사 터미널(pty)에 띄운 대화형 tcsh 와 bash 에 블록을 붙여넣듯이 보낸다.
  줄바꿈을 LF 로 보내는 경우와 CR+LF 로 보내는 터미널을 모두 시험한다.
  ssh, setsid, crontab 은 가짜 명령으로 바꾼다. 가짜 ssh 는 askpass 로 받은 비밀번호를 확인하고,
  제어 터미널이 없는지(실제 ssh 가 askpass 를 쓰는 조건) 확인한 뒤 VM 별 홈 폴더에서 sh -s 를 실행한다.
- ESX: 경로만 시험 폴더로 바꾼 ESX 블록을 dash(없으면 sh)로 실행한다.
- cron 실행 흉내: VM 줄은 cronie 처럼 \\% -> %, % -> 줄바꿈 처리 후 홈 폴더에서 sh -c,
  ESX 줄은 busybox crond 처럼 그대로 sh -c 로 실행해 결과 파일 이름과 인자를 확인한다.
"""
import datetime as dt
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tcgen_core as core  # noqa: E402

HAVE = (os.name == "posix" and shutil.which("tcsh") and shutil.which("sh") and shutil.which("awk")
        and shutil.which(sys.executable))
TEST_PW = "test-pw!'$x \"q"          # ! 와 따옴표가 있어도 저장되는지 확인
NOW = dt.datetime(2026, 11, 9, 15, 30, 12)

FAKE_SSH = r"""#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -G) echo "stricthostkeychecking accept-new"; exit 0 ;;
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
t=$1; shift
h=${t#*@}
if ( : < /dev/tty ) 2>/dev/null; then echo "fake ssh: controlling tty present" >&2; exit 254; fi
[ -t 0 ] && { echo "fake ssh: stdin is a tty" >&2; exit 253; }
[ -n "$SSH_ASKPASS" ] || { echo "fake ssh: SSH_ASKPASS not set" >&2; exit 255; }
pw=`"$SSH_ASKPASS" "$t's password: "`
[ "$pw" = "$FAKE_PW" ] || { echo "fake ssh: Permission denied" >&2; exit 255; }
HOME=$FAKE_VMS/$h; export HOME; mkdir -p "$HOME"; cd "$HOME" || exit 1
FAKE_CRONTAB=$HOME/crontab.txt; export FAKE_CRONTAB
echo "$t" >> "$FAKE_VMS/ssh_calls.txt"
exec "$@"
"""

FAKE_SETSID = """#!/bin/sh
exec "%s" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
""" % sys.executable

FAKE_CRONTAB = r"""#!/bin/sh
F=${FAKE_CRONTAB:?}
case "$1" in
  -l) if [ -f "$F" ]; then cat "$F"; else echo "no crontab for user" >&2; exit 1; fi ;;
  -*) echo "crontab: bad option" >&2; exit 1 ;;
  *) awk 'NF && $1 !~ /^#/ && NF < 6 { bad = 1 } END { exit bad }' "$1" || { echo "crontab: errors in crontab file" >&2; exit 1; }
     cat "$1" > "$F" ;;
esac
"""

SCRIPT = """#!/bin/sh
printf '%s|%s|%s\\n' "$(basename "$0")" "$(pwd)" "$*"
"""

ESX_DEFAULT = """#min hour day mon dow command
1    1    *   *   *   /sbin/tmpwatch.py
1    *    *   *   *   /sbin/auto-backup.sh
0    *    *   *   *   /usr/lib/vmware/vmksummary/log-heartbeat.py
"""


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def cronie_split(cmd):
    """cronie do_command.c 와 같은 % 처리: \\% -> %, 첫 % 이후는 표준 입력(% -> 줄바꿈)."""
    out, i, stdin = [], 0, None
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and i + 1 < len(cmd) and cmd[i + 1] == "%":
            out.append("%")
            i += 2
            continue
        if ch == "%":
            stdin = cmd[i + 1:].replace("%", "\n")
            break
        out.append(ch)
        i += 1
    return "".join(out), stdin


def cron_command(line):
    return line.split(None, 5)[5]


def run_pty(argv, env, cwd, steps, timeout=90):
    """의사 터미널에서 argv 를 실행한다. steps = [(기다릴 정규식 또는 None, 출력이 멈출 때까지 기다릴 초, 보낼 글)].
    출력은 별도 스레드가 계속 읽고, 입력은 막히면 기다리는 방식으로 쓴다. 끝나면 프로그램이 끝날 때까지 출력을 모은다.
    보낼 글은 한 줄씩 보내고, 셸이 그 줄을 받아 출력이 생기면(최대 1초) 다음 줄을 보낸다. macOS 의사 터미널에서는
    셸 줄 편집기가 줄마다 입력 모드를 바꿀 때 대기 중이던 많은 입력이 섞이는 경우가 있어서다(줄 편집기 없는 cat 은
    같은 글을 한 번에 받아도 정상). Linux 는 모드를 바꿀 때 대기 입력을 다시 처리하지 않는다."""
    import pty
    import threading
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    out = bytearray()
    lock = threading.Lock()
    eof = threading.Event()
    deadline = time.time() + timeout

    def reader():
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                eof.set()
                return
            with lock:
                out.extend(data)

    def snapshot(mark=0):
        with lock:
            return bytes(out[mark:])

    def size():
        with lock:
            return len(out)

    def tail():
        return snapshot()[-3000:].decode("utf-8", "replace")

    def writer(data, done):
        try:
            for line in data.splitlines(True):
                before = size()
                pos = 0
                while pos < len(line):
                    pos += os.write(fd, line[pos:pos + 256])
                until = time.time() + 1.0
                while size() == before and time.time() < until and not eof.is_set():
                    time.sleep(0.005)
        except OSError:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    mark = 0          # 기다릴 출력은 직전 단계 글을 보내기 시작한 뒤의 출력에서 찾는다
    try:
        for wait_re, idle, text in steps:
            if wait_re:
                rx = re.compile(wait_re.encode())
                while not rx.search(snapshot(mark)):
                    if time.time() > deadline or eof.is_set():
                        raise AssertionError("waiting for %r. output tail:\n%s" % (wait_re, tail()))
                    time.sleep(0.05)
            last, since = size(), time.time()
            while idle and time.time() - since < idle:
                time.sleep(0.05)
                n = size()
                if n != last:
                    last, since = n, time.time()
                if eof.is_set():
                    raise AssertionError("pty closed. output tail:\n" + tail())
            if text:
                mark = size()
                done = threading.Event()
                threading.Thread(target=writer, args=(text.encode(), done), daemon=True).start()
                while not done.wait(0.1):
                    if time.time() > deadline:
                        raise AssertionError("write timeout. output tail:\n" + tail())
        while not eof.wait(0.2):
            if time.time() > deadline:
                raise AssertionError("program did not exit. output tail:\n" + tail())
        os.waitpid(pid, 0)
    except BaseException:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass
        raise
    return snapshot().decode("utf-8", "replace")


@unittest.skipUnless(HAVE, "tcsh, sh 가 있는 Linux/macOS 에서만 실행")
class ShellBlockTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tcgen_sh_")
        r = self.root
        self.fakebin = os.path.join(r, "fakebin")
        self.bin = os.path.join(r, "bin")
        self.jump = os.path.join(r, "jump")
        self.vms = os.path.join(r, "vms")
        self.esx = os.path.join(r, "esx")
        self.log = os.path.join(r, "log")
        for d in (self.fakebin, self.bin, self.jump, self.vms, self.log, os.path.join(self.esx, "crontabs"),
                  os.path.join(self.esx, "tmp"), os.path.join(self.jump, "work", "deep")):
            os.makedirs(d, exist_ok=True)
        write_exec(os.path.join(self.fakebin, "ssh"), FAKE_SSH)
        write_exec(os.path.join(self.fakebin, "setsid"), FAKE_SETSID)
        write_exec(os.path.join(self.fakebin, "crontab"), FAKE_CRONTAB)
        for n in ("run_a.sh", "run_b.sh", "start_mon.sh", "stop_mon.sh", "host_mon.sh"):
            write_exec(os.path.join(self.bin, n), SCRIPT)
        with open(os.path.join(self.esx, "crontabs", "root"), "w") as f:
            f.write(ESX_DEFAULT)
        os.chmod(os.path.join(self.esx, "crontabs", "root"), 0o444)
        self.run_cmds = [
            {"app": "AppA", "name": "job_a", "desc": "1 job",
             "cmd": "%s/run_a.sh -o x%%y >> %s/run_a-<vm이름>-<실행시각>.log 2>&1" % (self.bin, self.log)},
            {"app": "AppB", "name": "job_b4", "desc": "4 jobs", "cmd": "%s/run_b.sh -n 4 -d \"`date +%%H`\"" % self.bin},
        ]
        self.mon_cmds = [
            {"app": "mon", "name": "guest_perf", "desc": "", "where": "VM",
             "start": "%s/start_mon.sh >> %s/mon-<vm이름>-<실행시각>.log" % (self.bin, self.log),
             "stop": "%s/stop_mon.sh" % self.bin},
            {"app": "mon", "name": "host_mon", "desc": "", "where": "ESX",
             "start": "/bin/sh %s/host_mon.sh start <vm이름> <실행시각> '%%H' >> %s/esx-<vm이름>-<실행시각>.log" % (self.bin, self.log),
             "stop": "/bin/sh %s/host_mon.sh stop" % self.bin},
        ]
        mons = [{"name": "guest_perf", "targets": ["PVM", "SVM"]}, {"name": "host_mon", "targets": ["ESX"]}]
        self.cases = []
        for name, svm_job, mode in (("TC-a", "(없음)", "auto"), ("TC-b4", "job_b4", "auto"), ("TC-b4-r2", "job_b4", "none")):
            self.cases.append({"name": name, "esx": "esx01", "pvm": "esx01ev01", "svm": "esx01ev02", "pvm_job": "job_a",
                               "svm_job": svm_job, "interval": "12:00", "start": "2026-11-20 00:00:00" if not self.cases else "",
                               "monitors": [dict(m, targets=list(m["targets"])) for m in mons], "stop_mode": mode})
        core.recalc_starts(self.cases)
        self.cases = [core._clean_case(c) for c in self.cases]      # 예전 형식(PVM, SVM)을 VM 목록 형식으로
        self.settings = core.resolve_paths(r, core.load_settings(r))
        self.env = {"HOME": self.jump, "PATH": self.fakebin + ":/usr/bin:/bin", "TERM": "dumb", "FAKE_PW": TEST_PW,
                    "FAKE_VMS": self.vms, "LANG": "C", "USER": "tester"}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def gen(self, cases):
        res = core.generate(cases, self.run_cmds, self.mon_cmds, self.settings, now=NOW, write=False)
        self.assertEqual(res["errors"], [])
        return res

    SHELLS = {
        "tcsh": ([shutil.which("tcsh") or "/bin/tcsh", "-f"], "set prompt='TCSH> '\n", "TCSH> "),
        "bash": (["/bin/bash", "--norc", "--noprofile", "-i"], "PS1='BASH$ '; PS2='> '\n", r"BASH\$ "),
    }

    def paste(self, shell, steps, crlf=False):
        """대화형 셸에 steps 를 보낸다. crlf 이면 붙여넣는 글의 줄바꿈을 CR+LF 로 보낸다(그렇게 보내는 터미널 흉내).
        마지막 단계 글 뒤에 exit 를 붙인다(붙여넣은 글 다음 줄로 처리)."""
        argv, set_prompt, prompt_re = self.SHELLS[shell]
        conv = (lambda t: t.replace("\n", "\r\n")) if crlf else (lambda t: t)
        steps = [(w, idle, conv(t)) for w, idle, t in steps]
        last = steps[-1]
        steps[-1] = (last[0], last[1], last[2] + "exit\n")
        return run_pty(argv, self.env, os.path.join(self.jump, "work", "deep"),
                       [(None, 0, set_prompt), (prompt_re, 0.5, "")] + steps)

    def setup_jump(self, res, shell, crlf):
        # 비밀번호 질문이 실제로 화면 끝에 나와 기다릴 때만 입력한다(Enter 는 CR)
        out = self.paste(shell, [(None, 0, res["setup"]), (r"VM password: \Z", 0.3, TEST_PW + "\r"),
                                 (r"saved: /", 0.3, "")], crlf)
        d = os.path.join(self.jump, ".tcgen")
        with open(os.path.join(d, "vmpw")) as f:
            self.assertEqual(f.read(), TEST_PW + "\n", out[-2000:])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "vmpw")).st_mode), 0o600)
        for n in ("vmssh", "askpass", "setpw"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode), 0o700, n)
        self.assertNotIn(TEST_PW, out, "비밀번호가 화면에 보이면 안 됨")
        self.assertEqual(len(re.findall(r"VM password: \r?\n", out)), 1, "비밀번호는 한 번만 묻고 입력을 기다려야 함")

    def vm_crontab(self, host):
        with open(os.path.join(self.vms, host, "crontab.txt")) as f:
            return f.read().splitlines()

    def vm_flow(self, shell, crlf):
        res = self.gen(self.cases)
        self.setup_jump(res, shell, crlf)
        os.makedirs(os.path.join(self.vms, "esx01ev01"))
        with open(os.path.join(self.vms, "esx01ev01", "crontab.txt"), "w") as f:
            f.write("30 9 * * 1 /home/tester/existing.sh > /tmp/x.log 2>&1\n")
        out = self.paste(shell, [(None, 0, res["vm"])], crlf)
        with open(os.path.join(self.vms, "ssh_calls.txt")) as f:
            self.assertEqual(f.read().split(), ["esx01ev01", "esx01ev02"], out[-3000:])
        expect = {}
        for e in res["entries"]:
            if not e["esx"]:
                expect.setdefault(e["host"], []).append(e["line"])
        for host, lines in expect.items():
            got = self.vm_crontab(host)
            tc = [ln for ln in got if " # tcgen " in ln]
            self.assertEqual(tc, lines, host)
        self.assertEqual(self.vm_crontab("esx01ev01")[0], "30 9 * * 1 /home/tester/existing.sh > /tmp/x.log 2>&1")
        self.assertFalse(os.path.exists(os.path.join(self.vms, "esx01ev01", ".tcgen_cron.new")))
        # 같은 블록을 다시 붙여넣어도 줄이 늘지 않는다
        self.paste(shell, [(None, 0, res["vm"])], crlf)
        for host, lines in expect.items():
            self.assertEqual([ln for ln in self.vm_crontab(host) if " # tcgen " in ln], lines)
        # 케이스 하나만 다시 만들면 그 케이스 줄만 바뀐다
        one = [dict(self.cases[1], start="2026-11-20 13:00:00")]
        res2 = self.gen(one)
        self.paste(shell, [(None, 0, res2["vm"])], crlf)
        got = [ln for ln in self.vm_crontab("esx01ev02") if " # tcgen " in ln]
        self.assertEqual(sum(1 for ln in got if "TC-b4 " in ln), sum(1 for ln in expect["esx01ev02"] if "TC-b4 " in ln))
        self.assertTrue(any(ln.startswith("00 13 20 11 * ") and "TC-b4 esx01ev02" in ln for ln in got))
        self.assertTrue(any("TC-a esx01ev02" in ln for ln in got))
        # cronie 처럼 실행: 홈 폴더에서 실행되고 변수와 % 가 맞게 바뀌는지
        for host in ("esx01ev01", "esx01ev02"):
            home = os.path.join(self.vms, host)
            for ln in self.vm_crontab(host):
                if " # tcgen " not in ln:
                    continue
                cmd, stdin = cronie_split(cron_command(ln))
                self.assertIsNone(stdin, ln)
                p = subprocess.run(["/bin/sh", "-c", cmd], cwd=home, env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                   input=b"", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                text = p.stdout.decode()
                self.assertEqual(p.returncode, 0, ln + "\n" + text)
                if "run_b.sh" in ln:
                    self.assertRegex(text, r"run_b\.sh\|%s\|-n 4 -d \d\d" % re.escape(os.path.realpath(home)))
        short = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE).stdout.decode().strip()
        logs = os.listdir(self.log)
        self.assertTrue(any(re.fullmatch(r"run_a-%s-\d{14}\.log" % re.escape(short), n) for n in logs), logs)
        a_log = [n for n in logs if n.startswith("run_a-")][0]
        with open(os.path.join(self.log, a_log)) as f:
            self.assertIn("|-o x%y", f.read())

    def test_vm_blocks_tcsh_lf(self):
        self.vm_flow("tcsh", False)

    def test_vm_blocks_tcsh_crlf(self):
        self.vm_flow("tcsh", True)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "bash 없음")
    def test_vm_blocks_bash_lf(self):
        self.vm_flow("bash", False)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "bash 없음")
    def test_vm_blocks_bash_crlf(self):
        self.vm_flow("bash", True)

    def esx_text(self, res):
        t = res["esx"]["esx01"]
        t = t.replace("/var/spool/cron/crontabs/", os.path.join(self.esx, "crontabs") + "/")
        t = t.replace("/tmp/", os.path.join(self.esx, "tmp") + "/")
        return t

    def run_esx(self, res):
        shell = shutil.which("dash") or "/bin/sh"
        p = subprocess.run([shell], input=self.esx_text(res).encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env={"PATH": "/usr/bin:/bin", "HOME": self.esx}, cwd=self.esx, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        with open(os.path.join(self.esx, "crontabs", "root")) as f:
            return f.read().splitlines(), p.stdout.decode()

    def test_esx_block(self):
        res = self.gen(self.cases)
        lines, out = self.run_esx(res)
        self.assertEqual(lines[:4], ESX_DEFAULT.splitlines())
        want = [e["line"] for e in res["entries"] if e["esx"]]
        self.assertEqual([ln for ln in lines if " # tcgen " in ln], want)
        with open(os.path.join(self.esx, "crontabs", "cron.update")) as f:
            self.assertEqual(f.read(), "root\n")
        self.assertTrue(os.path.exists(os.path.join(self.esx, "tmp", "root.crontab.tcgen.bak")))
        lines2, _ = self.run_esx(res)                      # 다시 붙여넣어도 줄이 늘지 않는다
        self.assertEqual([ln for ln in lines2 if " # tcgen " in ln], want)
        self.assertTrue(want[0].startswith("55 14 19 11 * "), want[0])   # KST 11-19 23:55 = UTC 14:55
        shell = shutil.which("dash") or "/bin/sh"
        for ln in want:                                   # busybox crond 처럼 그대로 sh -c
            p = subprocess.run([shell, "-c", cron_command(ln)], cwd=self.esx, env={"PATH": "/usr/bin:/bin"},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            self.assertEqual(p.returncode, 0, ln + "\n" + p.stdout.decode())
        short = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE).stdout.decode().strip()
        logs = [n for n in os.listdir(self.log) if n.startswith("esx-")]
        self.assertTrue(logs and all(re.fullmatch(r"esx-%s-\d{14}\.log" % re.escape(short), n) for n in logs), logs)
        with open(os.path.join(self.log, logs[0])) as f:
            self.assertRegex(f.read(), r"start %s \d{14} %%H" % re.escape(short))


if __name__ == "__main__":
    unittest.main()
