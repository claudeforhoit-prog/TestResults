# -*- coding: utf-8 -*-
"""
tcrun(점프 VM 진행 스크립트)과 tcgen_apply.py 의 통합 시험 (v2.12 의 동작. 사용자 결정 2026-11-18, 업무기록 W-20261118-08).

가짜 환경(fakeenv.py): 가짜 ssh(VM 별 홈 폴더, ESX 는 키 접속), 가짜 crontab, 등록된 줄을 그 시각에 실행하는 cron 흉내,
가짜 vim-cmd 와 esxcli, 가짜 vmx 폴더, 가상 시계(date 와 sleep 을 가짜 명령으로 바꿔 시간이 빨리 감).
tcrun 과 tcgen_apply.py 에는 시험용 코드가 없다. 경로만 환경 변수(TCGEN_TMP 등)로 바꾼다.

확인하는 동작:
- 잡은 plan 에 적힌 시작 시각 그대로 crontab 에 등록한다. tcrun 이 시각을 바꾸지 않는다
- 설정과 VM 전원이 같은 케이스들의 줄은 한꺼번에 등록한다. 설정이 다른 케이스는 잡 시작 N분 전에 VM 종료, 설정 적용,
  VM 전원 ON 을 한 뒤 그 케이스부터 다음 설정 변경 전까지의 줄을 등록한다
- 구성 스냅샷은 모든 케이스에서 잡 시작 N분 전에 읽는다. 잡이 실행되는 동안에는 VM 에 접속하지 않는다
- 등록 기록(jobs.csv), 멈춤과 취소, 같은 ESX 호스트의 실행 대기, 앞 실행이 남긴 줄의 삭제, 점검(check)
- 실패했을 때의 멈춤(VM 전원, 없는 VM, 없는 호스트 옵션, ESX 키)과 등록하지 못한 케이스의 기록, 모니터링 시작과 중지,
  구성 스냅샷의 내용과 스냅샷 때 실행하는 명령, 잡 시작 간격 경고, 홈 밖의 작업 폴더, dash 로 실행한 경우
"""
import csv
import datetime as dt
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakeenv  # noqa: E402
import fake_ops  # noqa: E402
from fakeenv import core  # noqa: E402

GEN = dt.datetime(2026, 11, 20, 10, 0, 0)
RID = "20261120-100000"
CHANGE_CALLS = ("power.shutdown", "power.off", "power.on", "reload", " set ")       # ESX 명령 가운데 설정이나 전원을 변경하는 명령


def epoch(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=fakeenv.KST).timestamp()


def vm(name, job="(없음)", monitors=(), profiles=(), power="on"):
    return {"name": name, "job": job, "monitors": list(monitors), "profiles": list(profiles), "end_ref": False, "power": power}


def case(name, vms, esx="esx01", esx_monitors=(), esx_profiles=(), max_wait="", group="g1", desc="", seq_start=""):
    c = core.new_case(esx, "", 0)
    c.update(name=name, vms=vms, esx_monitors=list(esx_monitors), esx_profiles=list(esx_profiles), max_wait=max_wait,
             group=group, desc=desc, seq_start=seq_start)
    return c


@unittest.skipUnless(fakeenv.HAVE, "perl, sh 가 있는 Linux/macOS 에서만 실행")
class TcrunBase(unittest.TestCase):
    DASH = False
    JDIR = False         # True: 점프 VM 작업 폴더를 홈 밖에 둔다
    SPEED = None         # 가상 시계의 배속. None 이면 fakeenv 의 기본값(30배). 초 단위로 시점을 맞추는 시험만 낮춘다

    def setUp(self):
        # 생성기는 시작 시각을 이 PC 의 시간대로 초(epoch)로 바꾼다. 가짜 환경의 시계는 KST 이므로 시험 프로세스도 KST 로 맞춘다
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.e = fakeenv.FakeEnv(speed=self.SPEED, dash=self.DASH, jdir=self.JDIR)
        e = self.e
        self.run_cmds = [
            {"app": "sim", "name": "job_a", "desc": "", "cmd": e.job_cmd("simA", 100), "proc": "simA"},
            {"app": "sim", "name": "job_b", "desc": "", "cmd": e.job_cmd("sim.B+x", 150), "proc": "sim.B+x"},
            {"app": "sim", "name": "job_long", "desc": "", "cmd": e.job_cmd("simLong", 900), "proc": "simLong"},
            {"app": "sim", "name": "job_wrong", "desc": "", "cmd": e.job_cmd("otherName", 60), "proc": "simA"},
        ]
        self.mon_cmds = [
            {"app": "m", "name": "gmon", "desc": "", "where": "VM", "start": e.mon_cmd("start", "g"), "stop": e.mon_cmd("stop", "g")},
            {"app": "m", "name": "gonce", "desc": "", "where": "VM", "start": e.mon_cmd("start", "once"), "stop": ""},
            {"app": "m", "name": "hmon", "desc": "", "where": "ESX", "start": e.mon_cmd("start", "h"), "stop": e.mon_cmd("stop", "h")},
        ]
        self.profiles = [
            {"name": "numa0", "target": "ESX", "text": "Numa.LocalityWeightActionAffinity = 0", "desc": ""},
            {"name": "nosuch", "target": "ESX", "text": "/Numa/NoSuchOption = 1", "desc": ""},
            {"name": "pin", "target": "VM", "text": 'sched.mem.pin = "TRUE"\nnuma.nodeAffinity =', "desc": ""},
            {"name": "same", "target": "VM", "text": "numa.nodeAffinity = 0\nnumvcpus = 8", "desc": ""},
        ]

    def tearDown(self):
        self.e.close()
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def two_vms(self, **esx_kw):
        self.e.add_vm("esx01ev01", crontab="30 9 * * 1 /home/tester/existing.sh > /tmp/x.log 2>&1\n")
        self.e.add_vm("esx01ev02")
        self.e.add_esx("esx01", vms=[("esx01ev01", esx_kw.get("vm1", {})), ("esx01ev02", esx_kw.get("vm2", {}))])

    def gen(self, cases, now=GEN, **over):
        res = core.generate(cases, self.run_cmds, self.mon_cmds, self.e.settings(**over), now=now, write=False,
                            profiles=self.profiles)
        self.assertEqual(res["errors"], [])
        return res, self.e.put_plan(res)

    def log(self, chain="esx01", rid=RID):
        return self.e.log_text(rid, chain)

    def ctx(self, out=""):
        return "\n---- tcrun output\n%s\n---- log\n%s" % (out, self.log())

    def results(self, chain="esx01", rid=RID):
        return list(csv.DictReader(io.StringIO(self.e.log_text(rid, chain, "-result.csv"))))

    def report(self, chain="esx01", rid=RID):
        raw = self.e.log_text(rid, chain, "-perfreport_cases.csv")
        return raw, list(csv.reader(io.StringIO(raw.lstrip("﻿"))))

    def assert_order(self, text, words):
        pos = -1
        for w in words:
            nxt = text.find(w, pos + 1)
            self.assertGreater(nxt, pos, "로그에서 '%s' 가 이 순서에 없음\n%s" % (w, text))
            pos = nxt

    def jobs(self, rid=RID):
        """등록 기록(jobs.csv)의 행: (case, vm, job_start, crontab)."""
        return [(r["case"], r["vm"], r["job_start"], r["crontab"]) for r in self.e.jobs_rows(rid)]

    def log_time(self, word, chain="esx01", rid=RID):
        """로그에서 word 가 처음 나오는 줄의 시각(epoch)."""
        for line in self.log(chain, rid).splitlines():
            if word in line:
                return epoch(line[:19])
        raise AssertionError("로그에 '%s' 가 없음\n%s" % (word, self.log(chain, rid)))

    def tc_lines(self, name):
        return [x for x in self.e.vm_crontab(name) if " # tcgen " in x]

    def fired(self, cron, host, word):
        return [t for h, t, line in cron.fired if h == host and word in line]

    def minutes(self):
        """점프 VM 에 기록된 잡 시작 시각(<작업 폴더>/minutes): {분의 시작 epoch: 기록한 실행, ESX 호스트, 케이스}."""
        d = os.path.join(self.e.tcgen, "minutes")
        out = {}
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            if not n.isdigit():
                continue
            try:
                with open(os.path.join(d, n, "who")) as f:
                    out[int(n)] = f.read().strip()
            except (IOError, OSError):
                out[int(n)] = ""
        return out

    def claim(self, when, who, reg=False):
        """다른 진행이 잡 시작 시각을 기록해 둔 것과 같은 폴더를 만든다. reg: 그 잡의 줄 등록까지 마친 기록."""
        d = os.path.join(self.e.tcgen, "minutes", str(int(epoch(when))))
        os.makedirs(d)
        with open(os.path.join(d, "who"), "w") as f:
            f.write(who)
        if reg:
            open(os.path.join(d, "reg"), "w").close()
        return d

    def ssh_calls(self):
        """가짜 ssh 가 적은 접속 기록의 줄: 'vm <대상>', 'esx <호스트> ...', 'deny <대상>'."""
        p = os.path.join(self.e.root, "ssh_calls.txt")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def mon_words(self, host, esx=False):
        """모니터링 명령이 남긴 기록(mon.log)의 줄마다 앞 두 낱말. 예: [["start", "g"], ["stop", "g"]]."""
        p = os.path.join(self.e.esx_dir(host) if esx else self.e.vm_dir(host), "mon.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [x.split()[:2] for x in f.read().splitlines()]

    def change_calls(self, esx="esx01"):
        """ESX 호스트에서 실행한 명령 가운데 설정이나 VM 전원을 변경하는 명령."""
        return [x for x in self.e.esx_calls(esx) if any(w in x for w in CHANGE_CALLS)]

    def wait_log(self, word, seconds=120, chain="esx01", rid=RID):
        """진행 로그에 word 가 나올 때까지 기다린다."""
        self.e.wait_for(lambda: word in self.log(chain, rid), seconds=seconds, what="로그에 '%s'" % word)

    def wait_state(self, word, seconds=120, chain="esx01", rid=RID):
        """진행의 상태(state)가 word 가 될 때까지 기다린다."""
        self.e.wait_for(lambda: self.e.state(rid, chain).get("state") == word, seconds=seconds,
                        what="%s %s 의 상태 %s (지금 %s)" % (rid, chain, word, self.e.state(rid, chain)))

    def wait_text(self, word, seconds=120, chain="esx01", rid=RID):
        """진행의 상태 글(text)에 word 가 나올 때까지 기다린다."""
        self.e.wait_for(lambda: word in self.e.state(rid, chain).get("text", ""), seconds=seconds,
                        what="%s %s 의 상태 글에 '%s'" % (rid, chain, word))

    def wait_clock(self, text, seconds=300):
        """가상 시계가 text(년-월-일 시:분:초)가 될 때까지 기다린다."""
        self.e.wait_for(lambda: self.e.now() >= epoch(text), seconds=seconds, what="가상 시계 %s" % text)

    def watch_logins(self, p, seconds=600):
        """진행 p 가 끝날 때까지 (가상 시각, 그때까지의 VM 접속 횟수)의 목록을 반환한다."""
        seen, end = [], time.time() + seconds
        while p.poll() is None:
            seen.append((self.e.now(), self.e.vm_logins()))
            self.assertLess(time.time(), end, "진행이 %d초 안에 끝나지 않음%s" % (seconds, self.ctx()))
            time.sleep(0.05)
        return seen

    def snap(self, seq, name, chain="esx01", rid=RID):
        """구성 스냅샷 csv: (원문, 행 목록, {(구분, 항목): VM 별 값 목록})."""
        raw = self.e.log_text(rid, chain, "-config-%s-%s.csv" % (seq, name))
        rows = list(csv.reader(io.StringIO(raw.lstrip("\ufeff"))))
        table, group = {}, ""
        for r in rows[1:]:
            group = r[0] or group
            table[(group, r[1])] = r[2:]
        return raw, rows, table

    def slow_crontab(self, name, seconds):
        """그 VM 의 다음 crontab 명령 한 번이 seconds 초(가상 시계) 걸리게 한다(접속은 됐고 줄을 넣는 명령이 멈춘 경우)."""
        with open(os.path.join(self.e.vm_dir(name), "crontab_slow"), "w") as f:
            f.write(str(seconds))

    def slow_login(self, name, seconds):
        """그 VM 의 다음 접속 한 번이 seconds 초(가상 시계) 걸리게 한다. 접속이 끝나기 전에 tcrun 이 접속을 끊으면 VM 에서는 아무것도
        실행되지 않는다."""
        with open(os.path.join(self.e.vm_dir(name), "login_slow"), "w") as f:
            f.write(str(seconds))

    def assert_gone(self, rid=RID, chain="esx01"):
        """tcrun stop 이 그 실행을 점프 VM 의 실행 기록에서 지웠는지 본다: 실행 폴더가 없고 status -a 에 나오지 않고, 같은 S 블록이
        다시 시작하지 않도록 stopped_runs 에 적었다. 로그 폴더의 로그는 남는다."""
        e = self.e
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", rid)), "실행 폴더를 지움: %s" % rid)
        self.assertNotIn(rid, e.tcrun("status", "-a")[1], "status 에 나오지 않음")
        with open(os.path.join(e.tcgen, "stopped_runs")) as f:
            self.assertIn("%s %s" % (rid, chain), f.read().split("\n"))
        self.assertTrue(os.path.exists(os.path.join(e.log, "tcrun-%s-%s.log" % (rid, chain))), "로그는 남음")

    def snap_files(self, chain="esx01", rid=RID):
        """로그 폴더에 있는 그 실행의 구성 스냅샷 csv 이름."""
        return sorted(n for n in os.listdir(self.e.log) if n.startswith("tcrun-%s-%s-config-" % (rid, chain)))


class RegisterTests(TcrunBase):
    """잡 시작 시각과 crontab 등록 (사용자 결정 2026-11-18: 적은 시각 그대로 등록, 설정 변경이 없는 케이스는 한꺼번에)."""

    def test_cases_with_the_same_settings_are_registered_at_once(self):
        """설정이 같은 케이스들: 실행을 시작하자마자 모든 케이스의 줄을 등록한다. 잡은 적은 시각에 시작하고, 그 사이 VM 접속은
        케이스마다 구성 스냅샷 한 번뿐이다. 진행은 마지막 잡 시작 1분 뒤에 끝나고 줄은 남는다."""
        e = self.e
        self.two_vms()
        c1 = case("t1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:12")
        c2 = case("t2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="+0:10")
        res, plan = self.gen([c1, c2])
        self.assertIn("SEG 001 1\n", res["plan"])
        self.assertIn("SEG 002 0\n", res["plan"])
        self.assertIn("START 002 %d 2026-11-20 10:22\n" % int(epoch("2026-11-20 10:22:00")), res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        want = [epoch("2026-11-20 10:12:00"), epoch("2026-11-20 10:22:00")]
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), want, "잡은 적은 시각 그대로 시작")
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), want)
        self.assert_order(log, [
            "==== case t1 (1 of 2) begin", "settings and VM power: already as wanted. Nothing is changed for case t1",
            "registered: case t1, job start 2026-11-20 10:12", "registered: case t2, job start 2026-11-20 10:22",
            "registration: 2 of 2 case(s) of this part are in crontab now",
            "config snapshot (before the job of this case)", "-config-001-t1.csv", "==== case t1 done. job start 2026-11-20 10:12:00",
            "==== case t2 (2 of 2) begin", "-config-002-t2.csv", "==== case t2 done. job start 2026-11-20 10:22:00",
            "run finished: 2 case(s), 2 job case(s) registered, 0 not registered"])
        self.assertLess(self.log_time("registered: case t2"), epoch("2026-11-20 10:06:00"), "두 케이스를 시작 직후에 한꺼번에 등록")
        self.assertNotIn("WARN", log)
        self.assertEqual(self.jobs(), [("t1", "esx01ev01", "2026-11-20 10:12", "registered"), ("t1", "esx01ev02", "2026-11-20 10:12", "registered"),
                                       ("t2", "esx01ev01", "2026-11-20 10:22", "registered"), ("t2", "esx01ev02", "2026-11-20 10:22", "registered")])
        a, b = self.results()
        self.assertEqual([(r["case"], r["status"], r["job_start"], r["apply"]) for r in (a, b)],
                         [("t1", "registered", "2026-11-20 10:12:00", "none"), ("t2", "registered", "2026-11-20 10:22:00", "none")])
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("all 2 case(s) finished: 2 job case(s) registered. The crontab lines stay on the hosts", st["text"])
        self.assertGreaterEqual(self.log_time("run finished"), epoch("2026-11-20 10:23:00"), "마지막 잡 시작 1분 뒤에 진행이 끝남")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "줄은 남는다(다음 실행이나 tcrun stop 이 지움)")
        self.assertEqual([x for x in e.esx_calls("esx01") if any(w in x for w in CHANGE_CALLS)], [], "설정과 전원은 그대로")
        # 구성 스냅샷은 잡 시작 3분 전(시험 설정)
        for seq, name, at in (("001", "t1", "10:09"), ("002", "t2", "10:19")):
            rows = list(csv.reader(io.StringIO(e.log_text(RID, "esx01", "-config-%s-%s.csv" % (seq, name)).lstrip("\ufeff"))))
            self.assertRegex(rows[1][2], r"^2026-11-20 %s:\d\d$" % at, "스냅샷 시각 %s" % rows[1])
        raw, rep = self.report()
        self.assertEqual([r[1:2] + r[4:6] for r in rep[1:]], [["t1", "2026-11-20 10:12:00", ""], ["t2", "2026-11-20 10:22:00", ""]])

    def test_no_vm_login_between_the_registration_and_the_snapshot(self):
        """등록한 뒤 다음 구성 스냅샷까지, 그리고 잡이 실행되는 동안 VM 에 접속하지 않는다."""
        e = self.e
        self.two_vms()
        c1 = case("n1", [vm("esx01ev01", "job_long")], seq_start="10:08")
        c2 = case("n2", [vm("esx01ev01", "job_a")], seq_start="10:30")
        res, plan = self.gen([c1, c2])
        e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "==== case n1 done" in self.log(), seconds=300, what="n1 스냅샷 끝")
        n0, seen = e.vm_logins(), []
        while "waiting until 2026-11-20 10:27:00 to take the config snapshot of case n2" not in e.state(RID, "esx01").get("text", ""):
            time.sleep(0.05)
            self.assertIsNone(p.poll(), self.ctx())
        while e.now() < epoch("2026-11-20 10:26:30"):
            seen.append(e.vm_logins())
            time.sleep(0.05)
        self.assertTrue(len(seen) > 10 and set(seen) == {n0}, "n1 의 잡이 실행되는 동안 VM 접속이 없음: %s, %s" % (n0, sorted(set(seen))))
        self.assertEqual(p.wait(timeout=300), 0, self.ctx())
        self.assertEqual(e.vm_logins() - n0, 1, "그 뒤의 접속은 n2 의 구성 스냅샷 한 번")

    def test_case_with_other_settings_is_prepared_before_its_job(self):
        """설정이 앞 케이스와 다른 케이스: 잡 시작 10분 전(시험 설정)에 VM 종료, 설정 적용, VM 전원 ON 을 하고, 그 케이스와 뒤의 같은
        설정 케이스를 함께 등록한다. 잡 시작 시각은 그대로다."""
        e = self.e
        self.two_vms()
        c1 = case("s1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:08")
        c2 = case("s2", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], seq_start="10:30")
        c3 = case("s3", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], seq_start="+0:10")
        res, plan = self.gen([c1, c2, c3])
        self.assertEqual([x for x in res["plan"].split("\n") if x.startswith("SEG ")], ["SEG 001 1", "SEG 002 1", "SEG 003 0"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, [
            "registered: case s1, job start 2026-11-20 10:08", "registration: 1 of 1 case(s) of this part",
            "==== case s1 done", "==== case s2 (2 of 3) begin",
            "settings and VM power: 2 value(s) differ. They are changed at 2026-11-20 10:20:00 (10 min before the job start 2026-11-20 10:30)",
            "settings: applied. The VMs of this case are powered off now.", "vm esx01ev01: ssh login works",
            "registered: case s2, job start 2026-11-20 10:30", "registered: case s3, job start 2026-11-20 10:40",
            "registration: 2 of 2 case(s) of this part", "-config-002-s2.csv", "==== case s2 done",
            "==== case s3 (3 of 3) begin", "-config-003-s3.csv", "==== case s3 done"])
        self.assertGreaterEqual(self.log_time("comparing and applying settings") if "comparing and applying settings" in log
                                else self.log_time("settings wanted by this case"), epoch("2026-11-20 10:20:00"))
        for name, when in (("s1", "10:08"), ("s2", "10:30"), ("s3", "10:40")):
            self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen %s " % name), [epoch("2026-11-20 %s:00" % when)], name)
        self.assertIn('sched.mem.pin = "TRUE"', e.vmx("esx01", "esx01ev01"))
        calls = e.esx_calls("esx01")
        self.assertEqual(len([x for x in calls if "power.shutdown" in x]), 2, "s2 에서만 VM 을 끔")
        self.assertEqual([r["apply"] for r in self.results()], ["none", "changed", "none"])
        self.assertNotIn("WARN", log)

    def test_first_case_that_needs_a_change_waits_for_its_time(self):
        """첫 케이스의 설정이 호스트와 다르면: 실행을 시작할 때 비교만 하고, 변경은 잡 시작 10분 전에 한다."""
        e = self.e
        self.two_vms()
        c1 = case("f1", [vm("esx01ev01", "job_a", profiles=["pin"])], seq_start="10:25")
        res, plan = self.gen([c1])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertIn("They are changed at 2026-11-20 10:15:00 (10 min before the job start 2026-11-20 10:25)", log)
        self.assertGreaterEqual(self.log_time("settings wanted by this case"), epoch("2026-11-20 10:15:00"))
        self.assertLess(self.log_time("settings wanted by this case"), epoch("2026-11-20 10:16:00"))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:25:00")])

    def test_values_already_the_same_are_not_changed(self):
        """프로파일의 값이 호스트와 이미 같으면 VM 을 끄지 않고 바로 등록한다."""
        e = self.e
        self.two_vms()
        c = case("same1", [vm("esx01ev01", "job_a", [], ["same"]), vm("esx01ev02", "(없음)", [], ["same"])], seq_start="10:10")
        res, plan = self.gen([c])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 0, self.ctx(out))
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if any(w in x for w in CHANGE_CALLS)], [])
        self.assertLess(self.log_time("registered: case same1"), epoch("2026-11-20 10:05:00"))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:10:00")])

    def test_start_time_is_not_moved_when_the_change_ends_too_late(self):
        """설정 변경이 잡 시작 전에 끝나지 않으면: 잡 시작 시각을 늦추지 않는다. 그 케이스는 등록하지 않고 기록에 남기며,
        같은 묶음의 뒤 케이스는 적은 시각에 등록한다."""
        e = self.e
        self.two_vms(vm1={"boot_sec": 300})
        c1 = case("l1", [vm("esx01ev01", "job_a", profiles=["pin"])], seq_start="10:06")
        c2 = case("l2", [vm("esx01ev01", "job_a", profiles=["pin"])], seq_start="10:30")
        res, plan = self.gen([c1, c2], seq_boot_wait_min=8)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertRegex(log, r"WARN settings and VM power: 2 value\(s\) differ\. The time to change them \(2026-11-20 09:56:00, 10 min before "
                              r"the job start 2026-11-20 10:06\) has passed: they are changed now, \d min before the job start\. "
                              r"The start time is kept\.")
        self.assertIn("WARN not registered: case l1, job start 2026-11-20 10:06: its job start time 2026-11-20 10:06:00 has passed or is less than 1 min away", log)
        self.assertIn("registered: case l2, job start 2026-11-20 10:30", log)
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen l1 "), [], "늦은 케이스의 잡은 시작하지 않는다(시각을 옮기지 않음)")
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen l2 "), [epoch("2026-11-20 10:30:00")])
        jobs = self.jobs()
        self.assertEqual(jobs[0][:3], ("l1", "esx01ev01", "2026-11-20 10:06"))
        self.assertTrue(jobs[0][3].startswith("not registered (its job start time"), jobs)
        self.assertEqual(jobs[1], ("l2", "esx01ev01", "2026-11-20 10:30", "registered"))
        a, b = self.results()
        self.assertEqual((a["status"], a["job_start"], b["status"]), ("not registered", "", "registered"))
        self.assertIn("1 NOT registered", e.state(RID, "esx01")["text"])
        self.assertNotIn("-config-001-l1.csv", log, "등록하지 않은 케이스는 구성 스냅샷을 찍지 않는다")

    def test_change_that_starts_late_is_marked_in_the_record(self):
        """설정을 바꿔야 하는데 잡 시작 10분 전(시험 설정)이 이미 지났으면: 바로 바꾸고 잡은 적은 시각에 등록한다. 로그에 WARN 을
        남기고 등록 기록에 설정을 몇 분 전에 바꿨는지 적는다."""
        e = self.e
        self.two_vms()
        c1 = case("m1", [vm("esx01ev01", "job_a", profiles=["pin"])], seq_start="10:09")
        res, plan = self.gen([c1])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertRegex(log, r"WARN settings and VM power: 2 value\(s\) differ\. The time to change them \(2026-11-20 09:59:00, 10 min before "
                              r"the job start 2026-11-20 10:09\) has passed: they are changed now, [5-8] min before the job start")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:09:00")], "잡은 적은 시각에 시작")
        (name, vmn, at, text), = self.jobs()
        self.assertEqual((name, vmn, at), ("m1", "esx01ev01", "2026-11-20 10:09"))
        self.assertRegex(text, r"^registered \(settings changed [5-8] min before the job start\. The setting is 10 min\)$")

    def test_nothing_is_changed_when_every_job_of_the_part_has_passed(self):
        """설정이 다른데 그 설정을 쓰는 케이스의 잡 시작이 모두 지났으면 VM 을 끄지 않는다(시작할 수 없는 잡을 위해 설정을 바꾸지 않음)."""
        e = self.e
        self.two_vms()
        c1 = case("p1", [vm("esx01ev01", "job_a", profiles=["pin"])], seq_start="10:02")
        c2 = case("p2", [vm("esx01ev01", "job_a")], seq_start="10:25")
        res, plan = self.gen([c1, c2], now=GEN - dt.timedelta(minutes=5))
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log("esx01", "20261120-095500")
        self.assertEqual(rc, 0, "%s\n%s" % (out, log))
        self.assertIn("Nothing is changed: the job start of every case with these settings has passed or is less than 1 min away", log)
        self.assertEqual([x for x in e.esx_calls("esx01") if any(w in x for w in CHANGE_CALLS)], [])
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen p1 "), [])
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen p2 "), [epoch("2026-11-20 10:25:00")])
        jobs = self.jobs("20261120-095500")
        self.assertTrue(jobs[0][3].startswith("not registered (its job start time"), jobs)
        self.assertEqual(jobs[1][3], "registered")


class FlowTests(TcrunBase):
    """전체 흐름과 케이스 구성의 여러 경우: 모니터링과 설정 프로파일, ESX 호스트가 없는 케이스, 잡이 없는 케이스, VM 시계 차이."""

    def test_full_flow_with_monitoring_and_settings(self):
        """VM 2대, VM 모니터링과 ESX 모니터링, 설정 프로파일(호스트 옵션, vmx 키)이 있는 케이스 2개. 잡 시작 10분 전에 설정 적용과 VM 전원
        ON 을 하고 두 케이스의 줄을 한꺼번에 등록한다. 모니터링 시작, 잡, 모니터링 중지는 모두 crontab 줄이고 plan 의 시각에 실행된다.
        등록한 뒤의 VM 접속은 케이스마다 구성 스냅샷 한 번(VM 마다 한 번)뿐이다. 결과 CSV, 리포트용 CSV, 등록 기록을 남긴다."""
        e = self.e
        self.two_vms()
        c1 = case("caseA", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"], desc="첫 케이스, 설명", seq_start="10:14")
        c2 = case("caseB", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"], seq_start="+0:16", max_wait="0:04")
        res, plan = self.gen([c1, c2])
        # 모니터링 중지 시각: 다음 케이스의 잡 시작 12분 전(설정 적용 10분 + 2분), 마지막 케이스는 잡 시작 + 최대 대기
        self.assertEqual([x for x in res["plan"].split("\n") if x.split(" ")[0] in ("SEG", "MSTOP")],
                         ["SEG 001 1", "MSTOP 001 %d 2026-11-20 10:18" % epoch("2026-11-20 10:18:00"),
                          "SEG 002 0", "MSTOP 002 %d 2026-11-20 10:34" % epoch("2026-11-20 10:34:00")])
        orig_vmx = e.vmx("esx01", "esx01ev01")
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        seen = self.watch_logins(p)
        log = self.log()
        self.assertEqual(p.returncode, 0, self.ctx())
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        # 단계 순서
        self.assert_order(log, [
            "==== case caseA (1 of 2) begin. VMs: esx01ev01 esx01ev02. Job start 2026-11-20 10:14",
            "settings and VM power: 3 value(s) differ. They are changed at 2026-11-20 10:04:00 (10 min before the job start 2026-11-20 10:14)",
            "settings wanted by this case:", "TCGEN-SET esx01ev01 sched.mem.pin: (none) -> \"TRUE\"  [change]",
            "TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 0  [change]", "TCGEN-RESULT changed 3 value(s)",
            "settings: applied. The VMs of this case are powered off now.",
            "VM power: the VMs of this case are powered on one after another when off (power on, 1GB page backing, ssh login): esx01ev01 esx01ev02",
            "esx01ev01: powered on", "vm esx01ev01: 1GB pages are not set for this VM", "vm esx01ev01: ssh login works, ready",
            "esx01ev02: powered on", "vm esx01ev02: ssh login works, ready",
            "VM power: done. Powered on by this case: esx01ev01 esx01ev02. The last one was ready at ",
            "registered: case caseA, job start 2026-11-20 10:14, monitoring start 2026-11-20 10:13:00, monitoring stop 2026-11-20 10:18:00, "
            "VMs: esx01ev01 esx01ev02",
            "registered: case caseB, job start 2026-11-20 10:30, monitoring start 2026-11-20 10:29:00, monitoring stop 2026-11-20 10:34:00, "
            "VMs: esx01ev01 esx01ev02",
            "registration: 2 of 2 case(s) of this part are in crontab now. Record: %s/tcrun-%s-jobs.csv" % (e.log, RID),
            "config snapshot (before the job of this case. Settings changed by this case. Powered on by this case: esx01ev01 esx01ev02): ",
            "-config-001-caseA.csv  (VM settings of 2 VM(s), guest OS settings of 2 VM(s))",
            "==== case caseA done. job start 2026-11-20 10:14:00 (crontab)",
            "==== case caseB (2 of 2) begin", "config snapshot (before the job of this case): ", "-config-002-caseB.csv",
            "==== case caseB done. job start 2026-11-20 10:30:00 (crontab)",
            "run finished: 2 case(s), 2 job case(s) registered, 0 not registered"])
        self.assertNotIn("WARN", log)
        self.assertTrue(all(ord(ch) < 128 for ch in log), "로그는 ASCII")
        self.assertGreaterEqual(self.log_time("settings wanted by this case:"), epoch("2026-11-20 10:04:00"), "설정 적용은 잡 시작 10분 전부터")
        # 설정: vmx 와 호스트 옵션, VM 을 켠 순서
        self.assertTrue(e.vmx("esx01", "esx01ev01").endswith('sched.mem.pin = "TRUE"\n'))
        self.assertNotIn("numa.nodeAffinity", e.vmx("esx01", "esx01ev01"))
        self.assertNotEqual(e.vmx("esx01", "esx01ev01"), orig_vmx)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.on" in x], ["vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11"])
        self.assertEqual(len([x for x in calls if "power.shutdown" in x]), 2, "VM 은 첫 케이스의 설정 적용 때 한 번만 끔")
        # crontab 줄의 내용: 모니터링 시작, 잡, 모니터링 중지가 케이스마다 있고 진행이 끝난 뒤에도 남는다. ESX 는 UTC
        b = e.bin
        self.assertEqual(e.vm_crontab("esx01ev01"), [
            "30 9 * * 1 /home/tester/existing.sh > /tmp/x.log 2>&1",
            "13 10 20 11 * sh %s/mon.sh start g # tcgen caseA esx01ev01" % b,
            "14 10 20 11 * sh %s/job.sh simA 100 # tcgen caseA esx01ev01" % b,
            "18 10 20 11 * sh %s/mon.sh stop g # tcgen caseA esx01ev01" % b,
            "29 10 20 11 * sh %s/mon.sh start g # tcgen caseB esx01ev01" % b,
            "30 10 20 11 * sh %s/job.sh simA 100 # tcgen caseB esx01ev01" % b,
            "34 10 20 11 * sh %s/mon.sh stop g # tcgen caseB esx01ev01" % b])
        self.assertEqual(e.vm_crontab("esx01ev02"), [
            "14 10 20 11 * sh %s/job.sh sim.B+x 150 # tcgen caseA esx01ev02" % b,
            "30 10 20 11 * sh %s/job.sh sim.B+x 150 # tcgen caseB esx01ev02" % b])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines() + [
            "13 01 20 11 * sh %s/mon.sh start h # tcgen caseA esx01" % b, "18 01 20 11 * sh %s/mon.sh stop h # tcgen caseA esx01" % b,
            "29 01 20 11 * sh %s/mon.sh start h # tcgen caseB esx01" % b, "34 01 20 11 * sh %s/mon.sh stop h # tcgen caseB esx01" % b])
        self.assertFalse(os.path.exists(os.path.join(e.vm_dir("esx01ev01"), ".tcgen_cron.new")))
        # cron 이 실행한 줄과 그 시각. 마지막 케이스의 모니터링 중지(10:34)는 진행이 끝난 뒤(10:31)에 실행된다
        e.wait_for(lambda: self.fired(cron, "esx01", "mon.sh stop h # tcgen caseB"), seconds=120, what="caseB 의 모니터링 중지")
        for host, word, when in (
                ("esx01ev01", "mon.sh start g # tcgen caseA", "10:13"), ("esx01", "mon.sh start h # tcgen caseA", "10:13"),
                ("esx01ev01", "job.sh simA 100 # tcgen caseA", "10:14"), ("esx01ev02", "job.sh sim.B+x 150 # tcgen caseA", "10:14"),
                ("esx01ev01", "mon.sh stop g # tcgen caseA", "10:18"), ("esx01", "mon.sh stop h # tcgen caseA", "10:18"),
                ("esx01ev01", "mon.sh start g # tcgen caseB", "10:29"), ("esx01", "mon.sh start h # tcgen caseB", "10:29"),
                ("esx01ev01", "job.sh simA 100 # tcgen caseB", "10:30"), ("esx01ev02", "job.sh sim.B+x 150 # tcgen caseB", "10:30"),
                ("esx01ev01", "mon.sh stop g # tcgen caseB", "10:34"), ("esx01", "mon.sh stop h # tcgen caseB", "10:34")):
            self.assertEqual(self.fired(cron, host, word), [epoch("2026-11-20 %s:00" % when)], (host, word))
        self.assertEqual(len(cron.fired), 12, cron.fired)
        j1, j2 = e.vm_file("esx01ev01", "job.log"), e.vm_file("esx01ev02", "job.log")
        self.assertEqual([x[0] for x in j1], ["start", "end", "start", "end"])
        self.assertEqual(int(j1[0][2]) // 60, int(j2[0][2]) // 60, "두 VM 의 잡이 같은 분에 시작")
        # cron 이 방금 실행한 중지 명령이 기록(mon.log)을 쓸 때까지 기다린다
        e.wait_for(lambda: len(self.mon_words("esx01ev01")) == 4 and len(self.mon_words("esx01", esx=True)) == 4, seconds=30,
                   what="모니터링 중지 명령의 기록")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"], ["start", "g"], ["stop", "g"]])
        self.assertEqual(self.mon_words("esx01", esx=True), [["start", "h"], ["stop", "h"], ["start", "h"], ["stop", "h"]])
        # VM 접속: 등록한 뒤 구성 스냅샷까지, 그리고 잡이 실행되는 동안에는 없다. 구성 스냅샷 때 VM 마다 한 번 접속한다
        def counts(t0, t1):
            return sorted(set(n for t, n in seen if t0 <= t <= t1))
        reg, a_done, b_done = (self.log_time(w) for w in ("registration: 2 of 2", "==== case caseA done", "==== case caseB done"))
        w1 = counts(reg + 5, epoch("2026-11-20 10:10:55"))          # 등록 뒤부터 caseA 의 스냅샷(10:11) 전까지
        w2 = counts(a_done + 5, epoch("2026-11-20 10:26:55"))       # caseA 의 스냅샷 뒤부터 caseB 의 스냅샷(10:27) 전까지. caseA 의 잡 실행 중
        w3 = counts(b_done + 5, epoch("2026-11-20 10:40:00"))       # caseB 의 스냅샷 뒤부터 진행이 끝날 때까지. caseB 의 잡 시작
        if not w1 and w2 and reg + 5 > epoch("2026-11-20 10:10:55"):
            w1 = [w2[0] - 2]            # 등록이 스냅샷 직전에 끝나(장비가 느린 때) 그 사이에 표본이 없음: 이 구간은 보지 않는다
        self.assertEqual([len(w1), len(w2), len(w3)], [1, 1, 1], "스냅샷 사이에는 VM 접속이 없음: %s %s %s" % (w1, w2, w3))
        self.assertEqual((w2[0] - w1[0], w3[0] - w2[0]), (2, 2), "구성 스냅샷 때 VM 마다 한 번 접속")
        self.assertEqual(e.vm_logins(), w3[0], "진행이 끝난 뒤에도 접속이 없음")
        # 결과 CSV: 잡을 확인하지 않으므로 first_seen, job_end 는 비어 있다
        a, b2 = self.results()
        self.assertEqual([(r["case"], r["esx"], r["apply"], r["monitor_start"], r["job_start"], r["first_seen"], r["job_end"],
                           r["monitor_stop"], r["status"]) for r in (a, b2)],
                         [("caseA", "esx01", "changed", "2026-11-20 10:13:00", "2026-11-20 10:14:00", "", "", "2026-11-20 10:18:00", "registered"),
                          ("caseB", "esx01", "none", "2026-11-20 10:29:00", "2026-11-20 10:30:00", "", "", "2026-11-20 10:34:00", "registered")])
        self.assertGreaterEqual(epoch(a["vms_on"]), epoch("2026-11-20 10:04:00"), "VM 을 다시 켠 케이스는 켜기가 끝난 시각을 적음")
        self.assertLess(epoch(a["vms_on"]), epoch("2026-11-20 10:13:00"))
        self.assertLessEqual(epoch(a["end"]), epoch(b2["begin"]) + 1, "앞 케이스의 스냅샷이 끝난 뒤에 다음 케이스")
        # 리포트용 CSV: BOM, 머리글, 잡 시작은 plan 의 시각, 잡 종료는 빈 칸(성능 리포트 생성기가 정한다)
        raw, rep = self.report()
        self.assertTrue(raw.startswith("﻿테스트 그룹,케이스명,설명,호스트 이름,잡 시작 시각,잡 종료 시각,VM,종료 기준 VM\r\n"))
        self.assertEqual(rep[1], ["g1", "caseA", "첫 케이스, 설명", "esx01", "2026-11-20 10:14:00", "", "esx01ev01;esx01ev02", ""])
        self.assertEqual(rep[2][:2] + rep[2][4:6], ["g1", "caseB", "2026-11-20 10:30:00", ""])
        self.assertEqual(len(rep), 3)
        # 등록 기록: 잡이 있는 VM 마다 한 행
        with open(os.path.join(e.log, "tcrun-%s-jobs.csv" % RID)) as f:
            self.assertEqual(f.readline(), "case,esx,vm,job_start,crontab\n")
        self.assertEqual([(r["case"], r["esx"], r["vm"], r["job_start"], r["crontab"]) for r in e.jobs_rows(RID)],
                         [("caseA", "esx01", "esx01ev01", "2026-11-20 10:14", "registered"), ("caseA", "esx01", "esx01ev02", "2026-11-20 10:14", "registered"),
                          ("caseB", "esx01", "esx01ev01", "2026-11-20 10:30", "registered"), ("caseB", "esx01", "esx01ev02", "2026-11-20 10:30", "registered")])
        # status: 끝난 실행과 호스트에 남은 줄
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : done" % RID, out)
        self.assertIn("crontab lines on the hosts: case caseA and 1 more. To remove them: sh %s/tcrun stop esx01" % e.tcgen, out)

    def test_job_without_monitoring_and_without_an_esx_host(self):
        """ESX 호스트를 적지 않은 케이스(모니터링 없음): ESX 호스트에 접속하지 않고, 실행을 시작하자마자 줄을 등록하고, 적은 시각에 잡을
        시작한다. 로그와 결과 파일의 호스트 이름은 noesx 다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("solo", [vm("esx01ev02", "job_a")], esx="", seq_start="10:05")])
        self.assertIn("CHAIN -\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "-")
        log = self.log("noesx")
        self.assertEqual(rc, 0, out + log)
        self.assertIn("registered: case solo, job start 2026-11-20 10:05, VMs: esx01ev02", log)
        self.assertNotIn("monitoring", log.split("==== case solo")[1], "모니터링이 없는 케이스의 등록 줄에는 모니터링 시각이 없음")
        r = self.results("noesx")[0]
        self.assertEqual((r["esx"], r["apply"], r["monitor_start"], r["job_start"], r["monitor_stop"], r["status"]),
                         ("noesx", "none", "", "2026-11-20 10:05:00", "", "registered"))
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh simA"), [epoch("2026-11-20 10:05:00")])
        self.assertEqual(len(cron.fired), 1)
        self.assertEqual(e.vm_crontab("esx01ev02"), ["05 10 20 11 * sh %s/job.sh simA 100 # tcgen solo esx01ev02" % e.bin])
        self.assertEqual(e.esx_calls("esx01"), [], "ESX 호스트를 적지 않은 케이스는 ESX 에 접속하지 않음")
        self.assertEqual([x for x in self.ssh_calls() if x.startswith("esx ")], [])
        self.assertEqual([(r["case"], r["esx"], r["vm"], r["job_start"], r["crontab"]) for r in e.jobs_rows(RID)],
                         [("solo", "noesx", "esx01ev02", "2026-11-20 10:05", "registered")])
        self.assertEqual(self.snap_files("noesx"), ["tcrun-%s-noesx-config-001-solo.csv" % RID], "구성 스냅샷은 Guest OS 설정만 남김")
        self.assertEqual(e.state(RID, "noesx")["state"], "done")

    def test_guest_shutdown_not_possible(self):
        """게스트 종료가 안 되는 VM(VMware Tools 없음)은 전원을 끄고 설정을 적용한다. 잡은 적은 시각에 시작한다."""
        e = self.e
        self.two_vms(vm1={"shutdown": "fault"})
        res, plan = self.gen([case("d1", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev02", "job_a")], seq_start="10:14")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.off" in x], ["vim-cmd vmsvc/power.off 10"])
        self.assertIn("esx01ev01: guest shutdown is not possible (Cannot complete operation because VMware Tools is not running", log)
        self.assertIn('sched.mem.pin = "TRUE"', e.vmx("esx01", "esx01ev01"))
        r = self.results()[0]
        self.assertEqual((r["apply"], r["status"], r["job_start"]), ("changed", "registered", "2026-11-20 10:14:00"))
        for name in ("esx01ev01", "esx01ev02"):
            self.assertEqual(self.fired(cron, name, "job.sh"), [epoch("2026-11-20 10:14:00")], name)

    def test_vmx_with_the_same_key_on_many_lines(self):
        """잡이 없는 케이스(설정만)는 적은 시각에 설정을 적용한다. 같은 키가 다른 값으로 여러 줄 있고 대소문자가 다른 vmx: 한 줄만 남기고,
        값을 비운 키는 지운다. 다시 실행하면 값이 같으므로 VM 을 끄지 않는다."""
        e = self.e
        e.add_vm("esx01ev01")
        odd = ('displayName = "esx01ev01"\nsched.mem.pin = "FALSE"\nSched.Mem.Pin = "maybe"\n# note\n'
               'Numa.NodeAffinity = "3"\nSCHED.MEM.PIN = "TRUE"\nguestOS = "rhel8-64"\n')
        e.add_esx("esx01", vms=[("esx01ev01", {})], vmx={"esx01ev01": odd})
        res, plan = self.gen([case("c1", [vm("esx01ev01", "(없음)", [], ["pin"])], seq_start="10:04")])
        self.assertIn("APPLYAT 001 %d 2026-11-20 10:04\n" % epoch("2026-11-20 10:04:00"), res["plan"])
        self.assertNotIn("START 001", res["plan"])
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(e.vmx("esx01", "esx01ev01"),
                         'displayName = "esx01ev01"\n# note\nguestOS = "rhel8-64"\nsched.mem.pin = "TRUE"\n')
        self.assert_order(log, ["settings and VM power: 2 value(s) differ. They are changed at 2026-11-20 10:04:00",
                                'TCGEN-SET esx01ev01 sched.mem.pin: 3 lines: "FALSE", "maybe", "TRUE" -> "TRUE"  [change]',
                                'TCGEN-SET esx01ev01 numa.nodeAffinity: "3" -> (remove)  [change]',
                                "==== case c1 done (settings only, no job)", "run finished: 1 case(s), 0 job case(s) registered, 0 not registered"])
        self.assertGreaterEqual(self.log_time("settings wanted by this case:"), epoch("2026-11-20 10:04:00"), "적은 시각에 적용")
        r = self.results()[0]
        self.assertEqual((r["apply"], r["status"], r["job_start"]), ("changed", "done (settings only)", ""))
        self.assertEqual(self.jobs(), [], "잡이 없는 케이스는 등록 기록에 행이 없음")
        self.assertEqual(self.snap_files(), [], "잡이 없는 케이스는 구성 스냅샷을 남기지 않음")
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        # 다시 실행하면 값이 같아 VM 을 끄지 않는다
        res, plan = self.gen([case("c2", [vm("esx01ev01", "(없음)", [], ["pin"])], seq_start="10:30")], now=GEN + dt.timedelta(seconds=9))
        n = len(e.esx_calls("esx01"))
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, out)
        self.assertEqual([x for x in e.esx_calls("esx01")[n:] if any(w in x for w in CHANGE_CALLS)], [])
        self.assertIn("settings and VM power: already as wanted. Nothing is changed for case c2", self.log(rid=res["run_id"]))
        self.assertEqual(self.results(rid=res["run_id"])[0]["apply"], "none")
        self.assertLess(e.now(), epoch("2026-11-20 10:30:00"), "바꿀 것이 없으면 적은 시각까지 기다리지 않고 끝남")

    def test_settings_only_case_then_job_cases(self):
        """잡이 없는 케이스(설정만)의 시작 시각을 비우면 다음 잡 케이스의 잡 시작 10분 전에 설정을 적용한다. 설정이 같은 뒤의 잡 케이스들은
        그때 함께 등록한다. 설정을 지정하지 않은 VM 은 끄지도 켜지도 않는다."""
        e = self.e
        self.two_vms()
        c1 = case("setonly", [vm("esx01ev01", "(없음)", [], ["pin"])], esx_profiles=["numa0"])
        c2 = case("after1", [vm("esx01ev01", "job_a", [], ["pin"])], esx_profiles=["numa0"], seq_start="10:14")
        c3 = case("after2", [vm("esx01ev01", "job_a", [], ["pin"])], esx_profiles=["numa0"], seq_start="+0:05")
        res, plan = self.gen([c1, c2, c3])
        self.assertEqual([x for x in res["plan"].split("\n") if x.split(" ")[0] in ("SEG", "APPLYAT")],
                         ["SEG 001 1", "APPLYAT 001 %d 2026-11-20 10:04" % epoch("2026-11-20 10:04:00"), "SEG 002 0", "SEG 003 0"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, [
            "==== case setonly (1 of 3) begin. VMs: esx01ev01", "settings and VM power: 3 value(s) differ. They are changed at 2026-11-20 10:04:00",
            "settings: applied. The VMs of this case are powered off now.", "vm esx01ev01: ssh login works, ready",
            "registered: case after1, job start 2026-11-20 10:14, VMs: esx01ev01", "registered: case after2, job start 2026-11-20 10:19, VMs: esx01ev01",
            "registration: 2 of 2 case(s) of this part are in crontab now", "==== case setonly done (settings only, no job)",
            "==== case after1 (2 of 3) begin", "-config-002-after1.csv", "==== case after1 done",
            "==== case after2 (3 of 3) begin", "-config-003-after2.csv", "==== case after2 done"])
        self.assertGreaterEqual(self.log_time("settings wanted by this case:"), epoch("2026-11-20 10:04:00"))
        self.assertLess(self.log_time("settings wanted by this case:"), epoch("2026-11-20 10:05:00"))
        self.assertNotIn("WARN", log)
        rows = self.results()
        self.assertEqual([(r["case"], r["apply"], r["status"], r["job_start"]) for r in rows],
                         [("setonly", "changed", "done (settings only)", ""), ("after1", "none", "registered", "2026-11-20 10:14:00"),
                          ("after2", "none", "registered", "2026-11-20 10:19:00")])
        self.assertEqual(rows[0]["monitor_start"], "")
        self.assertEqual(self.jobs(), [("after1", "esx01ev01", "2026-11-20 10:14", "registered"), ("after2", "esx01ev01", "2026-11-20 10:19", "registered")])
        self.assertEqual([x for x in e.esx_calls("esx01") if "power.on" in x], ["vim-cmd vmsvc/power.on 10"],
                         "설정을 지정하지 않은 VM 은 끄지도 켜지도 않음")
        with open(os.path.join(e.esx_dir("esx01"), "tmp", "apply.log")) as f:
            self.assertIn(" poweron esx01ev01 5400\n", f.read(), "VM 켜기 대기(기본 90분)를 초로 넘김")
        self.assertIn("power on wait 90 min", log)
        self.assertEqual(len(self.report()[1]), 3, "잡이 없는 케이스는 리포트용 CSV 에 넣지 않음")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:14:00"), epoch("2026-11-20 10:19:00")])
        self.assertEqual(len(cron.fired), 2)

    def test_vm_clocks_that_differ(self):
        """VM 시계가 서로 다르면(여기서는 50초): 점검(P)이 문제로 알린다. 진행은 시각을 바꾸지 않는다. crontab 줄은 VM 마다 그 VM 의
        시계로 적은 시각(분)에 실행된다. 중지 명령이 없는 모니터링은 시작 줄만 등록한다(중지 시각이 없어도 된다)."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02", clock_offset=50)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])
        res, plan = self.gen([case("skew", [vm("esx01ev01", "job_a", ["gonce"]), vm("esx01ev02", "job_a")], seq_start="10:08")])
        self.assertNotIn("MSTOP", res["plan"])
        self.assertEqual(res["notes"], [], "중지 명령이 없는 모니터링뿐이면 중지 시각이 없다는 알림도 없음")
        rc, out = e.tcrun("check", plan)
        self.assertRegex(out, r"\[ok\] vm esx01ev02 \(esx01ev02\): ssh login works, clock [5-9]\d s ahead of the jump VM")   # 50초 + 접속 지연
        self.assertRegex(out, r"\[PROBLEM\] the clocks of the VMs differ by [3-9]\d s\. Jobs of one case would start that far apart\. Check time sync\.")
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertIn("registered: case skew, job start 2026-11-20 10:08, monitoring start 2026-11-20 10:07:00, VMs: esx01ev01 esx01ev02", log)
        self.assertEqual(self.jobs(), [("skew", "esx01ev01", "2026-11-20 10:08", "registered"), ("skew", "esx01ev02", "2026-11-20 10:08", "registered")])
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh start once"), [epoch("2026-11-20 10:07:00")])
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:08:00")])
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [epoch("2026-11-20 10:08:00")], "시계가 빠른 VM 도 자기 시계의 10:08 에 실행")
        self.assertEqual(len(cron.fired), 3, "모니터링 시작과 잡 2개. 중지 줄은 없음")
        j1, j2 = e.vm_file("esx01ev01", "job.log"), e.vm_file("esx01ev02", "job.log")
        self.assertEqual((j1[0][0], j2[0][0]), ("start", "start"))
        self.assertEqual(int(j1[0][2]) // 60, int(j2[0][2]) // 60, "잡이 적은 시작 시각은 VM 마다 자기 시계 기준으로 같은 분")
        r = self.results()[0]
        self.assertEqual((r["monitor_start"], r["job_start"], r["monitor_stop"]), ("2026-11-20 10:07:00", "2026-11-20 10:08:00", ""))


class FailureTests(TcrunBase):
    """실패한 경우: 설정 변경이나 VM 전원에서 실패하면 멈추고(paused), 줄을 넣지 못한 케이스는 등록하지 않고 다음 케이스로 넘어간다."""

    def test_power_on_failure_rolls_back_and_pauses(self):
        """VM 전원 켜기 실패: 설정을 되돌리고, 켜져 있던 VM 을 순서대로 다시 켜 보고, 멈춘다. 등록하지 못한 케이스(그 케이스와 뒤의
        케이스)는 등록 기록에 'not registered (the run ended as paused: ...)' 로 남는다."""
        e = self.e
        self.two_vms(vm2={"poweron": "fail"})
        orig = e.vmx("esx01", "esx01ev01")
        c1 = case("e1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_a")], esx_profiles=["numa0"], seq_start="10:14")
        c2 = case("e2", [vm("esx01ev01", "job_a")], seq_start="10:32")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("power on of vm esx01ev02 failed", st["text"])
        self.assertIn("Failed to power on VM", st["text"])
        self.assertIn("The settings of case e1 were put back.", st["text"])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig, "vmx 를 백업으로 되돌림")
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130", "호스트 옵션을 이전 값으로")
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.on" in x],
                         ["vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11", "vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11"])
        self.assert_order(log, ["They are changed at 2026-11-20 10:04:00 (10 min before the job start 2026-11-20 10:14)",
                                "TCGEN-RESULT changed", "power on of vm esx01ev02 failed", "esx01ev01: powering off before the .vmx is put back",
                                "TCGEN-DONE vm esx01ev01: .vmx put back", "TCGEN-DONE host /Numa/LocalityWeightActionAffinity: put back to 130",
                                "rollback: rolledback 1 VM(s), 1 host option(s)", "vm esx01ev01: ssh login works",
                                "WARN power on of vm esx01ev02 failed", "PAUSED:", "run ended (paused) at case e1. Not finished: e1 e2"])
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "off"))
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        rows = self.results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["apply"], "rolledback")
        self.assertTrue(rows[0]["status"].startswith("paused: power on of vm esx01ev02 failed"))
        self.assertEqual(self.report()[1], [], "등록하지 못한 케이스는 리포트용 CSV 에 없음")
        jobs = self.jobs()
        self.assertEqual([j[:3] for j in jobs], [("e1", "esx01ev01", "2026-11-20 10:14"), ("e1", "esx01ev02", "2026-11-20 10:14"),
                                                 ("e2", "esx01ev01", "2026-11-20 10:32")], "멈춘 케이스와 그 뒤 케이스의 잡이 모두 기록에 남음")
        for j in jobs:
            self.assertTrue(j[3].startswith("not registered (the run ended as paused: power on of vm esx01ev02 failed"), j)
            self.assertTrue(j[3].endswith("The settings of case e1 were put back.)"), j)
        self.assertEqual(self.minutes(), {}, "등록하지 못한 케이스의 시작 시각 기록은 지움")

    def test_unknown_vm_pauses_before_any_change(self):
        """ESX 호스트에 없는 VM 이름: 실행을 시작할 때의 비교에서 알아내고, 아무것도 바꾸지 않고 멈춘다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx01ev09")
        orig = e.vmx("esx01", "esx01ev01")
        c1 = case("i1", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev09", "job_a", [], ["pin"])], esx_profiles=["numa0"], seq_start="10:20")
        c2 = case("i2", [vm("esx01ev01", "job_a")], seq_start="10:40")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("esx esx01: the settings of case i1 cannot be applied: VM 'esx01ev09' is not registered on this host", st["text"])
        self.assertNotIn("could not be read", st["text"], "읽지 못한 것이 아니라 적용할 수 없는 설정임을 알림")
        self.assertIn("Nothing of case i1 was done.", st["text"])
        self.assertLess(e.now(), epoch("2026-11-20 10:10:00"), "설정을 바꿀 시각(10:10)까지 기다리지 않고 실행을 시작할 때 멈춤")
        self.assertEqual(self.change_calls(), [])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130")
        jobs = self.jobs()
        self.assertEqual([j[:3] for j in jobs], [("i1", "esx01ev01", "2026-11-20 10:20"), ("i1", "esx01ev09", "2026-11-20 10:20"),
                                                 ("i2", "esx01ev01", "2026-11-20 10:40")])
        self.assertTrue(all(j[3].startswith("not registered (the run ended as paused: ") and "esx01ev09" in j[3] for j in jobs), jobs)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(cron.fired, [])
        self.assertEqual([x for x in self.ssh_calls() if x.startswith("vm ")], [], "VM 에 접속하지 않음")

    def test_unknown_host_option_pauses_before_any_change(self):
        """ESX 호스트에 없는 옵션을 적은 프로파일: 아무것도 바꾸지 않고 멈춘다."""
        e = self.e
        self.two_vms()
        orig = e.vmx("esx01", "esx01ev01")
        res, plan = self.gen([case("i2", [vm("esx01ev01", "job_a", [], ["pin"])], esx_profiles=["numa0", "nosuch"], seq_start="10:20")])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("esx esx01: the settings of case i2 cannot be applied: ", st["text"])
        self.assertIn("host option '/Numa/NoSuchOption' cannot be read", st["text"])
        self.assertEqual(self.change_calls(), [])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130")
        (name, vmn, at, text), = self.jobs()
        self.assertEqual((name, vmn, at), ("i2", "esx01ev01", "2026-11-20 10:20"))
        self.assertTrue(text.startswith("not registered (the run ended as paused: "), text)

    def test_registration_failure_on_one_vm(self):
        """한 VM 에서 줄 등록이 안 되면 그 케이스를 등록하지 않는다. 다른 VM 에 넣은 그 케이스의 줄을 지워서 일부 VM 만 잡이 시작되는
        일이 없다. 줄을 받지 않은 VM(crontab 이 파일을 받지 않았다고 답한 VM)에는 줄을 지우러 다시 접속하지 않는다. 실행은 멈추지 않고
        다음 케이스를 등록하며, 끝난 상태(done)의 글에 등록하지 못한 케이스 수를 적는다."""
        e = self.e
        self.two_vms()
        open(os.path.join(e.vm_dir("esx01ev02"), "crontab_broken"), "w").close()
        c1 = case("o1", [vm("esx01ev01", "job_a", ["gonce"]), vm("esx01ev02", "job_a")], seq_start="10:06")
        c2 = case("o2", [vm("esx01ev01", "job_a"), vm("esx01ev02")], seq_start="10:11")
        res, plan = self.gen([c1, c2])
        self.assertIn("SEG 002 0\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        why = "vm esx01ev02: the crontab lines were not registered (ssh exit 0: crontab: cannot be used)"
        self.assert_order(log, [
            "removed the lines of this case: vm esx01ev01",
            "WARN not registered: case o1, job start 2026-11-20 10:06: " + why,
            "registered: case o2, job start 2026-11-20 10:11, VMs: esx01ev01", "registration: 1 of 2 case(s) of this part are in crontab now",
            "==== case o1 done: NOT registered (see the WARN line above and %s/tcrun-%s-jobs.csv)" % (e.log, RID),
            "==== case o2 (2 of 2) begin", "-config-002-o2.csv", "==== case o2 done. job start 2026-11-20 10:11:00",
            "run finished: 2 case(s), 1 job case(s) registered, 1 not registered"])
        self.assertNotIn("removed the lines of this case: vm esx01ev02", log)
        self.assertNotIn("could not be removed", log, "줄을 받지 않은 VM 에는 줄을 지우러 다시 접속하지 않음")
        self.assertEqual(self.ssh_calls().count("vm esx01ev02"), 2, "esx01ev02 접속은 o1 의 등록과 o2 의 구성 스냅샷뿐(줄을 지우러 다시 접속하지 않음)")
        self.assertEqual(self.jobs(), [("o1", "esx01ev01", "2026-11-20 10:06", "not registered (%s)" % why),
                                       ("o1", "esx01ev02", "2026-11-20 10:06", "not registered (%s)" % why),
                                       ("o2", "esx01ev01", "2026-11-20 10:11", "registered")])
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("all 2 case(s) finished: 1 job case(s) registered, 1 NOT registered (see %s/tcrun-%s-jobs.csv)" % (e.log, RID), st["text"])
        self.assertEqual(self.tc_lines("esx01ev01"), ["11 10 20 11 * sh %s/job.sh simA 100 # tcgen o2 esx01ev01" % e.bin],
                         "등록하지 못한 케이스의 줄(모니터링 시작, 잡)은 남지 않음")
        self.assertEqual(self.tc_lines("esx01ev02"), [])
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen o1 ") + self.fired(cron, "esx01ev02", "# tcgen o1 "), [], "지운 줄은 실행되지 않음")
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen o2 "), [epoch("2026-11-20 10:11:00")])
        a, b = self.results()
        self.assertEqual([(r["case"], r["status"], r["job_start"], r["monitor_start"]) for r in (a, b)],
                         [("o1", "not registered", "", ""), ("o2", "registered", "2026-11-20 10:11:00", "")])
        self.assertEqual(self.snap_files(), ["tcrun-%s-esx01-config-002-o2.csv" % RID], "등록하지 못한 케이스는 구성 스냅샷을 남기지 않음")
        self.assertEqual(self.minutes(), {int(epoch("2026-11-20 10:11:00")): "run %s, esx esx01, case o2" % RID},
                         "등록하지 못한 케이스의 시작 시각 기록은 지우고, 등록한 케이스의 기록은 남음")
        self.assertTrue(os.path.exists(os.path.join(e.tcgen, "minutes", str(int(epoch("2026-11-20 10:11:00"))), "reg")))

    def test_lines_that_reach_a_vm_too_late_are_not_registered(self):
        """VM 이 줄을 받는 때가 첫 시작 시각 15초 전보다 늦으면(그 VM 의 시계 기준. 여기서는 VM 시계가 8분 빠르다) 그 VM 은 줄을 받지
        않고, 그 케이스를 등록하지 않는다. 다른 VM 에 넣은 그 케이스의 줄은 지운다. 시작 시각이 넉넉히 남은 다음 케이스는 등록한다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02", clock_offset=480)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])
        c1 = case("late1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:08")
        c2 = case("late2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:16")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        why = "vm esx01ev02: the lines arrived less than 15 s before the first start time 2026-11-20 10:08:00 (clock of the VM)"
        self.assertNotIn("removed the lines of this case: vm esx01ev02", log, "줄을 받지 않은 VM 에는 지울 것이 없음")
        self.assert_order(log, ["removed the lines of this case: vm esx01ev01",
                                "WARN not registered: case late1, job start 2026-11-20 10:08: " + why,
                                "registered: case late2, job start 2026-11-20 10:16, VMs: esx01ev01 esx01ev02",
                                "registration: 1 of 2 case(s) of this part are in crontab now"])
        jobs = self.jobs()
        self.assertEqual(jobs[:2], [("late1", "esx01ev01", "2026-11-20 10:08", "not registered (%s)" % why),
                                    ("late1", "esx01ev02", "2026-11-20 10:08", "not registered (%s)" % why)])
        self.assertEqual([j[3] for j in jobs[2:]], ["registered", "registered"])
        for name in ("esx01ev01", "esx01ev02"):
            self.assertEqual(self.fired(cron, name, "# tcgen late1 "), [], "등록하지 않은 케이스의 잡은 어느 VM 에서도 시작하지 않음 (%s)" % name)
            self.assertEqual(self.fired(cron, name, "# tcgen late2 "), [epoch("2026-11-20 10:16:00")], name)
            self.assertEqual(self.tc_lines(name), ["16 10 20 11 * sh %s/job.sh simA 100 # tcgen late2 %s" % (e.bin, name)])
        self.assertIn("1 NOT registered", e.state(RID, "esx01")["text"])

    def test_esx_key_not_registered(self):
        """ESX 호스트에 ssh 키가 등록돼 있지 않으면 아무것도 바꾸지 않고 멈춘다. 설정이 없는 케이스도 VM 전원을 호스트와 비교하므로 같다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {})], authorized=False)
        cron = e.start_cron()
        res, plan = self.gen([case("k1", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:20")])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("esx esx01: the settings and the VM power of case k1 could not be read (ssh exit 255: root@esx01: Permission denied", st["text"])
        self.assertEqual(e.esx_calls("esx01"), [])
        (name, vmn, at, text), = self.jobs()
        self.assertEqual((name, vmn, at), ("k1", "esx01ev01", "2026-11-20 10:20"))
        self.assertTrue(text.startswith("not registered (the run ended as paused: esx esx01: the settings and the VM power of case k1 "
                                        "could not be read"), text)
        res, plan = self.gen([case("k2", [vm("esx01ev01", "job_a")], esx_monitors=["hmon"], seq_start="10:20", max_wait="0:05")],
                             now=GEN + dt.timedelta(seconds=5))
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, out)
        self.assertIn("the settings and the VM power of case k2 could not be read (ssh exit 255: root@esx01: Permission denied",
                      e.state(res["run_id"], "esx01")["text"])
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(cron.fired, [])
        self.assertEqual([x for x in self.ssh_calls() if x.startswith("vm ")], [], "VM 에도 접속하지 않음")

    def test_version_mismatch(self):
        """plan 을 만든 생성기의 판과 설치된 tcrun 의 판이 다르면 시작하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("j1", [vm("esx01ev01", "job_a")], seq_start="10:10")])
        with open(plan) as f:
            text = f.read()
        with open(plan, "w") as f:
            f.write(text.replace("VER %s\n" % core.VERSION, "VER 9.9\n"))
        cron = e.start_cron()
        for cmd in (("run", plan, "esx01"), ("start", plan)):
            rc, out = e.tcrun(*cmd)
            self.assertEqual(rc, 3, out)
            self.assertIn("block A", out)
        self.assertEqual(e.state(RID, "esx01"), {})
        self.assertEqual(self.log(), "")
        self.assertEqual(self.ssh_calls(), [], "어디에도 접속하지 않음")
        self.assertEqual(cron.fired, [])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] the plan was made by tcgen 9.9, tcrun is %s: paste block A again" % core.VERSION, out)
        with open(plan, "w") as f:
            f.write("something else\n")
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual((rc, out.strip()), (2, "tcrun: not a tcgen plan of format 2: " + plan))

    def test_plan_with_unsafe_names_is_refused(self):
        """plan 은 생성기가 만들고 sha256 으로 확인하지만, tcrun 도 이름과 시각 줄에 명령줄에서 뜻이 달라지는 글자가 없는지 다시 본다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("u1", [vm("esx01ev01", "job_a")], seq_start="10:10")])
        with open(plan) as f:
            good = f.read()
        start = "START 001 %d 2026-11-20 10:10\n" % epoch("2026-11-20 10:10:00")
        cron = e.start_cron()
        for n, (old, new) in enumerate((("VM 001 esx01ev01 esx01ev01\n", "VM 001 esx01ev01 -oProxyCommand=x\n"),
                                        ("JOB 001 esx01ev01 ", "JOB 001 esx01*v01 "),
                                        (start, start.replace("10:10\n", "10:10;reboot\n")),
                                        ("SEG 001 1\n", "SEG 001 x\n"))):
            self.assertIn(old, good)
            rid = "20261120-10000%d" % (n + 1)
            d = os.path.join(e.tcgen, "runs", rid)
            os.makedirs(d)
            with open(os.path.join(d, "plan"), "w") as f:
                f.write(good.replace(old, new).replace("RUNID " + RID, "RUNID " + rid))
            rc, out = e.tcrun("run", os.path.join(d, "plan"), "esx01")
            self.assertEqual(rc, 1, out)
            st = e.state(rid, "esx01")
            self.assertEqual(st["state"], "paused", new)
            self.assertIn("a name that tcrun does not accept", st["text"])
            rc, out = e.tcrun("check", os.path.join(d, "plan"))
            self.assertIn("[PROBLEM] case number 001: the plan holds a name that tcrun does not accept", out)
        self.assertEqual(self.ssh_calls(), [], "어디에도 접속하지 않음")
        self.assertEqual(cron.fired, [])
        rc, out = e.tcrun("run", plan, "-x")
        self.assertEqual((rc, out.strip()), (2, "tcrun: bad ESX host name: -x"))
        d = os.path.join(e.tcgen, "runs", "20261120-100009")
        os.makedirs(d)
        with open(os.path.join(d, "plan"), "w") as f:
            f.write(good.replace("CHAIN esx01\n", "ADDR esx01 -oProxyCommand=x\nCHAIN esx01\n").replace("RUNID " + RID, "RUNID 20261120-100009"))
        rc, out = e.tcrun("run", os.path.join(d, "plan"), "esx01")
        self.assertEqual((rc, out.strip()), (2, "tcrun: bad ESX address in the plan for esx01"))

    def test_refused_login_is_not_tried_again(self):
        """VM 을 켠 뒤 ssh 로그인이 거부되면(Permission denied) 다시 시도하지 않고 멈춘다(계정 잠금 방지). 로그인 배너는 로그에 섞이지 않는다."""
        e = self.e
        with open(os.path.join(e.root, "banner"), "w") as f:
            f.write("Access is only available to authorized users. Activity is monitored.\n")
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "boot_sec": 0})])
        open(os.path.join(e.vm_dir("esx01ev01"), "deny"), "w").close()
        res, plan = self.gen([case("r1", [vm("esx01ev01", "job_a")], seq_start="10:14")])
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("vm esx01ev01: ssh login was refused (esx01ev01: Permission denied (publickey,password).). Not tried again", st["text"])
        self.assertEqual(self.ssh_calls().count("deny esx01ev01"), 1, "한 번만 시도")
        self.assertNotIn("Access is only available", self.log(), "LogLevel=ERROR 라서 배너가 출력에 섞이지 않음")
        self.assertEqual(self.jobs()[0][3][:44], "not registered (the run ended as paused: vm ")


class PowerTests(TcrunBase):
    """VM 전원: 꺼진 VM 을 하나씩 켜기(1GB page backing, ssh 로그인), 전원 끔인 VM 끄기, backing 이 멈춘 경우."""

    def test_vms_that_are_off_are_powered_on_one_after_another(self):
        """VM 이 모두 꺼져 있으면 잡 시작 N분 전(여기서는 15분)에 표의 순서대로 하나씩 켠다. VM 마다 1GB page backing 수가 메모리 GB 수와
        같아지고 ssh 로그인이 된 뒤 다음 VM 을 켠다. 잡이 없는 VM 도 켠다. 그 뒤에 줄을 등록하고 잡은 적은 시각에 시작한다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02")
        slow = {"state": "off", "lpage_rate": 0.1, "lpage_cap": 16}
        e.add_esx("esx01", vms=[("esx01ev01", dict(slow)), ("esx01ev02", dict(slow))],
                  vmx={"esx01ev01": fakeenv.vmx_1g("esx01ev01"), "esx01ev02": fakeenv.vmx_1g("esx01ev02")})
        res, plan = self.gen([case("p1", [vm("esx01ev01", "job_a"), vm("esx01ev02")], seq_start="10:23")], seq_prep_lead_min=15)
        self.assertIn("VM 001 esx01ev02 esx01ev02\n", res["plan"], "잡이 없는 VM 도 그 케이스의 VM")
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: 2 VM(s) to power on (compared with the hosts as they are now). The run changes them 15 min "
                      "before the job start of the case", out)
        self.assertIn("[ok] vm esx01ev01: powered off now. The run powers it on and then waits for its ssh login.", out)
        self.assertIn("CHECK RESULT: OK", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, [
            "==== case p1 (1 of 1) begin. VMs: esx01ev01 esx01ev02. Job start 2026-11-20 10:23",
            "settings and VM power: 2 VM(s) to power on. They are changed at 2026-11-20 10:08:00 (15 min before the job start 2026-11-20 10:23)",
            "VM power: the VMs of this case are powered on one after another when off (power on, 1GB page backing, ssh login): esx01ev01 esx01ev02",
            "esx01ev01: powered on", "1GB page backings, target, failed attempts: 16 16 0",
            "vm esx01ev01: 1GB page backing complete (16 of 16 1GB pages, failed attempts 0)", "vm esx01ev01: ssh login works, ready",
            "esx01ev02: powered on", "vm esx01ev02: 1GB page backing complete (16 of 16 1GB pages, failed attempts 0)",
            "vm esx01ev02: ssh login works, ready", "VM power: done. Powered on by this case: esx01ev01 esx01ev02. The last one was ready at",
            "registered: case p1, job start 2026-11-20 10:23, VMs: esx01ev01",
            "config snapshot (before the job of this case. Powered on by this case: esx01ev01 esx01ev02): ", "==== case p1 done"])
        self.assertNotIn("WARN", log)
        self.assertNotIn("settings wanted by this case:", log, "설정이 없는 케이스는 설정 적용을 하지 않고 전원만 맞춤")
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "on"), "잡이 없는 VM 도 켬")
        self.assertGreaterEqual(vms["esx01ev01"]["on_at"], epoch("2026-11-20 10:08:00"), "VM 켜기는 잡 시작 15분 전부터")
        self.assertGreaterEqual(vms["esx01ev02"]["on_at"] - vms["esx01ev01"]["on_at"], 160,
                                "둘째 VM 은 첫 VM 의 1GB backing(16개, 초당 0.1개 = 160초)이 끝난 뒤에 켬")
        r = self.results()[0]
        self.assertGreaterEqual(epoch(r["vms_on"]), vms["esx01ev02"]["on_at"] + 150, "준비 시각 = 둘째 VM 의 backing 과 ssh 로그인 뒤")
        self.assertEqual((r["apply"], r["status"], r["job_start"]), ("none", "registered", "2026-11-20 10:23:00"))
        self.assertGreater(self.log_time("registered: case p1"), epoch(r["vms_on"]) - 1, "줄 등록은 모든 VM 이 준비된 뒤")
        self.assertIn("vsish -e get /memory/lpage/vmLpage/100011", e.esx_calls("esx01"))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:23:00")])
        raw, rows, t = self.snap("001", "p1")
        self.assertEqual((t[("VM 기본 정보", "power state")], t[("Guest OS", "THP enabled")]), (["on", "on"], ["madvise", "madvise"]))

    def test_vm_marked_off_is_shut_down(self):
        """전원 끔인 VM 은 켜져 있으면 잡 시작 10분 전에 끈다. 그 VM 에는 접속하지 않는다. 구성 스냅샷에는 그 VM 의 VM 설정만 남고
        Guest OS 값은 비어 있다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a"), vm("esx01ev02", power="off")], seq_start="10:17")])
        self.assertIn("OFF 001 esx01ev02\n", res["plan"])
        rc, out = e.tcrun("check", plan)
        self.assertIn("TCGEN-POWER esx01ev02 now=on want=off  [change]", out)
        self.assertIn("[ok] settings and VM power: 1 VM(s) to power off (compared with the hosts as they are now)", out)
        self.assertIn("[ok] vm esx01ev02: the case wants it powered off. No login.", out)
        self.assertIn("CHECK RESULT: OK", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, [
            "==== case o1 (1 of 1) begin. VMs: esx01ev01 esx01ev02 (powered off for this case: esx01ev02). Job start 2026-11-20 10:17",
            "settings and VM power: 1 VM(s) to power off. They are changed at 2026-11-20 10:07:00 (10 min before the job start 2026-11-20 10:17)",
            "vm esx01ev02: shut down (the case wants it off)", "vm esx01ev01: powered on already",
            "vm esx01ev01: 1GB pages are not set for this VM", "vm esx01ev01: ssh login works",
            "VM power: done. Every VM was on already", "registered: case o1, job start 2026-11-20 10:17, VMs: esx01ev01",
            "config snapshot (before the job of this case. Powered off by this case: esx01ev02): ",
            "(VM settings of 2 VM(s), guest OS settings of 1 VM(s))", "==== case o1 done"])
        off = self.log_time("vm esx01ev02: shut down")
        self.assertTrue(epoch("2026-11-20 10:07:00") <= off < epoch("2026-11-20 10:10:00"), "VM 끄기는 잡 시작 10분 전: %s" % off)
        self.assertEqual(e.esx_vms("esx01")["esx01ev02"]["state"], "off")
        self.assertNotIn("vm esx01ev02", self.ssh_calls(), "끔인 VM 에는 접속하지 않음")
        r = self.results()[0]
        self.assertEqual((r["status"], r["vms_on"], r["apply"]), ("registered", "", "none"))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:17:00")])
        self.assertEqual(len(cron.fired), 1)
        raw, rows, t = self.snap("001", "o1")
        self.assertEqual((t[("VM 기본 정보", "power state")], t[("Guest OS", "THP enabled")], t[("VM 기본 정보", "cpu")]),
                         (["on", "off"], ["madvise", "(없음)"], ["8", "8"]))
        self.assertEqual(t[("Guest OS", "기준 시각 (게스트 조회)")][1], "(없음)")

    def test_backing_that_stops_pauses_and_keeps_the_settings(self):
        """1GB page backing 이 늘지 않고 실패 수가 있으면 멈춘다. 설정은 되돌리지 않고 VM 은 켜진 채로 둔다. 등록하지 못한 케이스는
        등록 기록에 남는다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"lpage_rate": 1, "lpage_cap": 8, "lpage_failed": 3})],
                  vmx={"esx01ev01": fakeenv.vmx_1g("esx01ev01")})
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a")], esx_profiles=["numa0"], seq_start="10:14"),
                              case("s2", [vm("esx01ev01", "job_a")], seq_start="10:32")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("vm esx01ev01: the 1GB page backing stopped at 8 of 16 1GB pages with 3 failed attempts", st["text"])
        self.assertIn("The settings of case s1 stay applied.", st["text"])
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0", "설정은 되돌리지 않음")
        self.assertEqual(e.esx_vms("esx01")["esx01ev01"]["state"], "on", "VM 은 켜진 채로 둠")
        self.assertNotIn("rollback:", log)
        self.assertIn("run ended (paused) at case s1. Not finished: s1 s2", log)
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.results()[0]["apply"], "changed")
        jobs = self.jobs()
        self.assertEqual([j[:3] for j in jobs], [("s1", "esx01ev01", "2026-11-20 10:14"), ("s2", "esx01ev01", "2026-11-20 10:32")])
        self.assertTrue(all(j[3].startswith("not registered (the run ended as paused: vm esx01ev01: the 1GB page backing stopped at 8 of 16")
                            for j in jobs), jobs)


class MonitoringTests(TcrunBase):
    """모니터링 시작과 중지, 그리고 끝난 실행이 호스트에 남긴 줄을 다음 실행이나 tcrun stop 이 지우는 흐름."""

    def test_monitoring_start_time_that_has_passed(self):
        """줄을 등록할 때 모니터링 시작 시각(잡 시작 N분 전)이 이미 지났으면 1분 이상 남은 첫 분에 모니터링을 시작한다(잡 시작보다 늦지
        않다). 잡 시작 시각은 그대로다. 로그에 WARN 을 남기고 등록 기록에 모니터링이 잡 시작 몇 분 전에 시작하는지 적는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("m1", [vm("esx01ev01", "job_a", ["gmon"])], esx_monitors=["hmon"], seq_start="10:06", max_wait="0:03")],
                             mon_lead_min=7)
        self.assertIn("SET MON_LEAD_MIN 7\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        m = re.search(r"(?m)^(\S+ \S+) WARN case m1: the monitoring start time 2026-11-20 09:59:00 has passed or is less than 1 min away\. "
                      r"The monitoring starts at (\S+ \S+)\. The job start 2026-11-20 10:06 is kept\.$", log)
        self.assertTrue(m, log)
        at, mon = epoch(m.group(1)), epoch(m.group(2))
        self.assertEqual(mon % 60, 0, "분 단위")
        # 시작 시각은 줄을 등록하려던 때(로그 줄을 쓰기 조금 전)를 기준으로 1분 이상 남은 첫 분이다
        self.assertTrue(30 <= mon - at < 120, "1분 이상 남은 첫 분: %s" % m.group(0))
        self.assertLessEqual(mon, epoch("2026-11-20 10:06:00"), "잡 시작보다 늦지 않음")
        note = "monitoring starts %d min before the job start. The setting is 7 min" % ((epoch("2026-11-20 10:06:00") - mon) // 60)
        self.assertIn("registered: case m1, job start 2026-11-20 10:06, monitoring start %s, monitoring stop 2026-11-20 10:09:00, "
                      "VMs: esx01ev01 (%s)" % (m.group(2), note), log)
        self.assertEqual(self.jobs(), [("m1", "esx01ev01", "2026-11-20 10:06", "registered (%s)" % note)])
        r = self.results()[0]
        self.assertEqual((r["monitor_start"], r["job_start"], r["monitor_stop"], r["status"]),
                         (m.group(2), "2026-11-20 10:06:00", "2026-11-20 10:09:00", "registered"))
        e.wait_for(lambda: self.fired(cron, "esx01", "mon.sh stop h"), seconds=120, what="모니터링 중지(10:09)")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh start g"), [mon])
        self.assertEqual(self.fired(cron, "esx01", "mon.sh start h"), [mon])
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:06:00")], "잡은 적은 시각에 시작")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop g"), [epoch("2026-11-20 10:09:00")])
        self.assertEqual(len(cron.fired), 5, cron.fired)

    def test_late_change_and_late_monitoring_are_both_marked_in_the_record(self):
        """설정을 바꿀 시각이 지나서 바로 바꾸고, 그 사이 모니터링 시작 시각도 지난 경우: 잡은 적은 시각에 등록하고 등록 기록에는 두 가지를
        함께 적는다(설정을 잡 시작 몇 분 전에 바꿨는지, 모니터링이 잡 시작 몇 분 전에 시작하는지)."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("b1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])], seq_start="10:09", max_wait="0:02")], mon_lead_min=9)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertRegex(log, r"WARN settings and VM power: 2 value\(s\) differ\. The time to change them \(2026-11-20 09:59:00, 10 min before "
                              r"the job start 2026-11-20 10:09\) has passed: they are changed now, \d min before the job start")
        m = re.search(r"WARN case b1: the monitoring start time 2026-11-20 10:00:00 has passed or is less than 1 min away\. "
                      r"The monitoring starts at (\S+ \S+)\. The job start 2026-11-20 10:09 is kept\.", log)
        self.assertTrue(m, log)
        mon = epoch(m.group(1))
        self.assertLessEqual(mon, epoch("2026-11-20 10:09:00"))
        (name, vmn, at, text), = self.jobs()
        self.assertEqual((name, vmn, at), ("b1", "esx01ev01", "2026-11-20 10:09"))
        self.assertRegex(text, r"^registered \(settings changed \d min before the job start\. The setting is 10 min\. "
                               r"monitoring starts \d min before the job start\. The setting is 9 min\)$")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh start g"), [mon])
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:09:00")], "잡은 적은 시각에 시작")

    def test_stop_removes_the_lines_that_a_finished_run_left(self):
        """마지막 케이스에 최대 대기가 없으면 모니터링 중지 시각이 없다. 실행은 마지막 잡 시작 1분 뒤에 done 으로 끝나고 줄과 모니터링이
        남는다. status 가 남은 줄을 알리고, tcrun stop 이 줄을 지우고 모니터링을 중지 명령으로 끝낸 뒤 실행 기록을 지운다. 실행 중인
        잡은 건드리지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_long", ["gmon"])], esx_monitors=["hmon"], seq_start="10:06")])
        self.assertNotIn("MSTOP", res["plan"])
        self.assertTrue(any("모니터링 중지 시각이 없음" in n for n in res["notes"]), res["notes"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("The crontab lines stay on the hosts until the next run of this ESX host registers its first lines, or sh %s/tcrun "
                      "stop esx01" % e.tcgen, st["text"])
        self.assertGreaterEqual(self.log_time("run finished"), epoch("2026-11-20 10:07:00"), "진행은 마지막 잡 시작 1분 뒤에 끝남")
        self.assertEqual(self.results()[0]["monitor_stop"], "")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "모니터링 시작 줄과 잡 줄이 남음(중지 줄은 등록하지 않음)")
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : done" % RID, out)
        self.assertIn("crontab lines on the hosts: case o1. To remove them: sh %s/tcrun stop esx01" % e.tcgen, out)
        rc, out = e.tcrun("stop")
        self.assertEqual(rc, 0, out)
        for word in ("== run %s esx esx01: ended as done. Removing the crontab lines that it left on the hosts" % RID,
                     "   crontab lines of case o1 removed (job start 2026-11-20 10:06)", "monitoring stop: vm esx01ev01 (exit 0)",
                     "monitoring stop: esx esx01 (exit 0)",
                     "   the crontab lines of 1 case(s) were removed: their jobs that have not started will not start. Jobs that run now "
                     "are not touched.", "   removed from tcrun status. The log stays: %s/tcrun-%s-esx01.log" % (e.log, RID)):
            self.assertIn(word, out)
        self.assertNotIn(": stopping (", out, "끝난 실행에는 끝낼 진행이 없음")
        self.assertEqual(out.rstrip().split("\n")[-1],
                         "stop finished: tcrun status shows no run now. The logs and records stay in the log folder.")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "VM 모니터링은 중지 명령을 한 번 실행")
        self.assertEqual(self.mon_words("esx01", esx=True), [["start", "h"], ["stop", "h"]])
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop"), [], "중지는 crontab 줄이 아니라 stop 이 실행한 명령")
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "실행 중인 잡은 끝내지 않음")
        self.assertEqual(self.jobs(), [("o1", "esx01ev01", "2026-11-20 10:06", "registered")], "잡이 이미 시작했으므로 기록에 더하는 행이 없음")
        self.assertIn("sh tcrun stop removed the crontab lines that this run left. The run is removed from tcrun status, this log stays",
                      self.log(), "로그는 남고 지운 사실을 적음")
        self.assert_gone()
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM")
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")

    def test_next_run_removes_the_lines_and_stops_the_monitoring(self):
        """끝난 실행이 줄과 모니터링을 남겼을 때 같은 ESX 호스트의 다음 실행: 설정 변경이 없으면 시작하자마자 자기 줄을 등록하고, 첫 잡 시작
        N분 전(여기서는 8분)에 앞 실행의 줄을 지우고 모니터링을 중지 명령으로 끝낸다. 점검(P)은 그 호스트의 VM 에 접속하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a", ["gmon"])], esx_monitors=["hmon"], seq_start="10:06")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2)
        res2, plan2 = self.gen([case("n1", [vm("esx01ev01", "job_a")], seq_start="10:22")], now=GEN + dt.timedelta(minutes=8),
                               seq_prep_lead_min=8)
        rid2 = res2["run_id"]
        n0 = e.vm_logins()
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[ok] esx esx01: crontab lines of run %s are still on the hosts (case o1). This run removes them 8 min before its first "
                      "job start. This check does not log in to the VMs of this ESX host (a job may run)" % RID, out)
        self.assertIn("[ok] vm esx01ev01: not logged in to by this check (run %s, see above). The run looks at it when it starts." % RID, out)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertEqual(e.vm_logins(), n0, "점검은 VM 에 접속하지 않음")
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % rid2, out)
        self.wait_text("to remove the crontab lines that run(s) %s left (8 min before the job start 2026-11-20 10:22 of case n1)" % RID, rid=rid2)
        st2 = e.state(rid2, "esx01")
        self.assertEqual((st2["state"], st2["step"]), ("running", "wait-cleanup"))
        self.assertIn("waiting until 2026-11-20 10:14:00 to remove the crontab lines", st2["text"])
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen o1 esx01ev01"] * 2 + ["tcgen n1 esx01ev01"],
                         "자기 줄은 시작하자마자 등록하고 앞 실행의 줄은 아직 그대로")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"]], "앞 실행의 모니터링은 아직 실행 중")
        self.wait_state("done", seconds=600, rid=rid2)
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "run(s) %s left crontab lines on the hosts of this ESX host. They are removed 8 min before the first job start of this run" % RID,
            "settings and VM power: already as wanted. Nothing is changed for case n1",
            "registered: case n1, job start 2026-11-20 10:22, VMs: esx01ev01", "registration: 1 of 1 case(s) of this part",
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "removed the lines of this case: vm esx01ev01", "removed the lines of this case: esx esx01",
            "monitoring stop commands run on vm esx01ev01 (exit 0)", "monitoring stop commands run on esx esx01 (exit 0)",
            "the crontab lines of 1 case(s) of run %s are removed" % RID, "-config-001-n1.csv", "==== case n1 done"])
        self.assertLess(self.log_time("registered: case n1", rid=rid2), epoch("2026-11-20 10:14:00"), "자기 줄은 시작하자마자 등록")
        removed = self.log_time("Removing them now", rid=rid2)
        self.assertTrue(epoch("2026-11-20 10:14:00") <= removed < epoch("2026-11-20 10:16:00"), "앞 실행의 줄은 첫 잡 시작 8분 전에 지움")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "앞 실행의 모니터링을 중지 명령으로 끝냄")
        self.assertEqual(self.mon_words("esx01", esx=True), [["start", "h"], ["stop", "h"]])
        self.assertEqual(self.tc_lines("esx01ev01"), ["22 10 20 11 * sh %s/job.sh simA 100 # tcgen n1 esx01ev01" % e.bin])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertRegex(st["text"], r"^all 1 case\(s\) finished\. The crontab lines of the run were removed \S+ \S+ by run %s$" % rid2)
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:06:00"), epoch("2026-11-20 10:22:00")])
        self.assertEqual(self.jobs(), [("o1", "esx01ev01", "2026-11-20 10:06", "registered")], "앞 실행의 등록 기록은 그대로")
        self.assertNotIn("crontab lines on the hosts: case o1", e.tcrun("status", "-a")[1])

    def test_case_with_the_same_name_in_the_next_run(self):
        """앞 실행이 남긴 줄 가운데 다음 실행의 케이스와 이름이 같은 케이스의 줄만 다음 실행을 시작하자마자 지운다(crontab 줄의 표시가 같기
        때문이다). 이름이 다른 케이스의 줄은 다음 실행의 첫 잡 시작 N분 전(여기서는 6분)에 지우고, 그때 다음 실행이 등록한 같은 이름의
        줄은 남는다. 중지 시각이 이미 지난 모니터링에는 중지 명령을 다시 실행하지 않는다."""
        e = self.e
        self.two_vms()
        c1 = case("same", [vm("esx01ev01", "job_a")], seq_start="10:06")
        c2 = case("other", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:11", max_wait="0:02")
        res, plan = self.gen([c1, c2])
        self.assertIn("MSTOP 002 %d 2026-11-20 10:13\n" % epoch("2026-11-20 10:13:00"), res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen same esx01ev01"] + ["tcgen other esx01ev01"] * 3)
        res2, plan2 = self.gen([case("same", [vm("esx01ev01", "job_a")], seq_start="10:22")], now=GEN + dt.timedelta(minutes=13),
                               seq_prep_lead_min=6)
        rid2 = res2["run_id"]
        p = e.tcrun_bg("run", plan2, "esx01")
        self.wait_text("waiting until 2026-11-20 10:16:00 to remove the crontab lines that run(s) %s left" % RID, rid=rid2)
        new_line = "22 10 20 11 * sh %s/job.sh simA 100 # tcgen same esx01ev01" % e.bin
        lines = self.tc_lines("esx01ev01")
        self.assertEqual([x.split(" # ")[1] for x in lines], ["tcgen other esx01ev01"] * 3 + ["tcgen same esx01ev01"], lines)
        self.assertEqual(lines[-1], new_line, "이름이 같은 앞 실행의 줄(10:06)은 지우고 이번 실행의 줄(10:22)을 등록")
        with open(os.path.join(e.tcgen, "runs", RID, "esx01.work", "times")) as f:
            self.assertEqual([x.split()[0] for x in f.read().splitlines()], ["002"], "앞 실행의 줄 목록에는 이름이 다른 케이스만 남음")
        self.assertEqual(p.wait(timeout=300), 0, self.ctx())
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "run(s) %s left crontab lines on the hosts of this ESX host. They are removed 6 min before the first job start of this run" % RID,
            "removed the lines of this case: vm esx01ev01", "the crontab lines of 1 case(s) of run %s are removed" % RID,
            "registered: case same, job start 2026-11-20 10:22, VMs: esx01ev01",
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "removed the lines of this case: vm esx01ev01", "the crontab lines of 1 case(s) of run %s are removed" % RID,
            "==== case same done"])
        self.assertLess(self.log_time("registered: case same", rid=rid2), epoch("2026-11-20 10:16:00"))
        self.assertGreaterEqual(self.log_time("Removing them now", rid=rid2), epoch("2026-11-20 10:16:00"), "이름이 다른 케이스의 줄은 6분 전에")
        self.assertNotIn("monitoring stop commands run", log2, "중지 시각(10:13)이 지난 모니터링에는 중지 명령을 실행하지 않음")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "중지는 crontab 줄이 한 번 실행한 것뿐")
        self.assertEqual(self.tc_lines("esx01ev01"), [new_line], "이번 실행이 등록한 같은 이름의 줄은 남음")
        self.assertEqual(self.fired(cron, "esx01ev01", "# tcgen same "), [epoch("2026-11-20 10:06:00"), epoch("2026-11-20 10:22:00")])
        st = e.state(RID, "esx01")
        self.assertRegex(st["text"], r"^all 2 case\(s\) finished\. The crontab lines of the run were removed \S+ \S+ by run %s$" % rid2)

    def test_next_run_that_changes_settings_removes_the_lines_first(self):
        """다음 실행의 첫 케이스가 설정을 바꿔야 하면: 설정을 바꾸기 직전(잡 시작 10분 전)에 앞 실행이 남긴 줄을 모두 지우고 모니터링을
        끝낸 뒤 VM 을 끈다. 그 전까지 앞 실행의 줄과 모니터링은 그대로다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:06")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        res2, plan2 = self.gen([case("n1", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:22")], now=GEN + dt.timedelta(minutes=8))
        rid2 = res2["run_id"]
        e.tcrun_bg("run", plan2, "esx01")
        self.wait_text("waiting until 2026-11-20 10:12:00 to change the settings and the VM power for case n1", rid=rid2)
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen o1 esx01ev01"] * 2, "앞 실행의 줄은 아직 그대로")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"]])
        self.wait_log("registration: 1 of 1", seconds=300, rid=rid2)
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "run(s) %s left crontab lines on the hosts of this ESX host. They are removed 10 min before the first job start of this run" % RID,
            "settings and VM power: 2 value(s) differ. They are changed at 2026-11-20 10:12:00 (10 min before the job start 2026-11-20 10:22)",
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "removed the lines of this case: vm esx01ev01", "monitoring stop commands run on vm esx01ev01 (exit 0)",
            "the crontab lines of 1 case(s) of run %s are removed" % RID,
            "settings wanted by this case:", "settings: applied. The VMs of this case are powered off now.",
            "registered: case n1, job start 2026-11-20 10:22, VMs: esx01ev01"])
        self.assertGreaterEqual(self.log_time("Removing them now", rid=rid2), epoch("2026-11-20 10:12:00"))
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]])
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen n1 esx01ev01"])
        self.assertRegex(e.state(RID, "esx01")["text"], r"The crontab lines of the run were removed \S+ \S+ by run %s$" % rid2)


class StopTests(TcrunBase):
    """stop 은 진행을 끝내고(다음 확인 시점), 등록한 crontab 줄을 지우고, 시작된 모니터링을 중지 명령으로 끝낸 뒤 실행 기록을 지운다.
    실행 중인 잡은 건드리지 않고 적용한 설정은 그대로 둔다. 지운 실행은 status 에 나오지 않고 같은 S 블록으로 다시 시작하지 않는다
    (사용자 결정 2026-11-20 "Stop으로 멈추고 cancle작업까지 둘다 허고 cancle 없애줘", "삭제하면 trunk status에 없어져야지")."""

    def test_stop_while_waiting_for_a_settings_change(self):
        """설정을 바꿀 시각을 기다리는 동안의 stop(아무것도 시작하기 전): 진행은 다음 확인 때 끝난다. 아무것도 바꾸지 않았고 줄도 등록하지
        않았다. 등록 기록에는 등록하지 못한 케이스가 남는다. 실행 기록을 지워 status 에 나오지 않고, 같은 S 블록을 다시 붙여넣어도
        시작하지 않는다. 같은 ESX 호스트의 새 실행은 시작할 수 있다."""
        e = self.e
        self.two_vms()
        c1 = case("h1", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:30")
        c2 = case("h2", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:40")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("waiting until 2026-11-20 10:20:00 to change the settings and the VM power for case h1 (job start 2026-11-20 10:30)")
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("step=wait-prepare", out)
        rc, out = e.tcrun("stop", "esx09")
        self.assertEqual(out.strip(), "nothing to stop for esx09: tcrun status shows no run of this ESX host")
        self.assertEqual(e.state(RID, "esx01")["state"], "running", "다른 ESX 호스트의 stop 은 이 진행을 건드리지 않음")
        rc, out = e.tcrun("stop")
        self.assertEqual(rc, 0, out)
        self.assertEqual(p.wait(timeout=120), 1, self.ctx(out))
        why = "before the settings of case h1 were changed. Nothing of this case was done."
        for word in ("== run %s esx esx01: stopping (state running, case h1, step wait-prepare)" % RID,
                     "   the runner (pid %d) ended: %s" % (p.pid, why), "   no crontab lines of this run were on the hosts",
                     "   stopped. Settings that this run applied stay as they are.",
                     "   removed from tcrun status. The log stays: %s/tcrun-%s-esx01.log" % (e.log, RID),
                     "stop finished: tcrun status shows no run now. The logs and records stay in the log folder."):
            self.assertIn(word, out)
        self.assertLess(e.now(), epoch("2026-11-20 10:20:00"), "변경 시각까지 기다리지 않고 멈춤")
        self.assertEqual(self.change_calls(), [])
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.jobs(), [("h1", "esx01ev01", "2026-11-20 10:30", "not registered (the run ended as stopped: %s)" % why),
                                       ("h2", "esx01ev01", "2026-11-20 10:40", "not registered (the run ended as stopped: %s)" % why)])
        self.assertTrue(self.results()[0]["status"].startswith("stopped: " + why))
        self.assertEqual(self.minutes(), {}, "등록하지 않은 케이스의 시작 시각 기록을 지움")
        self.assert_order(self.log(), ["STOPPED on request: " + why, "run ended (stopped) at case h1. Not finished: h1 h2",
                                       "STOP (sh tcrun stop). state was running, case h1, step wait-prepare",
                                       "STOPPED by sh tcrun stop. Settings that this run applied stay as they are."])
        self.assertNotIn("To go on with the others", self.log(), "stop 이 줄을 지우므로 남은 줄 안내를 쓰지 않음")
        self.assert_gone()
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM")
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")
        e.put_plan(res)                                  # 같은 S 블록을 다시 붙여넣음(블록이 plan 을 다시 쓴다)
        rc, out = e.tcrun("start", plan)
        self.assertIn("==== esx01: run %s was ended by sh tcrun stop on this jump VM. Not started again. Generate again in tcgen to run "
                      "again." % RID, out)
        self.assertNotIn("Type yes or no", out, "다시 시작하지 않으므로 묻지 않음")
        self.assertEqual(e.state(RID, "esx01"), {})
        res2, plan2 = self.gen([case("h3", [vm("esx01ev02", "job_a")], seq_start="10:30")], now=GEN + dt.timedelta(seconds=30))
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % res2["run_id"], out)

    def test_stop_after_the_lines_are_registered(self):
        """줄을 등록한 뒤 구성 스냅샷을 기다리는 동안의 stop(모니터링과 잡이 시작하기 전): 줄을 지워서 모니터링 시작, 잡, 모니터링
        중지가 하나도 실행되지 않는다. 시작 전이므로 중지 명령은 실행하지 않는다. 등록 기록에 'removed before the job start' 행을
        더하고, 아직 오지 않은 시작 시각 기록을 지운다. 구성 스냅샷은 읽지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("y1", [vm("esx01ev01", "job_a", ["gmon"])], esx_monitors=["hmon"], seq_start="10:10", max_wait="0:03")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("waiting until 2026-11-20 10:07:00 to take the config snapshot of case y1")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 3, "모니터링 시작, 잡, 모니터링 중지")
        self.assertEqual(len(e.esx_crontab("esx01")), len(fakeenv.ESX_DEFAULT_CRON.splitlines()) + 2)
        rc, out = e.tcrun("stop", "esx01")
        self.assertEqual(rc, 0, out)
        self.assertEqual(p.wait(timeout=60), 1, self.ctx(out))
        for word in ("== run %s esx esx01: stopping (state running, case y1, step wait-snapshot)" % RID,
                     "   the runner (pid %d) ended: before the config snapshot of case y1" % p.pid,
                     "   crontab lines of case y1 removed (job start 2026-11-20 10:10)",
                     "   the crontab lines of 1 case(s) were removed: their jobs that have not started will not start. Jobs that run now "
                     "are not touched.", "   stopped. Settings that this run applied stay as they are.",
                     "stop finished: tcrun status shows no run of this ESX host now. The logs and records stay in the log folder."):
            self.assertIn(word, out)
        self.assertNotIn("monitoring stop:", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        self.assertEqual(self.minutes(), {}, "시작 시각이 아직 오지 않았으므로 기록을 지움")
        self.assertEqual(self.jobs(), [("y1", "esx01ev01", "2026-11-20 10:10", "registered"),
                                       ("y1", "esx01ev01", "2026-11-20 10:10", "removed before the job start (sh tcrun stop)")])
        self.assertTrue(self.results()[0]["status"].startswith("stopped: before the config snapshot of case y1"))
        self.assertEqual(self.snap_files(), [], "멈춘 뒤에는 구성 스냅샷을 남기지 않음")
        self.assert_order(self.log(), ["STOPPED on request: before the config snapshot of case y1",
                                       "STOP (sh tcrun stop). state was running, case y1, step wait-snapshot",
                                       "removed the lines of this case: vm esx01ev01", "removed the lines of this case: esx esx01",
                                       "STOPPED by sh tcrun stop. Settings that this run applied stay as they are."])
        self.assert_gone()
        self.wait_clock("2026-11-20 10:13:30")
        self.assertEqual(cron.fired, [], "10:09 모니터링 시작, 10:10 잡, 10:13 모니터링 중지 모두 실행되지 않음")
        self.assertEqual(self.mon_words("esx01ev01") + self.mon_words("esx01", esx=True), [])

    def test_stop_while_a_job_runs(self):
        """잡이 실행되는 동안의 stop: 진행은 다음 케이스의 설정 변경 시각을 기다리다가 끝난다. 실행 중인 잡은 끝내지 않는다. 시작된
        모니터링은 중지 명령을 한 번 실행하고 그 케이스의 줄(모니터링 중지 줄 포함)을 지운다. 등록하지 못한 다음 케이스는 등록 기록에
        남는다."""
        e = self.e
        self.two_vms()
        c1 = case("r1", [vm("esx01ev01", "job_long", ["gmon"])], seq_start="10:07")
        c2 = case("r2", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])], seq_start="10:40", max_wait="0:05")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: self.fired(cron, "esx01ev01", "job.sh simLong"), seconds=300, what="r1 의 잡 시작(10:07)")
        self.wait_text("waiting until 2026-11-20 10:30:00 to change the settings and the VM power for case r2")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 3, "r1 의 모니터링 시작, 잡, 모니터링 중지")
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=120), 1, self.ctx(out))
        why = "before the settings of case r2 were changed. Nothing of this case was done."
        self.assertIn("the runner (pid %d) ended: %s" % (p.pid, why), out)
        self.assertIn("crontab lines of case r1 removed (job start 2026-11-20 10:07)", out)
        self.assertEqual(out.count("monitoring stop: vm esx01ev01 (exit 0)"), 1, out)
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "잡은 계속 실행 중")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "시작된 모니터링을 중지 명령으로 끝냄")
        self.assertEqual(self.tc_lines("esx01ev01"), [], "모니터링 중지 줄까지 지움")
        self.assertEqual(self.change_calls(), [])
        self.assertEqual(self.jobs(), [
            ("r1", "esx01ev01", "2026-11-20 10:07", "registered"),
            ("r2", "esx01ev01", "2026-11-20 10:40", "not registered (the run ended as stopped: %s)" % why)],
            "이미 시작한 r1 의 잡에는 행을 더하지 않음")
        self.assertEqual([r["status"][:30] for r in self.results()], ["registered", "stopped: before the settings o"])
        self.assertEqual(sorted(self.minutes()), [int(epoch("2026-11-20 10:07:00"))], "지난 r1 의 시작 시각 기록은 남고 r2 의 기록은 지움")
        self.assertIn("run ended (stopped) at case r2. Not finished: r2", self.log())
        self.assert_gone()

    def test_stop_while_waiting_for_the_last_job_start(self):
        """모든 케이스를 등록하고 마지막 잡 시작을 기다리는 동안의 stop: 줄을 지워서 그 잡은 시작하지 않는다. 남은 줄이 없으므로 같은
        ESX 호스트의 새 실행은 바로 시작할 수 있다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("w1", [vm("esx01ev01", "job_a")], seq_start="10:08")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("every case is handled. Waiting until 1 min after the last job start (2026-11-20 10:08:00), then this runner ends",
                       seconds=300)
        self.assertEqual(e.state(RID, "esx01")["step"], "wait-last-job")
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=120), 1, self.ctx(out))
        self.assertIn("the runner (pid %d) ended: while the runner waited for the last job start" % p.pid, out)
        self.assertIn("crontab lines of case w1 removed (job start 2026-11-20 10:08)", out)
        self.assertEqual([r["status"] for r in self.results()], ["registered"])
        self.assertEqual(self.jobs(), [("w1", "esx01ev01", "2026-11-20 10:08", "registered"),
                                       ("w1", "esx01ev01", "2026-11-20 10:08", "removed before the job start (sh tcrun stop)")])
        self.assertIn("run ended (stopped) after every case was handled: 1 job case(s) registered, 0 not registered (see %s/tcrun-%s-jobs.csv)"
                      % (e.log, RID), self.log())
        self.assertNotIn("Not finished", self.log(), "모든 케이스를 처리한 뒤의 stop 에는 끝나지 않은 케이스가 없음")
        self.assertNotIn("To go on with the others", self.log())
        self.assertEqual(self.snap_files(), ["tcrun-%s-esx01-config-001-w1.csv" % RID])
        self.assert_gone()
        res2, plan2 = self.gen([case("w2", [vm("esx01ev02", "job_a")], seq_start="10:30")], now=GEN + dt.timedelta(seconds=30))
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % res2["run_id"], out, "남은 줄이 없으므로 새 실행이 바로 시작함")
        self.wait_clock("2026-11-20 10:08:30")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [], "지운 w1 의 잡(10:08)은 시작하지 않음")


class StopDuringPowerTests(TcrunBase):
    """stop 은 VM 을 켜고 기다리는 동안에도 바로 적용된다. 설정 적용(VM 종료, 값 변경) 중에는 그 작업이 끝날 때까지 기다린다. 설정은
    되돌리지 않는다(되돌리면 VM 을 다시 끄게 된다)."""

    def test_stop_while_the_settings_are_applied_waits_for_that_task(self):
        """설정 적용 중의 stop: ESX 호스트에서 하는 그 작업은 60초가 넘어도 끝날 때까지 기다린다(작업 중에 끊지 않는다). 진행은 적용을
        마친 뒤 끝나고, 값을 바꾼 케이스의 VM 은 꺼진 채로 남는다. 줄은 등록하지 않았다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"shutdown_sec": 100})])
        res, plan = self.gen([case("a1", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:14")], seq_shutdown_wait_min=3)
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("comparing and applying settings on esx01", seconds=300)
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=60), 1, self.ctx(out))
        self.assertIn("the runner changes settings or VM power on the ESX host (step apply). tcrun stop waits until that task has ended", out)
        self.assertNotIn("did not end at a check", out, "작업 중에 끊지 않음")
        self.assertIn("the runner (pid %d) ended: after the settings of case a1 were applied. Its VMs are powered off and were not powered "
                      "on again." % p.pid, out)
        self.assertIn('sched.mem.pin = "TRUE"', e.vmx("esx01", "esx01ev01"), "적용은 끝까지 함")
        self.assertEqual(e.esx_vms("esx01")["esx01ev01"]["state"], "off")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assert_gone()

    def test_stop_while_waiting_for_the_ssh_login(self):
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "boot_sec": 1200}), ("esx01ev02", {"state": "off"})])
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], esx_profiles=["numa0"], seq_start="10:14")],
                             seq_boot_wait_min=30)
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: e.state(RID, "esx01").get("text", "").startswith("vm esx01ev01 is powered on. waiting for ssh login"), seconds=300,
                   what="ssh 로그인 대기")
        t0 = e.now()
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=60), 1, self.ctx(out))
        self.assertLess(e.now() - t0, 300, "부팅 대기 한도(30분)를 기다리지 않고 멈춤")
        self.assertIn("the runner (pid %d) ended: while the VMs of case s1 were being powered (stop requested while tcrun waited for the "
                      "ssh login of vm esx01ev01. The VM stays powered on). The settings of the case stay applied. VMs that were not "
                      "powered on yet stay off. No crontab line of the case was registered." % p.pid, out)
        self.assertNotIn("WARN it was changing settings", out, "진행이 스스로 끝남")
        self.assertIn("no crontab lines of this run were on the hosts", out)
        self.assertNotIn("rollback:", self.log(), "stop 은 설정을 되돌리지 않음")
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "off"))
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        jobs = self.jobs()
        self.assertEqual([j[:3] for j in jobs], [("s1", "esx01ev01", "2026-11-20 10:14"), ("s1", "esx01ev02", "2026-11-20 10:14")])
        self.assertTrue(all(j[3].startswith("not registered (the run ended as stopped: while the VMs of case s1 were being powered")
                            for j in jobs), jobs)
        self.assert_gone()

    def test_stop_while_the_power_on_task_runs(self):
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "poweron": "slow", "poweron_sec": 25})])
        res, plan = self.gen([case("s2", [vm("esx01ev01", "job_a")], seq_start="10:14")])
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: e.state(RID, "esx01").get("text") == "power on esx01ev01", seconds=300, what="켜기 작업 중")
        time.sleep(1.0)
        t0 = time.time()
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=60), 1, self.ctx(out))
        self.assertLess(time.time() - t0, 15, "켜기 작업(25초)이 끝나기를 기다리지 않고 멈춤")
        self.assertIn("stop requested while vm esx01ev01 was being powered on. Its power-on task goes on to its end on the ESX host", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assert_gone()


class StopAndStatusTests(TcrunBase):
    """stop 은 진행을 끝내고 그 실행의 crontab 줄을 지운다: 시작하지 않은 잡은 시작하지 않고, 실행 중인 잡은 건드리지 않고, 실행 중인
    모니터링은 중지 명령으로 끝낸다. 적용한 설정은 그대로 둔다. 줄을 다 지운 실행은 실행 기록에서 지운다. status 는 실행의 모든 ESX
    호스트를 보인다. v2.14 까지의 cancel 시험을 stop 으로 옮겼다."""

    def test_stop_while_a_job_runs_removes_every_line(self):
        """잡이 실행되는 동안의 stop(다음 케이스도 등록된 때): 진행을 끝내고 줄을 모두 지운다. 시작된 모니터링에는 중지 명령을 한 번
        실행하고, 실행 중인 잡은 끝내지 않는다. 아직 시작하지 않은 다음 케이스의 잡은 시작하지 않는다. 적용한 설정은 그대로 둔다."""
        e = self.e
        self.two_vms()
        c1 = case("x1", [vm("esx01ev01", "job_long", ["gmon"], ["pin"])], esx_monitors=["hmon"], seq_start="10:14")
        c2 = case("x2", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])], esx_monitors=["hmon"], seq_start="10:34")
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: self.fired(cron, "esx01ev01", "job.sh simLong"), seconds=600, what="x1 의 잡 시작(10:14)")
        self.wait_text("waiting until 2026-11-20 10:31:00 to take the config snapshot of case x2")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 5, "x1 과 x2 의 줄(x2 는 마지막 케이스라 중지 줄이 없음)")
        rc, out = e.tcrun("stop")
        self.assertEqual(rc, 0, out)
        p.wait(timeout=60)
        self.assertIn("== run %s esx esx01: stopping (state running, case x2, step wait-snapshot)" % RID, out)
        self.assertIn("the runner (pid %d) ended: before the config snapshot of case x2" % p.pid, out)
        self.assertIn("crontab lines of case x1 removed (job start 2026-11-20 10:14)", out)
        self.assertIn("crontab lines of case x2 removed (job start 2026-11-20 10:34)", out)
        self.assertEqual((out.count("monitoring stop: vm esx01ev01 (exit 0)"), out.count("monitoring stop: esx esx01 (exit 0)")), (1, 1),
                         "모니터링이 시작된 x1 에만 중지 명령을 실행")
        self.assertIn("the crontab lines of 2 case(s) were removed: their jobs that have not started will not start. Jobs that run now are "
                      "not touched.", out)
        self.assertIn("stopped. Settings that this run applied stay as they are.", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [], "VM 의 줄을 모두 지움")
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines(), "ESX 의 줄도 지움")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "VM 모니터링은 중지 명령을 한 번 실행")
        self.assertEqual(self.mon_words("esx01", esx=True), [["start", "h"], ["stop", "h"]])
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "잡은 끝내지 않음")
        self.assertIn('sched.mem.pin = "TRUE"', e.vmx("esx01", "esx01ev01"), "적용한 설정은 그대로")
        self.assertEqual(self.jobs(), [("x1", "esx01ev01", "2026-11-20 10:14", "registered"), ("x2", "esx01ev01", "2026-11-20 10:34", "registered"),
                                       ("x2", "esx01ev01", "2026-11-20 10:34", "removed before the job start (sh tcrun stop)")],
                         "이미 시작한 x1 의 잡에는 행을 더하지 않음")
        self.assertEqual(sorted(self.minutes()), [int(epoch("2026-11-20 10:14:00"))], "아직 오지 않은 x2 의 시작 시각 기록만 지움")
        self.assert_gone()
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")

    def test_stop_when_the_lines_cannot_be_removed_on_a_vm(self):
        """stop 때 한 VM 에 접속하지 못해 그 VM 의 줄을 지우지 못한 경우: 그 줄의 잡은 아직 시작할 수 있다. stop 은 지우지 못한 VM 을
        출력하고 그 케이스를 줄 목록(times)에 남기고, 그 실행을 status 에 남긴다. 접속이 되는 때에 stop 을 다시 실행하면 남은 줄만
        지우고 실행 기록을 지운다. stop 한 케이스의 잡은 어느 VM 에서도 시작하지 않는다. 등록 기록에는 VM 마다 지운 것과 지우지 못한
        것을 적는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("v1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:12")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("waiting until 2026-11-20 10:09:00 to take the config snapshot of case v1")
        with open(os.path.join(e.vm_dir("esx01ev02"), "ssh_fail"), "w") as f:
            f.write("1")                        # esx01ev02 의 다음 접속 한 번(stop 의 줄 삭제)이 실패한다
        rc, out = e.tcrun("stop")
        p.wait(timeout=60)
        self.assertIn("WARN the lines of this case could not be removed on vm esx01ev02 (ssh: connect to host esx01ev02 port 22: "
                      "Connection timed out)", self.log())
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(len(self.tc_lines("esx01ev02")), 1, "이 시험의 전제: esx01ev02 의 줄은 지우지 못해 남아 있음")
        self.assertIn("crontab lines of case v1 NOT removed on: esx01ev02 (job start 2026-11-20 10:12). What they start still starts there.", out)
        self.assertNotIn("crontab lines of case v1 removed", out, "지우지 못한 줄이 있으면 removed 라고 출력하지 않음")
        self.assertIn("WARN the crontab lines of 1 case(s) could NOT be removed on every host (see the lines above and %s/tcrun-%s-esx01.log). "
                      "This run stays in tcrun status: run sh %s/tcrun stop esx01 again when the hosts can be reached." % (e.log, RID, e.tcgen), out)
        self.assertIn("==== 1 run(s) stay in tcrun status (see the lines above). Run sh %s/tcrun stop again when the hosts can be reached."
                      % e.tcgen, out)
        self.assertNotIn("removed from tcrun status", out)
        self.assertNotIn("were removed: their jobs that have not started will not start", out)
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "stopped")
        self.assertIn("Crontab lines of 1 case(s) are STILL on a host that could not be reached: run sh %s/tcrun stop esx01 again"
                      % e.tcgen, st["text"])
        left = ("NOT removed on this VM (sh tcrun stop: the VM could not be reached): its job still starts at 2026-11-20 10:12. "
                "Run sh %s/tcrun stop esx01 again" % e.tcgen)
        self.assertEqual(self.jobs(), [("v1", "esx01ev01", "2026-11-20 10:12", "registered"), ("v1", "esx01ev02", "2026-11-20 10:12", "registered"),
                                       ("v1", "esx01ev01", "2026-11-20 10:12", "removed before the job start (sh tcrun stop)"),
                                       ("v1", "esx01ev02", "2026-11-20 10:12", left)])
        status = e.tcrun("status")[1]
        self.assertIn("== run %s  esx esx01 : stopped" % RID, status, "줄이 남은 실행은 status 에 남음")
        self.assertIn("crontab lines on the hosts: case v1. To remove them: sh %s/tcrun stop esx01" % e.tcgen, status)
        self.assertEqual(sorted(self.minutes()), [], "stop 한 실행의 시작 시각 기록은 지움")
        n1 = self.ssh_calls().count("vm esx01ev01")
        rc, out2 = e.tcrun("stop")                  # 이제 esx01ev02 에 접속된다
        self.assertIn("== run %s esx esx01: ended as stopped. Removing the crontab lines that it left on the hosts" % RID, out2)
        self.assertIn("crontab lines of case v1 removed (job start 2026-11-20 10:12)", out2)
        self.assertIn("the crontab lines of 1 case(s) were removed: their jobs that have not started will not start.", out2)
        self.assertIn("removed from tcrun status.", out2)
        self.assertEqual(self.ssh_calls().count("vm esx01ev01"), n1, "두 번째 stop 은 줄이 남은 VM 에만 접속")
        self.assertEqual(self.jobs()[4:], [("v1", "esx01ev02", "2026-11-20 10:12", "removed before the job start (sh tcrun stop)")],
                         "두 번째 stop 은 이번에 지운 VM 의 행만 더함")
        self.assert_gone()
        left = self.tc_lines("esx01ev02")
        self.wait_clock("2026-11-20 10:12:30")
        started = [(h, line.split(" # ")[1]) for h, t, line in cron.fired]
        self.assertEqual((left, started), ([], []), "두 번째 stop 뒤에도 줄이 남았거나 stop 한 케이스의 잡이 시작됨\n---- stop (1)\n%s\n"
                                                    "---- stop (2)\n%s\n---- jobs.csv\n%s" % (out, out2, self.jobs()))
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")

    def test_next_run_tries_again_when_the_lines_could_not_be_removed(self):
        """끝난 실행(done)이 남긴 줄을 다음 실행이 지울 때 한 VM 에 접속하지 못한 경우: 로그에 WARN 을 남기고 다음 실행은 계속한다.
        지우지 못한 줄은 그 실행의 줄 목록에 남아서, 그다음 실행이나 stop 이 남은 VM 의 줄만 다시 지운다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("n1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:04")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual((len(self.tc_lines("esx01ev01")), len(self.tc_lines("esx01ev02"))), (1, 1), "끝난 실행의 줄은 남아 있음")
        open(os.path.join(e.vm_dir("esx01ev02"), "down"), "w").close()          # esx01ev02 에 접속되지 않는다
        res2, plan2 = self.gen([case("n2", [vm("esx01ev01", "job_a")], seq_start="10:22")], now=GEN + dt.timedelta(minutes=6))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("run", plan2, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "registered: case n2, job start 2026-11-20 10:22, VMs: esx01ev01",
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "removed the lines of this case: vm esx01ev01",
            "WARN the lines of this case could not be removed on vm esx01ev02 (ssh: connect to host esx01ev02 port 22: Connection refused)",
            "WARN the crontab lines of 1 case(s) of run %s could not be removed on every host (see the WARN lines above). The next run of "
            "this ESX host tries again. sh %s/tcrun stop esx01 removes them too, and it ends the run that works for this ESX host"
            % (RID, e.tcgen), "==== case n2 done"])
        self.assertEqual(e.state(rid2, "esx01")["state"], "done", "줄을 지우지 못해도 다음 실행은 계속함")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:04:00"), epoch("2026-11-20 10:22:00")])
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen n2 esx01ev01"])
        self.assertEqual(len(self.tc_lines("esx01ev02")), 1, "접속하지 못한 VM 의 줄은 남아 있음")
        self.assertIn("crontab lines on the hosts: case n1. To remove them: sh %s/tcrun stop esx01" % e.tcgen, e.tcrun("status", "-a")[1])
        os.remove(os.path.join(e.vm_dir("esx01ev02"), "down"))
        n1 = self.ssh_calls().count("vm esx01ev01")
        rc, out = e.tcrun("stop", "esx01")
        self.assertIn("== run %s esx esx01: ended as done. Removing the crontab lines that it left on the hosts" % RID, out)
        self.assertIn("== run %s esx esx01: ended as done. Removing the crontab lines that it left on the hosts" % rid2, out)
        self.assertLess(out.index("== run %s esx esx01" % rid2), out.index("== run %s esx esx01" % RID), "늦은 실행부터")
        self.assertIn("crontab lines of case n1 removed (job start 2026-11-20 10:04)", out)
        self.assertEqual(self.tc_lines("esx01ev02"), [])
        self.assertEqual(self.tc_lines("esx01ev01"), [], "stop 은 n2 의 줄도 지움")
        self.assertEqual(self.ssh_calls().count("vm esx01ev01"), n1 + 1, "n1 의 줄은 esx01ev02 에서만 지움(esx01ev01 접속은 n2 의 줄 삭제 한 번)")
        self.assert_gone()
        self.assert_gone(rid2)

    def test_stop_ends_every_run_newest_first(self):
        """stop 은 진행 중이거나 대기 중인 실행을 모두 끝낸다(대기 중인 실행부터). ESX 호스트를 주면 그 호스트만 끝내고 지운다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {})])
        res, plan = self.gen([case("c1", [vm("esx01ev01", "job_long", ["gmon"])], seq_start="10:30"),
                              case("d1", [vm("esx02ev01", "job_long")], esx="esx02", seq_start="10:36")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        for h in ("esx01", "esx02"):
            self.wait_log("registration: 1 of 1", chain=h)
        res2, plan2 = self.gen([case("c2", [vm("esx01ev02", "job_a")], seq_start="10:50")], now=GEN + dt.timedelta(minutes=1))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        rc, out = e.tcrun("stop", "esx02")
        self.assertIn("== run %s esx esx02: stopping (state running, case d1, step wait-snapshot)" % RID, out)
        self.assertNotIn("esx esx01", out, "다른 호스트는 건드리지 않음")
        self.assertEqual(e.state(RID, "esx02"), {}, "esx02 의 상태 파일을 지움")
        self.assertEqual(e.state(RID, "esx01")["state"], "running")
        self.assertEqual(e.state(rid2, "esx01")["state"], "queued")
        self.assertTrue(os.path.isdir(os.path.join(e.tcgen, "runs", RID)), "esx01 이 진행 중이므로 실행 폴더는 남음")
        status = e.tcrun("status")[1]
        self.assertNotIn("esx esx02", status, "지운 호스트는 status 에 나오지 않음")
        self.assertIn("== run %s  esx esx01 : running" % RID, status)
        self.assertEqual(self.tc_lines("esx02ev01"), [])
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2)
        rc, out = e.tcrun("stop")
        self.assertLess(out.index("== run %s esx esx01: stopping (state queued" % rid2),
                        out.index("== run %s esx esx01: stopping (state running, case c1" % RID), "대기 중인 실행을 먼저 끝냄")
        why2 = "while this run was queued behind run %s. Nothing of this run was started." % RID
        self.assertIn("ended: " + why2, out)
        self.assertIn("no crontab lines of this run were on the hosts", out, "대기하던 실행은 등록한 줄이 없음")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.tc_lines("esx01ev02"), [], "대기하던 실행은 시작된 적이 없음")
        self.assertEqual([j[3] for j in self.jobs()], ["registered", "registered", "removed before the job start (sh tcrun stop)",
                                                       "removed before the job start (sh tcrun stop)"])
        self.assertEqual(self.jobs(rid2), [("c2", "esx01ev02", "2026-11-20 10:50", "not registered (the run ended as stopped: %s)" % why2)],
                         "대기하다 stop 으로 끝난 실행의 케이스도 등록 기록에 남음")
        self.assertEqual(os.listdir(os.path.join(e.tcgen, "runs")), [], "두 실행의 폴더를 지움(stop 의 작업 폴더도 남지 않음)")
        self.assertEqual(self.minutes(), {})
        time.sleep(120 / e.speed)
        self.assertEqual(e.state(rid2, "esx01"), {}, "stop 한 뒤에 대기하던 실행이 시작되지 않음")
        self.assertEqual(cron.fired, [])
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")

    def test_status_shows_every_host_of_the_run(self):
        """status 는 실행의 모든 ESX 호스트를 보인다. 멈춘 호스트는 이유를, 상태 파일이 없는 호스트는 시작하지 못했다고 보인다.
        stop 은 진행 중인 호스트를 끝내고, 끝난 호스트와 함께 실행 기록에서 지운다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {"state": "off", "poweron": "fail"})])
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_long")], seq_start="10:30"),
                              case("s2", [vm("esx02ev01", "job_a")], esx="esx02", seq_start="10:14")])
        with open(plan) as f:
            text = f.read()
        with open(plan, "w") as f:      # esx03: 진행이 시작하자마자 끝나 상태 파일을 쓰지 못하는 호스트(주소가 잘못됨)
            f.write(text.replace("CHAIN esx02\n", "CHAIN esx02\nADDR esx03 -bad\nCHAIN esx03\n").replace(
                "END 002\n", "END 002\nCASE 003 esx03 s3\nSEG 003 1\nVM 003 esx03ev01 esx03ev01\nEND 003\n"))
        e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("started: run %s esx esx01" % RID, out)
        self.assertIn("==== esx03: the runner did not start:", out)
        self.assertIn("status: sh %s/tcrun status       stop: sh %s/tcrun stop (ends the run, removes its crontab lines and stops its "
                      "monitoring)" % (e.tcgen, e.tcgen), out)
        self.assertNotIn("cancel", out)
        self.assertIn("files in %s : tcrun-%s-jobs.csv (which jobs are registered in crontab)" % (e.log, RID), out)
        self.wait_state("paused", seconds=300, chain="esx02")
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("step=wait-snapshot", out)
        self.assertIn("== run %s  esx esx02 : paused" % RID, out)
        self.assertIn("power on of vm esx02ev01 failed", out)
        self.assertIn("== run %s  esx esx03 : not started (no state file." % RID, out)
        self.assertIn("tcrun: bad ESX address in the plan for esx03", out)
        self.assertIn("   log=%s/tcrun-%s-esx01.log" % (e.log, RID), out)
        self.assertIn("stop: sh %s/tcrun stop [esx]   (ends the runs, removes their crontab lines and stops their monitoring. Then they "
                      "are not shown here)" % e.tcgen, out)
        rc, out = e.tcrun("stop")
        self.assertIn("== run %s esx esx01: stopping (state running, case s1, step wait-snapshot)" % RID, out)
        self.assertIn("crontab lines of case s1 removed (job start 2026-11-20 10:30)", out)
        self.assertIn("== run %s esx esx02: ended as paused, no crontab lines on the hosts. Removed from tcrun status." % RID, out)
        self.assertNotIn("esx esx03", out, "시작하지 못한 호스트에는 할 것이 없음")
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID)), "실행 폴더를 지움")
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM")

    def test_stop_and_the_next_run_remove_the_lines_of_a_run_made_by_tcgen_2_14(self):
        """v2.14 까지의 판으로 시작한 실행(plan 형식 2, VER 2.14, LIST 줄 없음)이 남긴 줄을 v2.15 의 다음 실행과 stop 이 지운다. 판이
        다른 plan 은 시작하지 않는다(2026-11-20: plan 형식 번호를 3으로 올린 초안에서 이 줄을 지우지 못한 것을 발견해 2로 되돌림)."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("old1", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:04")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "끝난 실행의 줄(모니터링 시작, 잡)이 남음")
        with open(plan, encoding="utf-8") as f:
            text = f.read()
        old = "".join(x for x in text.splitlines(True) if not x.startswith("LIST ")).replace("\nVER %s\n" % core.VERSION, "\nVER 2.14\n")
        self.assertTrue(old.startswith("TCGEN-PLAN 2\nVER 2.14\n"), old[:40])
        with open(plan, "w", encoding="utf-8", newline="\n") as f:
            f.write(old)
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 3, out)
        self.assertIn("==== tcrun %s, plan made by tcgen 2.14: paste block A again. Nothing started." % core.VERSION, out)
        res2, plan2 = self.gen([case("new1", [vm("esx01ev02", "job_a")], seq_start="10:22")], now=GEN + dt.timedelta(minutes=6))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("run", plan2, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(self.log(rid=rid2), [
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "removed the lines of this case: vm esx01ev01", "the crontab lines of 1 case(s) of run %s are removed" % RID])
        self.assertEqual(self.tc_lines("esx01ev01"), [], "v2.14 실행의 줄을 다음 실행이 지움")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "v2.14 실행의 모니터링도 중지")
        self.assertEqual(len(self.tc_lines("esx01ev02")), 1)
        with open(os.path.join(e.tcgen, "runs", rid2, "plan"), encoding="utf-8") as f:
            text2 = f.read()
        with open(os.path.join(e.tcgen, "runs", rid2, "plan"), "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(x for x in text2.splitlines(True) if not x.startswith("LIST ")).replace("\nVER %s\n" % core.VERSION,
                                                                                                "\nVER 2.14\n"))
        rc, out = e.tcrun("stop")
        self.assertIn("== run %s esx esx01: ended as done. Removing the crontab lines that it left on the hosts" % rid2, out)
        self.assertIn("crontab lines of case new1 removed (job start 2026-11-20 10:22)", out)
        self.assertIn("== run %s esx esx01: ended as done, no crontab lines on the hosts. Removed from tcrun status." % RID, out)
        self.assertEqual(self.tc_lines("esx01ev02"), [], "v2.14 실행의 줄을 stop 이 지움")
        self.assert_gone()
        self.assert_gone(rid2)


class QueueTests(TcrunBase):
    """같은 ESX 호스트의 실행은 한 번에 하나만 진행한다. 진행 중에 시작한 실행은 대기했다가 앞 실행이 done 으로 끝나면 스스로 시작한다."""

    def test_second_run_waits_in_the_queue_and_starts_by_itself(self):
        """앞 실행이 진행 중일 때: 점검(P)은 대기하게 된다고 알리고 그 호스트의 VM 에 접속하지 않는다. 시작(S)한 실행은 대기하고, 앞 실행이
        마지막 잡 시작 1분 뒤에 done 으로 끝나면 시작해 자기 줄을 등록한다. 앞 실행이 남긴 줄은 첫 잡 시작 10분 전에 지운다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("q1", [vm("esx01ev01", "job_b", ["gmon"])], seq_start="10:08")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("started: run %s esx esx01" % RID, out)
        self.wait_log("registration: 1 of 1")
        res2, plan2 = self.gen([case("q2", [vm("esx01ev01", "job_a")], seq_start="10:24")], now=GEN + dt.timedelta(minutes=1))
        rid2 = res2["run_id"]
        n0 = e.vm_logins()
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[ok] esx esx01: run %s still works for this ESX host until about 2026-11-20 10:09:00 (1 min after its last job "
                      "start). Block S puts this run in the queue: its lines are registered when that run has ended as done. This check "
                      "does not log in to the VMs of this ESX host (a job may run)" % RID, out)
        self.assertNotIn("is less than 2 min after the end of run", out, "q2 의 잡 시작(10:24)은 앞 실행이 끝난 뒤이므로 문제가 아님")
        self.assertIn("[ok] vm esx01ev01: not logged in to by this check (run %s, see above). The run looks at it when it starts." % RID, out)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertEqual(e.vm_logins(), n0, "점검은 VM 에 접속하지 않음")
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01   waiting for run %s of this ESX host to end (about 2026-11-20 10:09:00, 1 min after its "
                      "last job start). It starts when that has ended as done" % (rid2, RID), out)
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("== run %s  esx esx01 : queued" % rid2, out)
        self.assertEqual(self.tc_lines("esx01ev01")[-1].split(" # ")[1], "tcgen q1 esx01ev01", "대기 중에는 아무것도 등록하지 않음")
        self.wait_state("done", seconds=600, rid=rid2)
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "queued: run(s) %s still work for this ESX host. This run starts when they have ended as done" % RID,
            "queued: run %s ended as done" % RID,
            "run(s) %s left crontab lines on the hosts of this ESX host. They are removed 10 min before the first job start of this run" % RID,
            "==== case q2 (1 of 1) begin", "registered: case q2, job start 2026-11-20 10:24, VMs: esx01ev01",
            "run %s left crontab lines of its case(s) on the hosts of this ESX host. Removing them now" % RID,
            "monitoring stop commands run on vm esx01ev01 (exit 0)", "the crontab lines of 1 case(s) of run %s are removed" % RID,
            "==== case q2 done"])
        begin = self.log_time("==== case q2 (1 of 1) begin", rid=rid2)
        self.assertGreaterEqual(begin, self.log_time("run finished"), "앞 실행이 끝난 뒤에 시작")
        self.assertGreaterEqual(begin, epoch("2026-11-20 10:09:00"), "앞 실행은 마지막 잡 시작(10:08) 1분 뒤에 끝남")
        self.assertLess(self.log_time("registered: case q2", rid=rid2), epoch("2026-11-20 10:14:00"), "시작하자마자 등록")
        self.assertGreaterEqual(self.log_time("Removing them now", rid=rid2), epoch("2026-11-20 10:14:00"))
        self.assertEqual((self.results()[0]["status"], self.results(rid=rid2)[0]["status"]), ("registered", "registered"))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:08:00"), epoch("2026-11-20 10:24:00")])
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]])

    def test_queued_run_does_not_start_after_a_paused_run_and_stop_opens_the_host(self):
        """앞 실행이 done 이 아닌 상태로 끝나면 대기하던 실행은 시작하지 않는다. 앞 실행이 줄을 남긴 채 멈췄으면(그 잡이 아직 시작할 수
        있다) stop 으로 줄을 지우기 전에는 그 ESX 호스트의 새 실행을 시작하지 않는다. stop 뒤에는 같은 S 블록으로 시작한다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx01ev09")           # ESX 호스트에 등록되지 않은 VM: 둘째 케이스를 호스트와 비교할 때 실행이 멈춘다
        c1 = case("f1", [vm("esx01ev01", "job_a")], seq_start="10:12")
        c2 = case("f2", [vm("esx01ev01", "job_a"), vm("esx01ev09", "job_a")], seq_start="10:30")
        res, plan = self.gen([c1, c2], seq_snap_lead_min=6)
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.wait_log("registration: 1 of 1")
        res2, plan2 = self.gen([case("f3", [vm("esx01ev02", "job_a")], seq_start="10:40")], now=GEN + dt.timedelta(minutes=1))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        self.wait_state("paused", seconds=600, rid=rid2)
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("VM 'esx01ev09' is not registered on this host", st["text"])
        self.assertEqual(self.jobs()[0], ("f1", "esx01ev01", "2026-11-20 10:12", "registered"))
        self.assertTrue(all(j[0] == "f2" and j[3].startswith("not registered (the run ended as paused: ") for j in self.jobs()[1:]), self.jobs())
        st2 = e.state(rid2, "esx01")
        why2 = "run %s of this ESX host ended as paused, not as done. Nothing of this run was started." % RID
        self.assertIn(why2, st2["text"])
        self.assertIn("run ended (paused) before its first case. Not finished: f3", self.log(rid=rid2))
        self.assertEqual(self.tc_lines("esx01ev02"), [])
        self.assertEqual(self.results(rid=rid2), [], "시작하지 않은 실행은 결과 행이 없음")
        (name, vmn, at, text), = self.jobs(rid2)
        self.assertEqual((name, vmn, at), ("f3", "esx01ev02", "2026-11-20 10:40"))
        self.assertTrue(text.startswith("not registered (the run ended as paused: " + why2), text)
        # 멈춘 실행의 줄이 호스트에 남아 있음: 새 실행은 시작하지 않고, 점검도 문제로 알린다
        res3, plan3 = self.gen([case("f4", [vm("esx01ev02", "job_a")], seq_start="10:18")], now=GEN + dt.timedelta(minutes=7))
        rid3 = res3["run_id"]
        block = ("run %s of this ESX host ended as paused and crontab lines of its case(s) are still on the hosts (f1): their jobs may still "
                 "start. Remove them first: sh %s/tcrun stop esx01" % (RID, e.tcgen))
        rc, out = e.tcrun("start", plan3)
        self.assertIn("==== esx01: not started. " + block, out)
        self.assertEqual(e.state(rid3, "esx01"), {})
        rc, out = e.tcrun("check", plan3)
        self.assertIn("[PROBLEM] esx esx01: block S would not start this run. " + block, out)
        self.assertIn("[ok] vm esx01ev02: not logged in to by this check (run %s, see above)" % RID, out)
        self.assertRegex(out, r"CHECK RESULT: 1 problem")
        rc, out = e.tcrun("stop")
        self.assertIn("== run %s esx esx01: ended as paused. Removing the crontab lines that it left on the hosts" % RID, out)
        self.assertIn("crontab lines of case f1 removed (job start 2026-11-20 10:12)", out)
        self.assertIn("== run %s esx esx01: ended as paused, no crontab lines on the hosts. Removed from tcrun status." % rid2, out)
        self.assertNotIn(rid3, out, "시작하지 않은 실행에는 할 것이 없음")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.jobs()[-1], ("f1", "esx01ev01", "2026-11-20 10:12", "removed before the job start (sh tcrun stop)"))
        self.assert_gone()
        self.assert_gone(rid2)
        rc, out = e.tcrun("start", plan3)
        self.assertIn("started: run %s esx esx01" % rid3, out, "줄을 지운 뒤에는 같은 S 블록으로 시작")
        self.wait_state("done", seconds=600, rid=rid3)
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [], "stop 으로 지운 f1 의 잡(10:12)은 시작하지 않음")
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [epoch("2026-11-20 10:18:00")])

    def test_start_time_that_passes_while_the_run_is_queued(self):
        """대기 중인 실행의 시작 시각도 바뀌지 않는다. 앞 실행이 끝나기 전에 지나간 시작 시각의 케이스는 등록하지 않고 등록 기록에 이유를
        남기며, 아직 남은 케이스는 적은 시각에 등록한다. 점검(P)은 앞 실행이 끝나는 때보다 2분 넘게 늦지 않은 시작 시각을 문제로 알린다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("a1", [vm("esx01ev01", "job_a")], seq_start="10:10")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.wait_log("registration: 1 of 1")
        res2, plan2 = self.gen([case("b1", [vm("esx01ev02", "job_a")], seq_start="10:05"),
                                case("b2", [vm("esx01ev02", "job_a")], seq_start="10:18")], now=GEN + dt.timedelta(minutes=1))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[PROBLEM] case b1: its job start is less than 2 min after the end of run %s of this ESX host (about 2026-11-20 "
                      "10:11:00). The lines of this run are registered after that end, so this case would not be registered. To run it: "
                      "sh %s/tcrun stop esx01, then generate the cases that are left of that run together with this one."
                      % (RID, e.tcgen), out)
        self.assertNotIn("[PROBLEM] case b2", out, "b2 의 잡 시작(10:18)은 앞 실행이 끝난 뒤(10:11)의 2분 뒤보다 늦음")
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        self.wait_state("done", seconds=600, rid=rid2)
        log2 = self.log(rid=rid2)
        why = "its job start time 2026-11-20 10:05:00 has passed or is less than 1 min away"
        self.assert_order(log2, ["queued: run %s ended as done" % RID, "WARN not registered: case b1, job start 2026-11-20 10:05: " + why,
                                 "registered: case b2, job start 2026-11-20 10:18, VMs: esx01ev02",
                                 "==== case b1 done: NOT registered", "==== case b2 done. job start 2026-11-20 10:18:00"])
        self.assertGreaterEqual(self.log_time("==== case b1 (1 of 2) begin", rid=rid2), epoch("2026-11-20 10:11:00"),
                                "대기하던 실행은 앞 실행이 끝난 뒤(마지막 잡 시작 10:10 의 1분 뒤)에 시작")
        self.assertEqual(self.jobs(rid2), [("b1", "esx01ev02", "2026-11-20 10:05", "not registered (%s)" % why),
                                           ("b2", "esx01ev02", "2026-11-20 10:18", "registered")])
        self.assertIn("1 NOT registered", e.state(rid2, "esx01")["text"])
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [epoch("2026-11-20 10:18:00")], "지나간 시각의 잡은 시작하지 않고 시각을 옮기지도 않음")


class MultiHostTests(TcrunBase):
    """ESX 호스트가 여러 대인 실행, 진행끼리의 잡 시작 간격, 사라진 진행, 쓰지 않은 시작 시각 기록."""

    def test_two_esx_hosts_run_separately(self):
        """ESX 호스트 2대: 호스트별로 따로 진행하고 서로 기다리지 않는다. 같은 실행을 두 번 시작하지 않는다. 같은 호스트의 다른 실행은
        대기하고, stop 은 대기 중인 실행도 끝낸다. 등록 기록은 실행 하나에 파일 하나다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {})], addr="10.0.0.12")
        c1 = case("k1", [vm("esx01ev01", "job_long", ["gmon"])], esx_monitors=["hmon"], seq_start="10:30")
        c2 = case("k2", [vm("esx02ev01", "job_a")], esx="esx02", esx_monitors=["hmon"], seq_start="10:08", max_wait="0:02")
        res, plan = self.gen([c1, c2], esx_addr="esx02=10.0.0.12")
        self.assertEqual(res["chains"], ["esx01", "esx02"])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        self.assertIn("started: run %s esx esx01   log: %s/tcrun-%s-esx01.log" % (RID, e.log, RID), out)
        self.assertIn("started: run %s esx esx02" % RID, out)
        rc, out2 = e.tcrun("start", plan)
        self.assertEqual(out2.count("was already started on this jump VM"), 2, out2)
        res2, plan2 = self.gen([case("k3", [vm("esx01ev02", "job_a")], seq_start="10:50")], now=GEN + dt.timedelta(seconds=7))
        rid2 = res2["run_id"]
        rc, out3 = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01   waiting for run %s of this ESX host to end (about 2026-11-20 10:31:00, 1 min after its "
                      "last job start). It starts when that has ended as done" % (rid2, RID), out3)
        self.assertEqual(e.state(rid2, "esx01")["state"], "queued")
        self.wait_state("done", seconds=300, chain="esx02")
        self.assertEqual(e.state(RID, "esx01")["state"], "running", "esx02 는 esx01 을 기다리지 않고 끝남")
        self.assertIn("run finished: 1 case(s), 1 job case(s) registered, 0 not registered", self.log("esx02"))
        self.assertIn("esx esx02 (10.0.0.12)", self.log("esx02"))
        self.assertNotIn("==== case k2", self.log("esx01"), "다른 ESX 호스트의 케이스를 진행하지 않음")
        self.assertEqual([r["case"] for r in self.results("esx02")], ["k2"])
        self.assertEqual(self.fired(cron, "esx02ev01", "job.sh"), [epoch("2026-11-20 10:08:00")])
        self.assertEqual(self.fired(cron, "esx02", "mon.sh start h"), [epoch("2026-11-20 10:07:00")])
        self.assertEqual(sorted((r["case"], r["esx"], r["vm"], r["crontab"]) for r in e.jobs_rows(RID)),
                         [("k1", "esx01", "esx01ev01", "registered"), ("k2", "esx02", "esx02ev01", "registered")])
        rc, st = e.tcrun("status")
        self.assertIn("esx esx01 : running", st)
        self.assertIn("esx esx02 : done", st)
        self.assertIn("== run %s  esx esx01 : queued" % rid2, st)
        rc, out4 = e.tcrun("stop", "esx01")
        self.assertIn("== run %s esx esx01: stopping (state queued" % rid2, out4, "대기 중인 실행도 끝냄")
        self.assertIn("== run %s esx esx01: stopping (state running" % RID, out4)
        self.assertNotIn("esx esx02", out4, "다른 ESX 호스트는 건드리지 않음")
        why = "while this run was queued behind run %s. Nothing of this run was started." % RID
        self.assertIn("ended: " + why, out4)
        self.assertEqual(self.jobs(rid2), [("k3", "esx01ev02", "2026-11-20 10:50", "not registered (the run ended as stopped: %s)" % why)])
        self.assertEqual(self.tc_lines("esx01ev02"), [], "대기하다 멈춘 실행은 아무것도 등록하지 않음")
        self.assertEqual(self.tc_lines("esx01ev01"), [], "stop 은 앞 실행이 등록한 줄을 지움")
        self.assert_gone(rid2)
        st = e.tcrun("status")[1]
        self.assertIn("== run %s  esx esx02 : done" % RID, st, "다른 ESX 호스트의 진행은 status 에 남음")
        self.assertNotIn("esx esx01", st, "지운 호스트는 status 에 나오지 않음(not started 로도 보이지 않음)")
        e.wait_for(lambda: self.fired(cron, "esx02", "mon.sh stop h"), seconds=120, what="esx02 의 모니터링 중지(10:10)")
        self.assertEqual(self.fired(cron, "esx02", "mon.sh stop h"), [epoch("2026-11-20 10:10:00")])

    def test_start_near_the_start_of_another_runner_is_a_warning(self):
        """다른 진행(다른 실행, 다른 ESX 호스트)의 잡 시작과 5분보다 가까운 시작 시각: 점검(P)은 문제로 알린다. 그대로 실행하면 로그에
        WARN 만 남기고 적은 시각 그대로 등록한다(생성기는 한 번의 생성 안에서만 이런 시각을 막는다)."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {})])
        res, plan = self.gen([case("a1", [vm("esx01ev01", "job_a")], seq_start="10:10")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.wait_log("registration: 1 of 1")
        res2, plan2 = self.gen([case("b1", [vm("esx02ev01", "job_a")], esx="esx02", seq_start="10:12")], now=GEN + dt.timedelta(seconds=30))
        rid2 = res2["run_id"]
        near = "2026-11-20 10:10:00 of run %s, esx esx01, case a1" % RID
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[PROBLEM] case b1: the start time 2026-11-20 10:12 is less than 5 min from the job start %s. Job starts on this jump VM "
                      "are 5 min or more apart: change the start time in tcgen and generate again." % near, out)
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        warn = ("WARN the start time 2026-11-20 10:12 of case b1 is less than 5 min from the job start %s. The start time of the plan is kept: "
                "check the two start times." % near)
        rc, out = e.tcrun("start", plan2)
        self.assertIn("==== esx02: " + warn, out)
        self.assertIn("started: run %s esx esx02" % rid2, out)
        self.wait_state("done", seconds=600, chain="esx02", rid=rid2)
        log2 = self.log("esx02", rid2)
        self.assertIn(warn, log2)
        self.assertIn("WARN the job of run %s, esx esx01, case a1 starts 2026-11-20 10:10:00, less than 5 min from the job start "
                      "2026-11-20 10:12 of case b1. The start time of the plan is kept." % RID, log2)
        self.assertIn("registered: case b1, job start 2026-11-20 10:12, VMs: esx02ev01", log2)
        self.assertEqual(self.jobs(rid2), [("b1", "esx02ev01", "2026-11-20 10:12", "registered")])
        self.assertEqual(self.results("esx02", rid2)[0]["job_start"], "2026-11-20 10:12:00")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:10:00")])
        self.assertEqual(self.fired(cron, "esx02ev01", "job.sh"), [epoch("2026-11-20 10:12:00")], "적은 시각 그대로 시작")
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        self.assertNotIn("WARN", self.log(), "먼저 시작한 실행의 로그에는 경고가 없음")
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "minutes", ".lock")), "잠금은 남지 않음")
        # 간격 경고를 내고 유지한 시작 시각(b1, 10:12)도 기록한다: 그 뒤에 만든 실행의 점검이 이 시각과의 간격을 본다
        self.assertEqual(self.minutes(), {int(epoch("2026-11-20 10:10:00")): "run %s, esx esx01, case a1" % RID,
                                          int(epoch("2026-11-20 10:12:00")): "run %s, esx esx02, case b1" % rid2})
        res3, plan3 = self.gen([case("c1", [vm("esx01ev02", "job_a")], seq_start="10:16")], now=GEN + dt.timedelta(seconds=60))
        rc, out = e.tcrun("check", plan3)
        self.assertIn("[PROBLEM] case c1: the start time 2026-11-20 10:16 is less than 5 min from the job start 2026-11-20 10:12:00 of run %s, "
                      "esx esx02, case b1." % rid2, out)
        self.assertNotIn("from the job start 2026-11-20 10:10:00", out, "a1(10:10)과는 6분 떨어져 있음")

    def test_claims_of_a_dead_runner_and_old_claims_do_not_count(self):
        """죽은 진행이 등록하지 못하고 남긴 미래의 시작 시각 기록은 간격을 볼 때 세지 않고 지운다. 하루 넘게 지난 기록도 지운다. 등록을
        마친 잡의 기록에는 reg 파일이 생긴다."""
        e = self.e
        self.two_vms()
        self.claim("2026-11-20 10:10:00", "run 20261120-080000, esx esx08, case dead")
        dead = os.path.join(e.tcgen, "runs", "20261120-080000")
        os.makedirs(dead)
        with open(os.path.join(dead, "esx08.state"), "w") as f:
            f.write("state=running\n")
        with open(os.path.join(dead, "esx08.pid"), "w") as f:
            f.write("999999\n")
        self.claim("2026-11-18 10:00:00", "run old, esx esx09, case gone")
        res, plan = self.gen([case("m1", [vm("esx01ev01", "job_a")], seq_start="10:08")])
        rc, out = e.tcrun("check", plan)
        self.assertIn("CHECK RESULT: OK", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertNotIn("WARN", self.log())
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:08:00")])
        self.assertEqual(self.minutes(), {int(epoch("2026-11-20 10:08:00")): "run %s, esx esx01, case m1" % RID},
                         "죽은 진행의 미래 기록과 하루 넘게 지난 기록은 지움")
        self.assertTrue(os.path.exists(os.path.join(e.tcgen, "minutes", str(int(epoch("2026-11-20 10:08:00"))), "reg")),
                        "등록을 마친 잡의 기록에는 reg")
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "minutes", ".lock")), "잠금은 남지 않음")

    def test_runner_that_died_is_reported_and_blocks_a_new_run_until_stop(self):
        """진행 프로세스가 사라지면(점프 VM 재부팅 등) status 가 died 로 알린다. 그 실행의 줄이 호스트에 남아 있으므로 같은 ESX 호스트의
        새 실행은 stop 으로 줄을 지운 뒤에 시작한다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("z1", [vm("esx01ev01", "job_long")], seq_start="10:30")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_text("waiting until 2026-11-20 10:27:00 to take the config snapshot of case z1")
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : died (the runner is not running any more." % RID, out)
        self.assertIn("crontab lines on the hosts: case z1. To remove them: sh %s/tcrun stop esx01" % e.tcgen, out)
        res2, plan2 = self.gen([case("z2", [vm("esx01ev02", "job_a")], seq_start="10:10")], now=GEN + dt.timedelta(seconds=30))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("==== esx01: not started. run %s of this ESX host ended as died and crontab lines of its case(s) are still on the hosts "
                      "(z1): their jobs may still start. Remove them first: sh %s/tcrun stop esx01" % (RID, e.tcgen), out)
        self.assertEqual(e.state(rid2, "esx01"), {}, "시작하지 않았으므로 같은 S 블록을 다시 붙여넣을 수 있음")
        self.assertIn("esx esx01 : died", e.tcrun("status", "-a")[1])
        rc, out = e.tcrun("stop", "esx01")
        self.assertIn("== run %s esx esx01: the runner died. Removing the crontab lines that it left on the hosts" % RID, out)
        self.assertEqual(self.tc_lines("esx01ev01"), [], "죽은 실행이 남긴 줄을 지움")
        self.assertEqual(self.jobs()[-1], ("z1", "esx01ev01", "2026-11-20 10:30", "removed before the job start (sh tcrun stop)"))
        self.assert_gone()
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % rid2, out)
        self.wait_state("done", seconds=600, rid=rid2)
        self.assertIn("== run %s  esx esx01 : done" % rid2, e.tcrun("status")[1])
        self.assertNotIn(RID, e.tcrun("status", "-a")[1], "stop 으로 지운 실행은 -a 에도 나오지 않음")
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [epoch("2026-11-20 10:10:00")])

    def test_unused_start_times_are_given_back_when_the_run_ends(self):
        """S 블록(start)은 plan 의 시작 시각을 먼저 기록한다. 멈춘 실행은 등록하지 못한 케이스의 기록을 지워서, 다시 생성한 실행이 그
        시각을 그대로 쓸 수 있다."""
        e = self.e
        self.two_vms(vm1={"poweron": "fail", "state": "off"})
        res, plan = self.gen([case("u1", [vm("esx01ev01", "job_a")], seq_start="10:14"),
                              case("u2", [vm("esx01ev02", "job_a")], seq_start="10:30")])
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.minutes()), [int(epoch("2026-11-20 10:14:00")), int(epoch("2026-11-20 10:30:00"))],
                         "start 가 적은 시작 시각을 먼저 기록(진행은 10:04 까지 VM 전원을 맞출 시각을 기다리는 중)")
        self.wait_state("paused", seconds=300)
        e.wait_for(lambda: not os.path.exists(os.path.join(e.tcgen, "runs", RID, "esx01.pid")), what="진행 끝")
        self.assertIn("power on of vm esx01ev01 failed", e.state(RID, "esx01")["text"])
        self.assertEqual(self.minutes(), {}, "멈춘 실행의 기록을 모두 지움(등록한 잡이 없음)")


class CheckTests(TcrunBase):
    """점검(P 블록, tcrun check): 읽기만 하고 아무것도 바꾸지 않는다. 그리고 key, version, 사용법, esxssh."""

    def test_check_reports_and_changes_nothing(self):
        """점검: 시각 규칙, ESX 키 접속, 도구, VM 찾기, 현재 값과 목표 값, VM 전원, VM 접속, 시계 차이를 보이고 아무것도 바꾸지 않는다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_vm("lost01")
        e.add_esx("esx02", vms=[("esx02ev01", {})], authorized=False)
        open(os.path.join(e.vm_dir("lost01"), "down"), "w").close()
        cases = [case("n1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_a", [], ["same"])],
                      esx_profiles=["numa0"], esx_monitors=["hmon"], seq_start="10:30"),
                 case("n2", [vm("esx02ev01", "job_a", [], ["pin"])], esx="esx02", seq_start="10:40"),
                 case("n3", [vm("lost01", "job_a")], esx="", seq_start="10:50")]
        res, plan = self.gen(cases)
        before = (e.vmx("esx01", "esx01ev01"), e.vm_crontab("esx01ev01"), e.esx_crontab("esx01"))
        rc, out = e.tcrun("check", plan)
        for word in ("tcrun %s check, run %s, " % (core.VERSION, RID), "Nothing is changed by this check.",
                     "[ok] folder %s: 1 MB can be written" % e.log,
                     "[ok] start times: the jobs are registered for the start times of the plan. tcrun does not change them",
                     "[ok] settings and VM power: a case that differs from the hosts is changed 10 min before its job start (VMs shut down, "
                     "settings written, VMs powered on). Then its lines and the lines of the cases after it with the same settings are "
                     "registered at once",
                     "[ok] config snapshot: taken 3 min before the job start of every case",
                     "[ok] command at the config snapshot: none (the guest settings are not recorded in VCF Operations by the run)",
                     "== ESX host esx01", "[ok] esx esx01 (esx01): ssh key login works, host name esx01", "[ok] esx esx01: python3 found",
                     "[ok] esx esx01: vim-cmd found", "[ok] esx esx01: esxcli found", "-- case n1   vm esx01ev01, vm esx01ev02",
                     "[ok] monitoring: starts 1 min before the job start. Stop: not registered (the case has no monitoring stop time: the "
                     "monitoring goes on until the next run of this ESX host or sh %s/tcrun stop esx01)" % e.tcgen,
                     "TCGEN-VM esx01ev01 vmid=10 power=on vmx=", 'TCGEN-SET esx01ev01 sched.mem.pin: (none) -> "TRUE"  [change]',
                     'TCGEN-SET esx01ev02 numa.nodeAffinity: "0" -> "0"  [same]',
                     "TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 0  [change]",
                     "TCGEN-POWER esx01ev01 now=on want=on  [same]",
                     "[ok] settings and VM power: 3 value(s) differ (compared with the hosts as they are now). The run changes them 10 min "
                     "before the job start of the case (when a value differs, the VMs of the case are shut down first).",
                     "[ok] vm esx01ev01 (esx01ev01): ssh login works, clock", "tcgen lines in crontab: 0",
                     "== ESX host esx02", "[PROBLEM] esx esx02 (esx02): ssh key login failed (exit 255: root@esx02: Permission denied",
                     "sh %s/tcrun key" % e.tcgen, "[PROBLEM] settings and VM power of case n2 not checked (no ESX login)",
                     "== ESX host noesx", "(cases without an ESX host: no ESX login. The power of their VMs is not handled",
                     "[PROBLEM] vm lost01 (lost01): ssh login failed (exit 255: ssh: connect to host lost01 port 22: Connection refused)",
                     "CHECK RESULT: 3 problem(s). Fix them before block S.",
                     "saved: %s/tcrun-%s-check.log" % (e.log, RID)):
            self.assertIn(word, out)
        self.assertRegex(out, r"\[ok\] start time: job at 2026-11-20 10:30 \(2\d min from now\)")
        self.assertNotIn("its settings or VM power differ from the host, and the time to change them", out,
                         "첫 케이스의 설정이 달라도 변경 시각(10:20)이 지나지 않았으면 문제가 아님")
        self.assertEqual((e.vmx("esx01", "esx01ev01"), e.vm_crontab("esx01ev01"), e.esx_crontab("esx01")), before)
        self.assertEqual(self.change_calls(), [])
        with open(os.path.join(e.log, "tcrun-%s-check.log" % RID)) as f:
            self.assertIn("CHECK RESULT: 3 problem(s)", f.read())
        self.assertEqual(e.state(RID, "esx01"), {}, "점검은 진행을 시작하지 않음")
        self.assertEqual(self.minutes(), {}, "점검은 시작 시각을 기록하지 않음")
        self.assertFalse(os.path.exists(os.path.join(e.log, "tcrun-%s-jobs.csv" % RID)), "점검은 등록 기록을 만들지 않음")
        # 문제가 없으면 OK
        open(os.path.join(e.esx_dir("esx02"), "authorized"), "w").close()
        os.remove(os.path.join(e.vm_dir("lost01"), "down"))
        rc, out = e.tcrun("check", plan)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertNotIn("[PROBLEM]", out)

    def test_check_first_case_whose_change_time_has_passed(self):
        """ESX 호스트의 첫 케이스: 설정이나 VM 전원이 호스트와 다른데 변경 시각(잡 시작 10분 전)이 이미 지났으면 점검(P)이 문제로 알린다.
        변경 시각이 지나지 않았거나 호스트와 같으면 문제가 아니다. 점검은 아무것도 바꾸지 않는다."""
        e = self.e
        self.two_vms(vm2={"state": "off"})
        late = "its settings or VM power differ from the host, and the time to change them"
        # 설정이 다르고 변경 시각(09:58)이 지남. 둘째 케이스는 첫 케이스가 아니므로 알리지 않는다
        res, plan = self.gen([case("f1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])], seq_start="10:08", max_wait="0:20"),
                              case("f2", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])], seq_start="10:40", max_wait="0:20")])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: 2 value(s) differ (compared with the hosts as they are now).", out)
        self.assertIn("[PROBLEM] case f1: %s (2026-11-20 09:58:00, 10 min before the job start) has passed. Set a later start time in tcgen "
                      "and generate again. (When block S is pasted as it is, they are changed at once and the job starts at its time.)" % late, out)
        self.assertNotIn("[PROBLEM] case f2", out)
        self.assertIn("[ok] monitoring: starts 1 min before the job start. Stop: 2026-11-20 10:28", out)
        self.assertIn("CHECK RESULT: 1 problem(s). Fix them before block S.", out)
        # VM 전원이 다르고(꺼져 있는 VM 을 켜야 함) 변경 시각(09:59)이 지남
        res, plan = self.gen([case("p1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:09")],
                             now=GEN + dt.timedelta(seconds=1))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: 1 VM(s) to power on (compared with the hosts as they are now).", out)
        self.assertIn("[PROBLEM] case p1: %s (2026-11-20 09:59:00, 10 min before the job start) has passed." % late, out)
        self.assertIn("[ok] vm esx01ev02: powered off now. The run powers it on and then waits for its ssh login.", out)
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        # 같은 케이스라도 변경 시각(10:20)이 지나지 않았으면 문제가 아니다
        res, plan = self.gen([case("p2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:30")],
                             now=GEN + dt.timedelta(seconds=2))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: 1 VM(s) to power on (compared with the hosts as they are now).", out)
        self.assertNotIn(late, out)
        self.assertIn("CHECK RESULT: OK", out)
        # 호스트와 같으면 잡 시작이 가까워도 문제가 아니다
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a", [], ["same"])], seq_start="10:12")], now=GEN + dt.timedelta(seconds=3))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: already as wanted. The VMs will not be shut down or powered on.", out)
        self.assertNotIn(late, out)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertEqual(self.change_calls(), [], "점검은 아무것도 바꾸지 않음")
        self.assertEqual(e.esx_vms("esx01")["esx01ev02"]["state"], "off")
        self.assertEqual(self.tc_lines("esx01ev01"), [])

    def test_check_reports_a_start_time_that_has_passed(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("k1", [vm("esx01ev01", "job_a")], seq_start="09:55"),
                              case("k2", [vm("esx01ev01", "job_a")], seq_start="23:00")], now=dt.datetime(2026, 11, 20, 9, 50, 0))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] case k1: the start time 2026-11-20 09:55 has already passed. Change it in tcgen and generate again.", out)
        self.assertRegex(out, r"\[ok\] start time: job at 2026-11-20 23:00 \(\d+ min from now\)")
        self.assertRegex(out, r"CHECK RESULT: 1 problem")

    def test_check_shows_the_ssh_error_line(self):
        """ssh 가 실패하면 출력의 첫 줄이 아니라 ssh 의 오류 줄을 보인다."""
        e = self.e
        self.two_vms()
        open(os.path.join(e.vm_dir("esx01ev02"), "down"), "w").close()
        res, plan = self.gen([case("b1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:30")])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] vm esx01ev02 (esx01ev02): ssh login failed (exit 255: ssh: connect to host esx01ev02 port 22: "
                      "Connection refused)", out)

    def test_folders_that_cannot_be_written(self):
        """작업 폴더나 로그 폴더에 1 MB 를 쓸 수 없으면(디스크, quota) start 는 시작하지 않고 check 는 문제로 알린다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("w1", [vm("esx01ev01", "job_a")], seq_start="10:30")])
        rd = os.path.dirname(plan)
        os.chmod(rd, 0o500)
        try:
            rc, out = e.tcrun("start", plan)
            self.assertEqual(rc, 2, out)
            self.assertIn("==== 1 MB cannot be written in %s (disk full or disk quota of the account" % rd, out)
            self.assertIn("Nothing started.", out)
        finally:
            os.chmod(rd, 0o700)
        self.assertEqual(e.state(RID, "esx01"), {})
        os.chmod(e.log, 0o500)
        try:
            rc, out = e.tcrun("check", plan)
            self.assertIn("[PROBLEM] folder %s: 1 MB cannot be written" % e.log, out)
            self.assertIn("[ok] folder %s: 1 MB can be written" % e.tcgen, out)
        finally:
            os.chmod(e.log, 0o700)

    def test_key_and_usage(self):
        e = self.e
        rc, out = e.tcrun("key")
        with open(os.path.join(e.tcgen, "id_esx.pub")) as f:
            pub = f.read().strip()
        self.assertIn("echo '%s' >> /etc/ssh/keys-root/authorized_keys" % pub, out)
        self.assertIn("sh %s/esxssh <esx host> hostname" % e.tcgen, out)
        self.assertEqual(e.tcrun("version"), (0, core.VERSION + "\n"))
        rc, out = e.tcrun()
        self.assertEqual(rc, 2)
        self.assertIn("usage: sh %s/tcrun check <plan> | start <plan> | run <plan> <esx> | status [-a] | stop [esx] | key | version"
                      % e.tcgen, out)
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM")
        self.assertEqual(e.tcrun("stop")[1].strip(), "nothing to stop: tcrun status shows no run")
        self.assertEqual(e.tcrun("stop", "-x"), (2, "tcrun stop: bad ESX host name: -x\n"))
        self.assertEqual(e.tcrun("cancel"), (2, "tcrun %s has no cancel: sh %s/tcrun stop [esx] ends the runs, removes their crontab lines "
                                                "and stops their monitoring\n" % (core.VERSION, e.tcgen)))
        rc, out = e.tcrun("run", os.path.join(e.root, "nofile"), "esx01")
        self.assertEqual(rc, 2)

    def test_esxssh_passes_stdin_and_uses_the_key(self):
        e = self.e
        self.two_vms()
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh"), "esx01", "sh", "-s"], input=b"hostname; echo $TCGEN_TMP\n",
                           env=e.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.stdout.decode().split(), ["esx01", os.path.join(e.esx_dir("esx01"), "tmp")])
        os.remove(os.path.join(e.tcgen, "id_esx"))
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh"), "esx01", "hostname"], env=e.env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.returncode, 255, p.stdout)
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh")], env=e.env, stdout=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 2)


class SlowRegistrationTests(TcrunBase):
    """줄 등록이 첫 시작 시각 가까이에서 일어나는 경우(느린 접속, 멈춘 crontab 명령, 시계가 다른 VM, 등록 중 stop). 초 단위로 시점을
    맞춰야 하므로 가상 시계를 3배속으로 낮춘다(실제 1초 = 가상 3초). 진행을 시작한 시점이 전제와 다르면 그 시험은 건너뛴다(skip)."""
    SPEED = 3

    def started(self, cron):
        return [(h, line.split(" # ")[1]) for h, t, line in cron.fired]

    def test_slow_login_never_leaves_a_half_started_case(self):
        """한 VM 의 접속이 오래 걸려 첫 시작 시각 15초 전까지 줄을 넣지 못하는 경우. tcrun 은 그 접속에 남은 시간만 주고 끊은 뒤, 줄을
        받은 다른 VM 들에서 줄을 지운다. 등록하지 않은 것으로 기록한 케이스의 잡은 어느 VM 에서도 시작하지 않는다(일부 VM 만 잡이
        시작되는 일이 없다). 시작 시각은 옮기지 않는다."""
        e = self.e
        for n in (1, 2, 3):
            e.add_vm("esx01ev0%d" % n)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {}), ("esx01ev03", {})])
        self.slow_login("esx01ev02", 32)            # 10:01:1x 에 줄을 받는다(첫 시작 시각 10:02 의 15초 전보다 앞)
        self.slow_login("esx01ev03", 150)           # 10:01:45 까지 답이 없다
        res, plan = self.gen([case("slow1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a"), vm("esx01ev03", "job_a")], seq_start="10:02")])
        cron = e.start_cron()
        self.wait_clock("2026-11-20 10:00:26")
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        begin = self.log_time("settings and VM power: already as wanted")       # 이 줄 바로 뒤에 줄 등록을 시작한다
        if not epoch("2026-11-20 10:00:26") <= begin <= epoch("2026-11-20 10:00:50"):
            self.skipTest("이 시험의 전제가 맞지 않음: 줄 등록을 10:00:26 ~ 10:00:50 에 시작해야 함\n" + log)
        m = re.search(r"WARN not registered: case slow1, job start 2026-11-20 10:02: (vm esx01ev03: no answer within the (\d+) s that were "
                      r"left to register the lines 15 s before the first start time 2026-11-20 10:02:00)", log)
        self.assertTrue(m, log)
        why = m.group(1)
        self.assertLessEqual(int(m.group(2)), 45, "esx01ev03 의 접속에는 10:01:45 까지 남은 시간만 줌")
        self.assert_order(log, ["removed the lines of this case: vm esx01ev01", "removed the lines of this case: vm esx01ev02",
                                "removed the lines of this case: vm esx01ev03", "WARN not registered: case slow1"])
        self.assertLess(self.log_time("removed the lines of this case: vm esx01ev02"), epoch("2026-11-20 10:02:00"),
                        "줄을 받은 VM 들의 줄은 첫 시작 시각 전에 지움")
        self.assertEqual(self.jobs(), [("slow1", "esx01ev0%d" % n, "2026-11-20 10:02", "not registered (%s)" % why) for n in (1, 2, 3)])
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02") + self.tc_lines("esx01ev03"), [], "줄은 남지 않음")
        self.assertNotIn("may have started", log)
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        self.assertIn("1 NOT registered", e.state(RID, "esx01")["text"])
        self.assertEqual(self.minutes(), {}, "등록하지 못한 케이스의 시작 시각 기록은 지움")
        self.wait_clock("2026-11-20 10:02:10")
        self.assertEqual(self.started(cron), [], "등록하지 않은 것으로 기록한 케이스의 잡이 일부 VM 에서 시작됨\n%s" % log)

    def test_vm_whose_crontab_command_hangs_is_marked_in_the_record(self):
        """한 VM 의 crontab 명령이 멈춰서(접속은 됐고 줄을 넣는 중이었다) 남은 시간 안에 답이 없는 경우. tcrun 은 접속을 끊고 다른 VM 의
        줄을 첫 시작 시각 전에 지운다. 답이 없던 VM 은 접속을 끊은 뒤에도 줄을 받을 수 있으므로, 등록 기록의 그 VM 행에 그 사실을
        적고 케이스를 줄 목록에 남긴다(stop 과 다음 실행이 그 VM 의 줄을 다시 지운다). 여기서는 줄이 첫 시작 시각 뒤에 들어온다."""
        e = self.e
        for n in (1, 2, 3):
            e.add_vm("esx01ev0%d" % n)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {}), ("esx01ev03", {})])
        self.slow_crontab("esx01ev02", 32)          # 10:01:1x 에 줄을 받는다(첫 시작 시각 10:02 의 15초 전보다 앞)
        self.slow_crontab("esx01ev03", 110)         # crontab 명령이 110초 걸린다: 줄은 10:02:5x 뒤에 들어온다
        res, plan = self.gen([case("slow2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a"), vm("esx01ev03", "job_a")], seq_start="10:02")])
        cron = e.start_cron()
        self.wait_clock("2026-11-20 10:00:26")
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        begin = self.log_time("settings and VM power: already as wanted")
        if not epoch("2026-11-20 10:00:26") <= begin <= epoch("2026-11-20 10:00:50"):
            self.skipTest("이 시험의 전제가 맞지 않음: 줄 등록을 10:00:26 ~ 10:00:50 에 시작해야 함\n" + log)
        m = re.search(r"WARN not registered: case slow2, job start 2026-11-20 10:02: (vm esx01ev03: no answer within the \d+ s that were "
                      r"left to register the lines 15 s before the first start time 2026-11-20 10:02:00)", log)
        self.assertTrue(m, log)
        why = m.group(1)
        self.assertIn("WARN case slow2 is not registered, but esx01ev03 gave no answer while it was taking the lines: they may still arrive "
                      "there. Look at its crontab, or run sh %s/tcrun stop esx01" % e.tcgen, log)
        jobs = self.jobs()
        self.assertEqual(jobs[:2], [("slow2", "esx01ev0%d" % n, "2026-11-20 10:02", "not registered (%s)" % why) for n in (1, 2)])
        self.assertRegex(jobs[2][3], r"^not registered \(%s\)\. This VM gave no answer while it was taking the lines, and it had none when "
                                     r"tcrun removed them at 2026-11-20 10:01:\d\d \(clock of the VM\): they may still arrive\. Look at its "
                                     r"crontab, or run sh %s/tcrun stop esx01$" % (re.escape(why), re.escape(e.tcgen)))
        self.assertEqual(self.started(cron), [], "줄을 받은 VM 들의 잡은 시작하지 않음")
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        e.wait_for(lambda: self.tc_lines("esx01ev03"), seconds=120, what="멈췄던 crontab 명령이 끝나 esx01ev03 에 줄이 들어옴")
        self.assertIn("crontab lines on the hosts: case slow2. To remove them: sh %s/tcrun stop esx01" % e.tcgen, e.tcrun("status")[1])
        rc, out = e.tcrun("stop")
        self.assertIn("crontab lines of case slow2 removed (job start 2026-11-20 10:02)", out)
        self.assertEqual(self.tc_lines("esx01ev03"), [], "stop 이 늦게 들어온 줄을 지움")
        self.assert_gone()
        self.assertEqual(self.started(cron), [])

    def test_lines_removed_after_the_first_start_time_are_marked_in_the_record(self):
        """줄을 지운 때가 그 VM 의 시계로 첫 시작 시각 뒤인 경우(여기서는 esx01ev01 의 시계가 50초 빠르다). 그 VM 에서는 그 시각의 명령
        (모니터링 시작)이 이미 실행됐을 수 있다. 등록 기록의 그 VM 행에 그 사실을 적고, 시작된 모니터링에는 중지 명령을 실행한다.
        잡은 어느 VM 에서도 시작하지 않는다."""
        e = self.e
        e.add_vm("esx01ev01", clock_offset=50)
        e.add_vm("esx01ev02")
        e.add_vm("esx01ev03")
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {}), ("esx01ev03", {})])
        self.slow_login("esx01ev02", 38)            # 10:01:1x 에 줄을 받는다. 그때 esx01ev01 의 시계는 10:02 를 지났다
        open(os.path.join(e.vm_dir("esx01ev03"), "crontab_broken"), "w").close()
        res, plan = self.gen([case("h1", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_a"), vm("esx01ev03", "job_a")],
                                   seq_start="10:03")])
        cron = e.start_cron()
        self.wait_clock("2026-11-20 10:00:26")
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        begin = self.log_time("settings and VM power: already as wanted")
        if not epoch("2026-11-20 10:00:26") <= begin <= epoch("2026-11-20 10:00:46"):
            self.skipTest("이 시험의 전제가 맞지 않음: 줄 등록을 10:00:26 ~ 10:00:46 에 시작해야 함\n" + log)
        why = "vm esx01ev03: the crontab lines were not registered (ssh exit 0: crontab: cannot be used)"
        self.assert_order(log, [
            "removed the lines of this case: vm esx01ev01", "removed the lines of this case: vm esx01ev02",
            "WARN case h1 is not registered, but its lines were still on esx01ev01 at the first start time 2026-11-20 10:02:00: what was to "
            "start then may have started there (see %s/tcrun-%s-jobs.csv)" % (e.log, RID),
            "monitoring stop commands run on vm esx01ev01 (exit 0)", "WARN not registered: case h1, job start 2026-11-20 10:03: " + why])
        jobs = self.jobs()
        self.assertRegex(jobs[0][3], r"^not registered \(%s\)\. The lines were removed on this VM at 2026-11-20 10:02:\d\d \(clock of the "
                                     r"VM\), after the first start time: what was to start then may have started on this VM$" % re.escape(why))
        self.assertEqual(jobs[1:], [("h1", "esx01ev0%d" % n, "2026-11-20 10:03", "not registered (%s)" % why) for n in (2, 3)])
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "시작된 모니터링은 중지 명령으로 끝냄")
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02") + self.tc_lines("esx01ev03"), [])
        self.wait_clock("2026-11-20 10:03:20")
        self.assertEqual([(h, line) for h, t, line in cron.fired if "job.sh" in line], [], "잡은 어느 VM 에서도 시작하지 않음")
        self.assertEqual(len(self.fired(cron, "esx01ev01", "mon.sh start g")), 1, "이 시험의 전제: esx01ev01 의 모니터링 시작은 실행됨")
        self.assertEqual(e.state(RID, "esx01")["state"], "done")

    def test_stop_while_the_lines_are_being_registered(self):
        """줄을 등록하는 중의 stop: 진행은 하던 등록을 마치고 다음 확인 때 끝난다(60초 안). 그 뒤 stop 이 등록된 줄을 모두 지운다.
        일부 VM 에만 줄이 남지 않는다."""
        e = self.e
        self.two_vms()
        self.slow_login("esx01ev02", 30)
        c1 = case("c1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:20")
        c2 = case("c2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:40")
        res, plan = self.gen([c1, c2])
        self.assertIn("SEG 002 0\n", res["plan"])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: self.tc_lines("esx01ev01"), seconds=120, what="esx01ev01 에 c1 의 줄")
        self.assertEqual(e.state(RID, "esx01")["step"], "register")
        rc, out = e.tcrun("stop")
        self.assertEqual(p.wait(timeout=60), 1, self.ctx(out))
        self.assertIn("== run %s esx esx01: stopping (state running, case c1, step register)" % RID, out)
        self.assertIn("the runner (pid %d) ended: before the config snapshot of case c1" % p.pid, out)
        self.assertNotIn("did not end at a check", out, "등록이 60초 안에 끝나 진행이 스스로 끝남")
        self.assertIn("crontab lines of case c1 removed (job start 2026-11-20 10:20)", out)
        self.assertIn("crontab lines of case c2 removed (job start 2026-11-20 10:40)", out)
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [], "등록을 마친 두 케이스의 줄을 모두 지움")
        gone = "removed before the job start (sh tcrun stop)"
        self.assertEqual(self.jobs(), [("c1", "esx01ev01", "2026-11-20 10:20", "registered"), ("c1", "esx01ev02", "2026-11-20 10:20", "registered"),
                                       ("c2", "esx01ev01", "2026-11-20 10:40", "registered"), ("c2", "esx01ev02", "2026-11-20 10:40", "registered"),
                                       ("c1", "esx01ev01", "2026-11-20 10:20", gone), ("c1", "esx01ev02", "2026-11-20 10:20", gone),
                                       ("c2", "esx01ev01", "2026-11-20 10:40", gone), ("c2", "esx01ev02", "2026-11-20 10:40", gone)])
        self.assertEqual(self.minutes(), {})
        self.assert_gone()
        self.assertEqual(cron.fired, [])

    def test_stop_ends_a_runner_that_does_not_end_at_a_check(self):
        """stop 이 기다리는 동안(기본 60초) 진행이 끝나지 않으면 진행을 끝낸다(TERM). 여러 VM 이나 여러 케이스의 등록은 60초를 넘을
        수 있다. 여기서는 기다리는 시간을 5초로 줄여(TCRUN_STOP_GRACE, 시험 전용) 등록 중에 끝낸다. 진행은 하던 접속을 끊는다. 그
        케이스는 등록을 시작할 때 줄 목록에 적어 두므로, stop 이 이미 줄을 받은 VM 의 줄을 지운다. 등록 기록에는 등록 중이던
        케이스와 아직 등록하지 않은 케이스가 모두 남는다. 끊은 접속이 뒤늦게 줄을 넣지 않는다."""
        e = self.e
        self.two_vms()
        self.slow_login("esx01ev02", 90)
        c1 = case("c1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:20")
        c2 = case("c2", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:40")
        res, plan = self.gen([c1, c2])
        self.assertIn("SEG 002 0\n", res["plan"])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: self.tc_lines("esx01ev01"), seconds=120, what="esx01ev01 에 c1 의 줄")
        self.assertEqual(e.state(RID, "esx01")["step"], "register")
        e.env["TCRUN_STOP_GRACE"] = "5"
        rc, out = e.tcrun("stop")
        del e.env["TCRUN_STOP_GRACE"]
        p.wait(timeout=60)
        self.assertIn("== run %s esx esx01: stopping (state running, case c1, step register)" % RID, out)
        self.assertIn("the runner (pid %d) did not end at a check and was ended at step register" % p.pid, out)
        self.assertIn("crontab lines of case c1 removed (job start 2026-11-20 10:20)", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [], "등록 중이던 케이스의 줄을 지움")
        self.assertEqual(self.tc_lines("esx01ev02"), [])
        mid = "not registered (the run ended as stopped: sh tcrun stop, while the lines of this case were being registered)"
        rest = "not registered (the run ended as stopped: sh tcrun stop)"
        gone = "removed before the job start (sh tcrun stop)"
        self.assertEqual(self.jobs(), [("c1", "esx01ev01", "2026-11-20 10:20", mid), ("c1", "esx01ev02", "2026-11-20 10:20", mid),
                                       ("c2", "esx01ev01", "2026-11-20 10:40", rest), ("c2", "esx01ev02", "2026-11-20 10:40", rest),
                                       ("c1", "esx01ev01", "2026-11-20 10:20", gone), ("c1", "esx01ev02", "2026-11-20 10:20", gone)])
        self.assertIn("ended by a signal", self.log())
        self.assertEqual(self.minutes(), {})
        self.assert_gone()
        time.sleep(110 / e.speed)                   # 끊은 접속(90초 걸리던 것)이 끝날 시각을 넘김
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [], "끊은 접속이 뒤늦게 줄을 넣지 않음")
        self.assertEqual(cron.fired, [])
    def test_snapshot_before_the_job_of_every_case(self):
        """잡이 있는 케이스마다 잡 시작 3분 전에 한 개씩 남긴다: 바꾼 것이 없는 케이스, 설정을 바꾼 케이스, 그 뒤의 같은 설정 케이스.
        잡이 없는 케이스(설정만)는 남기지 않는다. 표는 config_snapshot 의 VM 표와 같은 배치(구분, 항목, VM 이름 열)이고 VM 설정은 ESX
        호스트의 vmx 에서, Guest OS 설정은 VM 에서 읽는다."""
        e = self.e
        self.two_vms()
        e.guest_set("esx01ev02", "sys/kernel/mm/transparent_hugepage/enabled", "[always] madvise never\n")
        e.guest_set("esx01ev02", "sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages", None)
        c1 = case("c1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:06")
        c2 = case("c2", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], seq_start="10:18")
        c3 = case("c3", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], seq_start="10:23")
        c4 = case("c4", [vm("esx01ev01", profiles=["pin"]), vm("esx01ev02")], seq_start="10:25")
        res, plan = self.gen([c1, c2, c3, c4])
        self.assertIn("CFGL h1 구분\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(self.snap_files(), ["tcrun-%s-esx01-config-00%d-c%d.csv" % (RID, n, n) for n in (1, 2, 3)],
                         "잡이 있는 케이스마다 한 개. 잡이 없는 c4 는 남기지 않음")
        self.assert_order(log, [
            "==== case c1 (1 of 4) begin", "registered: case c1, job start 2026-11-20 10:06",
            "config snapshot (before the job of this case): ", "-config-001-c1.csv  (VM settings of 2 VM(s), guest OS settings of 2 VM(s))",
            "==== case c1 done", "==== case c2 (2 of 4) begin", "settings: applied. The VMs of this case are powered off now.",
            "VM power: done. Powered on by this case: esx01ev01 esx01ev02", "registered: case c2, job start 2026-11-20 10:18",
            "registered: case c3, job start 2026-11-20 10:23",
            "config snapshot (before the job of this case. Settings changed by this case. Powered on by this case: esx01ev01 esx01ev02): ",
            "-config-002-c2.csv  (VM settings of 2 VM(s), guest OS settings of 2 VM(s))", "==== case c2 done",
            "==== case c3 (3 of 4) begin", "config snapshot (before the job of this case): ", "-config-003-c3.csv", "==== case c3 done",
            "==== case c4 (4 of 4) begin", "==== case c4 done (settings only, no job)"])
        self.assertEqual(log.count("config snapshot ("), 3)
        self.assertNotIn("WARN", log)
        raw, rows, t = self.snap("001", "c1")
        self.assertTrue(raw.startswith("\ufeff") and "\r\n" in raw, "엑셀에서 열리는 UTF-8(BOM), CR+LF")
        self.assertEqual(rows[0], ["구분", "항목", "esx01ev01", "esx01ev02"])
        self.assertEqual(rows[1][:2], ["수집 시각", ""])
        self.assertEqual([r[0] for r in rows if r[0]], ["구분", "수집 시각", "VM 기본 정보", "vm advanced config", "Guest OS", "CPU affinity"])
        self.assertEqual(t[("VM 기본 정보", "기준 시각 (ESX 호스트 조회)")], [rows[1][2]] * 2)
        self.assertEqual((t[("VM 기본 정보", "power state")], t[("VM 기본 정보", "cpu")], t[("VM 기본 정보", "memory (GB)")],
                          t[("VM 기본 정보", "cores per socket / sockets")], t[("VM 기본 정보", "latency sensitivity")]),
                         (["on", "on"], ["8", "8"], ["16", "16"], ["1 / 8", "1 / 8"], ["(없음)", "(없음)"]))
        self.assertEqual((t[("vm advanced config", "sched.mem.pin")], t[("vm advanced config", "sched.mem.lpage.enable1GPage")]),
                         (["(없음)", "(없음)"], ["(없음)", "(없음)"]))
        self.assertEqual((t[("CPU affinity", "sched.cpu.affinity")], t[("CPU affinity", "numa.nodeAffinity")]), (["all", "all"], ["0", "0"]))
        self.assertEqual((t[("Guest OS", "THP enabled")], t[("Guest OS", "THP defrag")], t[("Guest OS", "kernel.numa_balancing")],
                          t[("Guest OS", "khugepaged scan_sleep_millisecs")], t[("Guest OS", "khugepaged pages_to_scan")],
                          t[("Guest OS", "hugepages 2MB nr_hugepages")], t[("Guest OS", "hugepages 1GB nr_hugepages")],
                          t[("Guest OS", "default_hugepagesz (cmdline)")], t[("Guest OS", "kernel")]),
                         (["madvise", "always"], ["madvise", "madvise"], ["0", "0"], ["10000", "10000"], ["4096", "4096"], ["0", "0"],
                          ["16", "(없음)"], ["1G", "1G"], [os.uname().release] * 2))
        self.assertNotIn(("Guest OS", "Collected At"), t, "게스트의 수집 시각은 기준 시각 행에 적는다")
        # 스냅샷을 읽은 시각: 케이스마다 잡 시작 3분 전. 게스트 값도 잡 시작 전에 읽는다
        for seq, name, job in (("001", "c1", "2026-11-20 10:06:00"), ("002", "c2", "2026-11-20 10:18:00"), ("003", "c3", "2026-11-20 10:23:00")):
            raw_n, rows_n, t_n = self.snap(seq, name)
            taken = epoch(rows_n[1][2])
            self.assertTrue(epoch(job) - 180 <= taken < epoch(job) - 120, "%s 의 스냅샷 시각 %s (잡 시작 %s)" % (name, rows_n[1][2], job))
            for v in t_n[("Guest OS", "기준 시각 (게스트 조회)")]:
                self.assertRegex(v, r"^2026-11-20 \d\d:\d\d:\d\d \S+$")
                self.assertTrue(epoch(job) - 180 <= epoch(v[:19]) < epoch(job) - 60, "%s 의 게스트 조회 시각 %s" % (name, v))
        # 설정을 바꾼 케이스: 바꾼 뒤의 값(프로파일 pin: sched.mem.pin = TRUE, numa.nodeAffinity 삭제)
        raw2, rows2, t2 = self.snap("002", "c2")
        self.assertEqual(rows2[0], rows[0])
        self.assertEqual((t2[("vm advanced config", "sched.mem.pin")], t2[("CPU affinity", "numa.nodeAffinity")]),
                         (["TRUE", "(없음)"], ["(없음)", "0"]))
        self.assertEqual(t2[("Guest OS", "THP enabled")], ["madvise", "always"])
        self.assertEqual([r[:2] for r in rows2], [r[:2] for r in rows], "항목과 순서는 스냅샷마다 같음")
        raw3, rows3, t3 = self.snap("003", "c3")
        self.assertEqual(t3[("vm advanced config", "sched.mem.pin")], ["TRUE", "(없음)"], "바꾼 것이 없는 케이스도 그때의 값을 남김")
        self.assertTrue(all(ord(ch) < 128 for ch in log), "로그는 ASCII")
        self.assertEqual(len(cron.fired), 6)

    def test_command_at_the_snapshot(self):
        """스냅샷 때 VM 에서 실행할 명령: 케이스의 켜져 있는 VM 마다 Guest OS 설정을 읽은 뒤 한 번 실행한다. 결과는 진행 로그와 스냅샷
        csv 의 snapshot command 행에 남는다. 실패한 VM 이 있어도 케이스는 진행한다(WARN). 전원 끔인 VM 에서는 실행하지 않는다.
        점검(P)은 VM 에 그 파일이 있는지 보고 명령은 실행하지 않는다."""
        e = self.e
        for n in (1, 2, 3):
            e.add_vm("esx01ev0%d" % n)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {}), ("esx01ev03", {"state": "off"})])
        open(os.path.join(e.vms, "esx01ev02", "gpush_fail"), "w").close()
        c1 = case("q1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a"), vm("esx01ev03", power="off")], seq_start="10:14")
        # 구성 스냅샷을 잡 시작 6분 전에 읽는다: 명령은 잡 시작 2분 전까지만 시작하므로 VM 2대의 명령에 4분이 있다
        res, plan = self.gen([c1], seq_snap_lead_min=6, seq_guest_cmd=e.gpush_cmd())
        self.assertIn("GCMD %s\n" % e.gpush_cmd(), res["plan"])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] command at the config snapshot (run on every VM of a case that is powered on, before the job): %s"
                      % e.gpush_cmd(), out)
        for name in ("esx01ev01", "esx01ev02"):
            self.assertIn("[ok] vm %s: the file of the command at the config snapshot is there (%s/gpush.sh)" % (name, e.bin), out)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertEqual((e.gpush_runs("esx01ev01"), e.gpush_runs("esx01ev02")), (0, 0), "점검은 그 명령을 실행하지 않음")
        # 경로가 틀린 명령: 점검이 알린다
        res2, bad = self.gen([case("q3", [vm("esx01ev01", "job_a")], seq_start="10:40")], seq_guest_cmd="sh /nonexistent/dir/gpush.sh --insecure",
                             now=GEN + dt.timedelta(seconds=5))
        rc, out = e.tcrun("check", bad)
        self.assertIn("[PROBLEM] vm esx01ev01: the file of the command at the config snapshot is not there or cannot be read: "
                      "/nonexistent/dir/gpush.sh. Check the command in the settings area of tcgen", out)
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual([r["status"] for r in self.results()], ["registered"], "명령이 실패한 VM 이 있어도 케이스는 진행")
        self.assertEqual((e.gpush_runs("esx01ev01"), e.gpush_runs("esx01ev02"), e.gpush_runs("esx01ev03")), (1, 0, 0),
                         "켜져 있는 VM 마다 한 번(esx01ev02 는 실패, esx01ev03 은 전원 끔)")
        self.assert_order(log, [
            "registered: case q1, job start 2026-11-20 10:14", "config snapshot: command on vm esx01ev01: ok ([OPS] esx01ev01 Guest properties 10 recorded ",
            "-config-001-q1.csv  (VM settings of 3 VM(s), guest OS settings of 2 VM(s), command at the snapshot ok on 1 VM(s))",
            "WARN config snapshot of case q1 is not complete: command on vm esx01ev02 failed (exit 1: [ERROR] property write failed "
            "HTTP 500:  internal error); The case goes on.", "==== case q1 done"])
        self.assertEqual(log.count("WARN"), 1)
        self.assertTrue(all(ord(ch) < 128 for ch in log), "명령의 출력에 한글이 있어도 로그는 ASCII")
        raw, rows, t = self.snap("001", "q1")
        row = [r for r in rows if r[1] == "snapshot command"][0]
        self.assertRegex(row[2], r"^ok: \[OPS\] esx01ev01 Guest properties 10 recorded 2026-11-20 10:\d\d:\d\d KST$")
        self.assertEqual(row[3:], ["failed (exit 1): [ERROR] property write failed HTTP 500:  internal error", "(없음)"])
        self.assertEqual(rows.index(row), [r[1] for r in rows].index("kernel") + 1, "Guest OS 구간의 마지막 행")
        self.assertTrue(epoch("2026-11-20 10:08:00") <= epoch(row[2][-23:-4]) < epoch("2026-11-20 10:12:30"),
                        "명령은 스냅샷(잡 시작 6분 전) 때 실행하고 잡 시작 2분 전보다 늦게 시작하지 않음: %s" % row[2])
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:14:00")])
        self.assertNotIn("vm esx01ev03", self.ssh_calls(), "전원 끔인 VM 에는 접속하지 않음")

    def test_command_is_not_run_when_no_time_is_left(self):
        """스냅샷 때 실행하는 명령이 오래 걸려 잡 시작 2분 전을 넘기면, 남은 VM 에서는 명령을 실행하지 않는다(WARN). 잡은 이미 등록돼
        있으므로 적은 시각에 시작한다."""
        e = self.e
        self.two_vms()
        with open(os.path.join(e.vms, "esx01ev01", "gpush_slow"), "w") as f:
            f.write("130")
        c1 = case("d1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:09")
        res, plan = self.gen([c1], seq_snap_lead_min=4, seq_guest_cmd=e.gpush_cmd())
        self.assertIn("SET SNAP_LEAD_MIN 4\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual((e.gpush_runs("esx01ev01"), e.gpush_runs("esx01ev02")), (1, 0))
        self.assert_order(log, [
            "registered: case d1, job start 2026-11-20 10:09", "config snapshot: command on vm esx01ev01: ok (",
            "command at the snapshot ok on 1 VM(s))",
            "WARN config snapshot of case d1 is not complete: command on vm esx01ev02 not run (no time left before the job start); "
            "The case goes on.", "==== case d1 done. job start 2026-11-20 10:09:00"])
        raw, rows, t = self.snap("001", "d1")
        self.assertTrue(epoch("2026-11-20 10:05:00") <= epoch(rows[1][2]) < epoch("2026-11-20 10:06:00"), "스냅샷은 잡 시작 4분 전: %s" % rows[1][2])
        row = [x for x in rows if x[1] == "snapshot command"][0]
        self.assertTrue(row[2].startswith("ok: [OPS] esx01ev01") and row[3] == "not run (no time left before the job start)", row)
        self.assertEqual(t[("Guest OS", "THP enabled")], ["madvise", "madvise"], "명령을 실행하지 않은 VM 도 Guest OS 설정은 읽음")
        self.assertEqual(self.results()[0]["job_start"], "2026-11-20 10:09:00")
        for name in ("esx01ev01", "esx01ev02"):
            self.assertEqual(self.fired(cron, name, "job.sh"), [epoch("2026-11-20 10:09:00")], "명령이 오래 걸려도 잡은 적은 시각에 시작")

    def test_snapshot_that_cannot_be_read_does_not_stop_the_case(self):
        """스냅샷을 읽지 못해도 케이스는 진행한다. 로그에 WARN 을 남기고 읽은 부분만 적는다. ESX 호스트가 없는 케이스는 Guest OS 설정만 남긴다."""
        e = self.e
        e.add_vm("vm-a")
        e.add_vm("vm-b")
        c1 = case("n1", [vm("vm-a", "job_a"), vm("vm-b", "job_a")], esx="", seq_start="10:08")
        res, plan = self.gen([c1])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "-")
        self.wait_log("registration: 1 of 1", chain="noesx")
        with open(os.path.join(e.vm_dir("vm-b"), "ssh_fail"), "w") as f:
            f.write("1")                        # vm-b 의 다음 접속(스냅샷) 한 번만 실패한다
        self.assertEqual(p.wait(timeout=300), 0, self.log("noesx"))
        log = self.log("noesx")
        self.assert_order(log, ["registered: case n1, job start 2026-11-20 10:08, VMs: vm-a vm-b",
                                "config snapshot (before the job of this case): ", "(VM settings of 0 VM(s), guest OS settings of 1 VM(s))",
                                "WARN config snapshot of case n1 is not complete: guest OS settings of vm-b not read (ssh exit 255: "
                                "ssh: connect to host vm-b port 22: Connection timed out); The case goes on.",
                                "==== case n1 done. job start 2026-11-20 10:08:00"])
        raw, rows, t = self.snap("001", "n1", chain="noesx")
        self.assertEqual(rows[0], ["구분", "항목", "vm-a", "vm-b"])
        self.assertEqual([r[0] for r in rows if r[0]], ["구분", "수집 시각", "Guest OS"], "ESX 호스트가 없으면 VM 설정 구간이 없음")
        self.assertEqual(t[("Guest OS", "THP enabled")], ["madvise", "(없음)"])
        self.assertEqual(self.results("noesx")[0]["status"], "registered")
        for name in ("vm-a", "vm-b"):
            self.assertEqual(self.fired(cron, name, "job.sh"), [epoch("2026-11-20 10:08:00")], name)


class SlowClockTests(TcrunBase):
    """초 단위로 시점을 맞춰야 하는 경우. 가상 시계를 6배속으로 낮춰(실제 1초 = 가상 6초) 진행을 시작하는 때와 등록에 걸리는 시간을 맞춘다."""
    SPEED = 6

    def test_snapshot_waits_60_seconds_after_a_power_on(self):
        """이 실행이 방금 켠 VM 이 있으면 마지막 VM 의 준비 60초 뒤에 스냅샷을 읽는다(게스트가 부팅 때 적용하는 설정이 들어간 뒤에 읽기
        위해서다). 잡 시작 2분 전보다 늦어지지 않을 때만 기다리고, 잡 시작 시각은 그대로다."""
        e = self.e
        self.two_vms(vm2={"state": "off", "boot_sec": 75})
        c1 = case("w1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], seq_start="10:06")
        res, plan = self.gen([c1], seq_prep_lead_min=5)
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_log("==== case w1 done", seconds=300)
        log = self.log()
        self.assert_order(log, ["settings and VM power: 1 VM(s) to power on. They are changed at 2026-11-20 10:01:00 (5 min before the job "
                                "start 2026-11-20 10:06)",
                                "VM power: done. Powered on by this case: esx01ev02. The last one was ready at ",
                                "registered: case w1, job start 2026-11-20 10:06, VMs: esx01ev01 esx01ev02",
                                "config snapshot: taken at ", "60 s after the last VM was ready (the guest applies the settings of its boot)",
                                "config snapshot (before the job of this case. Powered on by this case: esx01ev02): ", "==== case w1 done"])
        self.assertNotIn("WARN", log)
        ready = epoch(re.search(r"The last one was ready at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", log).group(1))
        self.assertTrue(epoch("2026-11-20 10:02:00") < ready < epoch("2026-11-20 10:03:00"),
                        "이 시험의 전제: 준비가 스냅샷 시각(10:03) 60초 전보다 늦게, 스냅샷 시각 전에 끝남 (%s)" % ready)
        self.assertIn("config snapshot: taken at %s, 60 s after the last VM was ready"
                      % dt.datetime.fromtimestamp(ready + 60, fakeenv.KST).strftime("%Y-%m-%d %H:%M:%S"), log)
        raw, rows, t = self.snap("001", "w1")
        self.assertGreaterEqual(epoch(rows[1][2]) - ready, 60, "스냅샷은 마지막 VM 의 준비 60초 뒤: %s" % rows[1][2])
        for v in t[("Guest OS", "기준 시각 (게스트 조회)")]:
            self.assertGreaterEqual(epoch(v[:19]) - ready, 60, "게스트 값은 마지막 VM 의 준비 60초 뒤에 읽음: %s" % v)
            self.assertLess(epoch(v[:19]), epoch("2026-11-20 10:05:00"))
        for name in ("esx01ev01", "esx01ev02"):
            self.assertEqual(self.tc_lines(name), ["06 10 20 11 * sh %s/job.sh simA 100 # tcgen w1 %s" % (e.bin, name)],
                             "잡 시작 시각은 그대로")
        self.assertEqual(self.jobs(), [("w1", "esx01ev01", "2026-11-20 10:06", "registered"), ("w1", "esx01ev02", "2026-11-20 10:06", "registered")])

    def test_snapshot_is_not_taken_when_the_job_start_is_less_than_a_minute_away(self):
        """줄 등록이 끝났을 때 잡 시작까지 1분이 남지 않았으면 구성 스냅샷을 읽지 않는다(잡이 실행되는 동안 VM 에 접속하지 않기 위해서다).
        로그에 WARN 을 남기고 케이스는 등록된 것으로 끝난다."""
        e = self.e
        self.two_vms()
        self.slow_crontab("esx01ev01", 38)          # 등록이 38초 걸린다: 10:00:3x 에 시작해 10:01:1x 에 끝난다(잡 시작 10:02 의 1분 전을 넘김)
        res, plan = self.gen([case("n1", [vm("esx01ev01", "job_a")], seq_start="10:02")])
        cron = e.start_cron()
        self.wait_clock("2026-11-20 10:00:24")
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, ["registered: case n1, job start 2026-11-20 10:02, VMs: esx01ev01",
                                "WARN config snapshot of case n1 not taken: the job start 2026-11-20 10:02 is less than 1 min away or has "
                                "passed (no login to the VMs while a job runs)", "==== case n1 done. job start 2026-11-20 10:02:00"])
        self.assertGreaterEqual(self.log_time("registered: case n1"), epoch("2026-11-20 10:01:00"), "이 시험의 전제: 등록이 잡 시작 1분 전을 넘겨 끝남")
        self.assertEqual(self.snap_files(), [])
        self.assertEqual(self.ssh_calls().count("vm esx01ev01"), 1, "VM 접속은 줄 등록 한 번뿐")
        self.assertEqual(self.jobs(), [("n1", "esx01ev01", "2026-11-20 10:02", "registered")])
        self.assertEqual(self.results()[0]["status"], "registered")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:02:00")])


class OpsTests(TcrunBase):
    """VCF Operations 기록(사용자 결정 2026-11-18): 구성 스냅샷 때 점프 VM 이 VM 설정(vm advanced config)과 케이스의 호스트
    옵션을 Operations 객체의 속성으로 기록한다. 가짜 Operations(fake_ops.py)로 확인한다."""
    PW = 'Pw "1" \\ $x'

    def setUp(self):
        TcrunBase.setUp(self)
        self.ops = fake_ops.FakeOps(user="admin", password=self.PW, vms=["esx01ev01"], hosts=["esx01.lab.local"]).start()
        # curl 을 감싸 명령줄 인자를 기록한다(암호와 토큰이 명령줄에 실리지 않는지 본다). 호출은 실제 curl 이 한다
        fakeenv.write_exec(os.path.join(self.e.fakebin, "curl"),
                           '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_ROOT/curl_args.txt"\nexec /usr/bin/curl "$@"\n')

    def curl_args(self):
        p = os.path.join(self.e.root, "curl_args.txt")
        if not os.path.exists(p):
            return ""
        with open(p) as f:
            return f.read()

    def tearDown(self):
        self.ops.stop()
        TcrunBase.tearDown(self)

    def ops_settings(self, password=None):
        enc, key = core.ops_encrypt(self.PW if password is None else password)
        self.key = key
        return dict(ops_host=self.ops.base, ops_user="admin", ops_pass_enc=enc, ops_enc_key=key)

    def test_vm_settings_and_host_options_are_written_at_the_snapshot(self):
        """케이스마다 구성 스냅샷 때: VM 설정은 VM 객체의 Custom Configuration|VM|<키> 로, 케이스의 호스트 옵션은 호스트 객체의
        Custom Configuration|Host|<구역.옵션> 으로 기록한다. Operations 에 객체가 없는 VM 은 경고만 남기고 케이스는 진행한다."""
        e = self.e
        self.two_vms()
        c1 = case("o1", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], esx_profiles=["numa0"], seq_start="10:25")
        c2 = case("o2", [vm("esx01ev01", "job_a", profiles=["pin"]), vm("esx01ev02", "job_a")], esx_profiles=["numa0"], seq_start="+0:10")
        res, plan = self.gen([c1, c2], **self.ops_settings())
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:25:00"), epoch("2026-11-20 10:35:00")],
                         "잡은 적은 시각에 시작" + self.ctx(out))
        vmp = self.ops.props["vm-esx01ev01"]
        self.assertEqual(sorted(vmp), sorted("Custom Configuration|VM|" + k for k in (
            "sched.mem.lpage.enable1GPage", "sched.mem.pin", "sched.mem.prealloc", "sched.mem.prealloc.pinnedMainMem",
            "sched.swap.vmxSwapEnabled", "numa.vcpu.preferHT", "numa.vcpu.maxPerVirtualNode", "monitor_control.halt_in_monitor",
            "monitor.idleLoopSpinBeforeHalt", "monitor.idleLoopMinSpinUS", "monitor_control.disable_pause_loop_exiting", "vpmc.enable")),
            "config_snapshot 의 키와 설정 프로파일의 키(프로파일에 있는 sched.mem.pin 포함). CPU affinity 키는 보내지 않는다")
        self.assertEqual(vmp["Custom Configuration|VM|sched.mem.pin"], "TRUE")
        self.assertEqual(vmp["Custom Configuration|VM|vpmc.enable"], "(없음)", "vmx 에 없는 키는 스냅샷 csv 와 같은 글")
        self.assertEqual(self.ops.props["host-esx01.lab.local"], {"Custom Configuration|Host|Numa.LocalityWeightActionAffinity": "0"},
                         "호스트 객체는 이름이 <ESX 호스트>.<도메인> 이어도 찾는다")
        self.assertNotIn("vm-esx01ev02", self.ops.props)
        self.assertEqual(self.ops.logins, [("admin", "LOCAL"), ("admin", "LOCAL")], "케이스마다 한 번 로그인")
        self.assert_order(log, [
            "config snapshot: VCF Operations: vm esx01ev01: 12 VM setting(s) written (Custom Configuration|VM)",
            "config snapshot: VCF Operations: esx esx01: 1 host option(s) written (Custom Configuration|Host)",
            "-config-001-o1.csv  (VM settings of 2 VM(s), guest OS settings of 2 VM(s), VM settings written to VCF Operations for 1 VM(s))",
            "WARN config snapshot of case o1 is not complete: VCF Operations has no VirtualMachine object named esx01ev02; The case goes on.",
            "==== case o1 done", "-config-002-o2.csv"])
        rows = list(csv.reader(io.StringIO(e.log_text(RID, "esx01", "-config-001-o1.csv").lstrip("\ufeff"))))
        self.assertEqual([r[1:] for r in rows if r[1] == "operations write"],
                         [["operations write", "ok: 12 properties", "not written (no VirtualMachine object with this name)"]])
        args = self.curl_args()
        self.assertIn("/suite-api/api/resources/vm-esx01ev01/properties", args, "이 시험의 전제: curl 호출의 인자를 기록함")
        for secret in (self.PW, self.ops.token, self.key):
            self.assertNotIn(secret, log)
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, args, "암호, 토큰, 키는 curl 명령줄에 싣지 않는다(파일로 넘긴다)")
        self.assertNotIn("Authorization", args)
        work = os.path.join(e.tcgen, "runs", RID, "esx01.work")
        self.assertEqual([f for f in os.listdir(work) if f.startswith("ops.")], [],
                         "암호 키, 요청 본문, 토큰이 든 응답 파일과 curl 설정 파일을 남기지 않는다")
        self.assertEqual([r["status"] for r in self.results()], ["registered", "registered"])

    def test_host_options_are_written_with_the_values_that_the_host_has(self):
        """호스트 옵션은 plan 의 목표 값이 아니라 구성 스냅샷 때 ESX 호스트에서 읽은 값을 기록한다. 여기서는 설정을 적용하고 줄을 등록한
        뒤, 스냅샷 전에 호스트의 값이 달라진 경우다(누군가 호스트에서 직접 변경). 읽지 못한 옵션은 기록하지 않고 WARN 을 남긴다."""
        e = self.e
        self.two_vms()
        self.profiles.append({"name": "two", "target": "ESX", "desc": "",
                              "text": "Numa.LocalityWeightActionAffinity = 0\nCpu.HTWholeCoreThreshold = 0"})
        c1 = case("h1", [vm("esx01ev01", "job_a")], esx_profiles=["two"], seq_start="10:25")
        res, plan = self.gen([c1], **self.ops_settings())
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        self.wait_log("registered: case h1", seconds=600)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0", "이 시험의 전제: 설정 적용이 끝남")
        path = os.path.join(e.esx_dir("esx01"), "adv.json")
        with open(path) as f:
            adv = json.load(f)
        adv["/Numa/LocalityWeightActionAffinity"]["value"] = "55"       # 스냅샷 전에 호스트의 값이 달라짐
        del adv["/Cpu/HTWholeCoreThreshold"]                             # 이 옵션은 스냅샷 때 읽지 못함
        with open(path, "w") as f:
            json.dump(adv, f, indent=1)
        self.wait_state("done", seconds=900)
        p.wait(timeout=60)
        log = self.log()
        self.assertEqual(self.ops.props["host-esx01.lab.local"], {"Custom Configuration|Host|Numa.LocalityWeightActionAffinity": "55"},
                         "호스트에서 읽은 값을 기록하고, 읽지 못한 옵션은 기록하지 않는다")
        self.assertIn("config snapshot: VCF Operations: esx esx01: 1 host option(s) written (Custom Configuration|Host)", log)
        self.assertIn("1 of 2 host option(s) of the case not read on esx esx01 and not written to VCF Operations;", log)
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:25:00")], "잡은 적은 시각에 시작")

    def test_sched_mem_pin_is_written_only_when_a_profile_sets_it(self):
        """설정 프로파일이 다루지 않으면 sched.mem.pin 은 보내지 않는다(config_snapshot 이 보내던 11개 키와 같다)."""
        e = self.e
        self.two_vms()
        c1 = case("q1", [vm("esx01ev01", "job_a")], seq_start="10:10")
        res, plan = self.gen([c1], **self.ops_settings())
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 0, self.ctx(out))
        keys = sorted(k.split("|")[-1] for k in self.ops.props["vm-esx01ev01"])
        self.assertEqual(len(keys), 11, keys)
        self.assertNotIn("sched.mem.pin", keys)
        self.assertNotIn("host-esx01.lab.local", self.ops.props, "호스트 옵션이 없는 케이스는 호스트 객체에 기록하지 않는다")

    def test_login_failure_is_a_warning_and_the_case_goes_on(self):
        e = self.e
        self.two_vms()
        c1 = case("w1", [vm("esx01ev01", "job_a")], seq_start="10:10")
        res, plan = self.gen([c1], **self.ops_settings(password="wrong"))
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertRegex(log, r"WARN config snapshot of case w1 is not complete: VCF Operations: nothing written \(login to VCF Operations "
                              r"http://127\.0\.0\.1:\d+ failed \(account admin, curl exit 0: ")
        self.assertEqual(self.ops.props, {})
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:10:00")], "Operations 기록 실패는 잡에 영향이 없다")
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        rows = list(csv.reader(io.StringIO(e.log_text(RID, "esx01", "-config-001-w1.csv").lstrip("\ufeff"))))
        self.assertEqual([r[2] for r in rows if r[1] == "operations write"], ["failed (no login)"])
        self.assertNotIn("wrong", log)

    def test_nothing_is_sent_without_an_address(self):
        """Operations 주소를 적지 않으면 Operations 를 호출하지 않는다. ESX 호스트가 없는 케이스(BM 서버)도 호출하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("n1", [vm("esx01ev01", "job_a")], seq_start="10:10")])
        self.assertNotIn("\nOPS ", res["plan"])
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(self.ops.calls, [])
        self.assertNotIn("VCF Operations", self.log())
        e.add_vm("bm01")
        res2, plan2 = self.gen([case("b1", [vm("bm01", "job_a")], esx="", seq_start="10:40")], now=GEN + dt.timedelta(minutes=20),
                               **self.ops_settings())
        rc, out = e.tcrun("run", plan2, "-", timeout=600)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ops.calls, [], "ESX 호스트가 없는 케이스: VM 설정을 읽지 않으므로 기록하지 않는다")

    def test_check_reports_the_login_and_the_objects(self):
        """P 블록: Operations 로그인과, VM 과 호스트 객체가 있는지 본다. 아무것도 기록하지 않는다."""
        e = self.e
        self.two_vms()
        c1 = case("k1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], esx_profiles=["numa0"], seq_start="10:40")
        res, plan = self.gen([c1], **self.ops_settings())
        rc, out = e.tcrun("check", plan, timeout=300)
        self.assertRegex(out, r"\[ok\] VCF Operations http://127\.0\.0\.1:\d+: login works \(account admin\)")
        self.assertIn("[ok] vm esx01ev01: VCF Operations has a VirtualMachine object with this name", out)
        self.assertIn("[PROBLEM] vm esx01ev02: VCF Operations has no VirtualMachine object with this name. Its VM settings would not be written", out)
        self.assertIn("[ok] esx esx01: VCF Operations has a HostSystem object for this host", out)
        self.assertIn("CHECK RESULT: 1 problem(s)", out)
        self.assertEqual(self.ops.props, {}, "점검은 기록하지 않는다")
        self.assertNotIn(self.ops.token, out)
        work = os.path.join(e.tcgen, "runs", RID, "check.work")
        self.assertEqual([f for f in os.listdir(work) if f.startswith("ops.")], [])
        res, plan = self.gen([c1], now=GEN + dt.timedelta(seconds=1), **self.ops_settings(password="wrong"))
        rc, out = e.tcrun("check", plan, timeout=300)
        self.assertRegex(out, r"\[PROBLEM\] VCF Operations: login to VCF Operations http://127\.0\.0\.1:\d+ failed \(account admin")
        res, plan = self.gen([c1], now=GEN + dt.timedelta(seconds=2))
        rc, out = e.tcrun("check", plan, timeout=300)
        self.assertIn("[ok] VCF Operations: no address in the settings of tcgen. The VM settings are not written to VCF Operations", out)


class WorkFolderTests(TcrunBase):
    """점프 VM 작업 폴더를 홈 밖에 둔 경우(설정의 jump_dir)."""
    JDIR = True

    def test_work_folder_outside_the_home(self):
        """작업 폴더를 홈 밖에 두면 진행 스크립트, 키, 비밀번호, 실행 기록, 시작 시각 기록이 모두 그 폴더에 있고 홈에는 쓰지 않는다."""
        e = self.e
        self.assertTrue(e.tcgen.endswith("/data/tcgen"))
        self.two_vms()
        res, plan = self.gen([case("f1", [vm("esx01ev01", "job_a")], esx_profiles=["numa0"], seq_start="10:14")])
        self.assertTrue(plan.startswith(e.tcgen + "/runs/"))
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("started: run %s esx esx01" % RID, out)
        self.assertIn("status: sh %s/tcrun status" % e.tcgen, out)
        self.wait_state("done", seconds=600)
        self.assertFalse(os.path.exists(os.path.join(e.jump, ".tcgen")), "홈의 ~/.tcgen 을 쓰지 않음")
        self.assertTrue(os.path.isdir(os.path.join(e.tcgen, "minutes")))
        self.assertTrue(os.path.exists(os.path.join(e.tcgen, "runs", RID, "esx01.work", "times")))
        self.assertIn("sh %s/tcrun stop esx01" % e.tcgen, e.state(RID, "esx01")["text"])
        self.assertIn("== run %s  esx esx01 : done" % RID, e.tcrun("status")[1])
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:14:00")])
        self.assertEqual(self.jobs(), [("f1", "esx01ev01", "2026-11-20 10:14", "registered")])


@unittest.skipUnless(fakeenv.HAVE and fakeenv.shutil.which("dash"), "dash 필요")
class DashTests(TcrunBase):
    """모든 sh 호출을 dash 로 바꿔 실행한다: tcrun 자신, VM 에 보내는 글, ESX 에 보내는 글, cron 이 실행하는 줄."""
    DASH = True

    def test_flow_with_settings_and_monitoring_in_dash(self):
        """점검, 설정 적용과 VM 전원, 줄 등록(모니터링 시작, 잡, 모니터링 중지), 구성 스냅샷과 스냅샷 때의 명령, 끝난 뒤의 stop."""
        e = self.e
        self.two_vms()
        c1 = case("dashA", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"], seq_start="10:16", max_wait="0:02")
        res, plan = self.gen([c1], seq_snap_lead_min=6, seq_guest_cmd=e.gpush_cmd())
        cron = e.start_cron()
        self.assertEqual(os.path.realpath(fakeenv.shutil.which("sh", path=e.env["PATH"])), os.path.realpath(fakeenv.shutil.which("dash")))
        rc, out = e.tcrun("check", plan)
        self.assertIn("CHECK RESULT: OK", out)
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertNotIn("WARN", log)
        rows = self.results()
        self.assertEqual([(r["case"], r["apply"], r["status"], r["monitor_start"], r["job_start"], r["monitor_stop"]) for r in rows],
                         [("dashA", "changed", "registered", "2026-11-20 10:15:00", "2026-11-20 10:16:00", "2026-11-20 10:18:00")])
        self.assertEqual(self.jobs(), [("dashA", "esx01ev01", "2026-11-20 10:16", "registered"), ("dashA", "esx01ev02", "2026-11-20 10:16", "registered")])
        e.wait_for(lambda: self.fired(cron, "esx01", "mon.sh stop h"), seconds=120, what="모니터링 중지(10:18)")
        for host, word, when in (("esx01ev01", "mon.sh start g", "10:15"), ("esx01", "mon.sh start h", "10:15"), ("esx01ev01", "job.sh simA 100", "10:16"),
                                 ("esx01ev02", "job.sh sim.B+x 150", "10:16"), ("esx01ev01", "mon.sh stop g", "10:18"), ("esx01", "mon.sh stop h", "10:18")):
            self.assertEqual(self.fired(cron, host, word), [epoch("2026-11-20 %s:00" % when)], (host, word))
        self.assertEqual(len(cron.fired), 6)
        self.assertTrue(e.vmx("esx01", "esx01ev01").endswith('sched.mem.pin = "TRUE"\n'))
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        self.assertTrue(self.report()[0].startswith("\ufeff"))
        raw, rows, t = self.snap("001", "dashA")
        self.assertEqual((rows[0], t[("vm advanced config", "sched.mem.pin")], t[("Guest OS", "THP enabled")]),
                         (["구분", "항목", "esx01ev01", "esx01ev02"], ["TRUE", "(없음)"], ["madvise", "madvise"]))
        self.assertEqual((e.gpush_runs("esx01ev01"), e.gpush_runs("esx01ev02")), (1, 1))
        rc, out = e.tcrun("stop")
        self.assertIn("== run %s esx esx01: ended as done. Removing the crontab lines that it left on the hosts" % RID, out)
        self.assertIn("removed from tcrun status.", out)
        self.assertNotIn("monitoring stop:", out, "중지 시각(10:18)이 지난 모니터링에는 중지 명령을 실행하지 않음")
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())

    def test_pause_with_rollback_in_dash(self):
        e = self.e
        self.two_vms(vm2={"poweron": "fail"})
        res, plan = self.gen([case("dashE", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev02", "job_a")], seq_start="10:14")])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        self.assertEqual(e.state(RID, "esx01")["state"], "paused")
        self.assertIn("rollback: rolledback 1 VM(s), 0 host option(s)", self.log())
        jobs = self.jobs()
        self.assertEqual([j[:3] for j in jobs], [("dashE", "esx01ev01", "2026-11-20 10:14"), ("dashE", "esx01ev02", "2026-11-20 10:14")])
        self.assertTrue(all(j[3].startswith("not registered (the run ended as paused: power on of vm esx01ev02 failed") for j in jobs), jobs)

    def test_queue_and_stop_in_dash(self):
        """대기(queue), status, stop 의 흐름."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("dq1", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:30", max_wait="0:05")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("started: run %s esx esx01" % RID, out)
        self.wait_log("registration: 1 of 1")
        res2, plan2 = self.gen([case("dq2", [vm("esx01ev02", "job_a")], seq_start="10:50")], now=GEN + dt.timedelta(minutes=1))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        out = e.tcrun("status")[1]
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("== run %s  esx esx01 : queued" % rid2, out)
        rc, out = e.tcrun("stop")
        self.assertIn("== run %s esx esx01: stopping (state queued" % rid2, out)
        self.assertIn("== run %s esx esx01: stopping (state running, case dq1, step wait-snapshot)" % RID, out)
        self.assertIn("crontab lines of case dq1 removed (job start 2026-11-20 10:30)", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.jobs(), [("dq1", "esx01ev01", "2026-11-20 10:30", "registered"),
                                       ("dq1", "esx01ev01", "2026-11-20 10:30", "removed before the job start (sh tcrun stop)")])
        self.assertEqual(os.listdir(os.path.join(e.tcgen, "runs")), [])
        self.assertEqual(self.minutes(), {})
        self.assertEqual(cron.fired, [])
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM")

    def test_next_run_removes_the_lines_in_dash(self):
        """끝난 실행이 남긴 줄을 다음 실행이 첫 잡 시작 N분 전(여기서는 6분)에 지우고 모니터링을 끝내는 흐름."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("d1", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:06")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        res2, plan2 = self.gen([case("d2", [vm("esx01ev01", "job_a")], seq_start="10:18")], now=GEN + dt.timedelta(minutes=8),
                               seq_prep_lead_min=6)
        rid2 = res2["run_id"]
        e.tcrun_bg("run", plan2, "esx01")
        self.wait_log("the crontab lines of 1 case(s) of run %s are removed" % RID, seconds=300, rid=rid2)
        self.assertGreaterEqual(self.log_time("Removing them now", rid=rid2), epoch("2026-11-20 10:12:00"))
        self.assertLess(self.log_time("registered: case d2", rid=rid2), epoch("2026-11-20 10:12:00"))
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]])
        self.assertEqual([x.split(" # ")[1] for x in self.tc_lines("esx01ev01")], ["tcgen d2 esx01ev01"])
        self.wait_text("The crontab lines of the run were removed", rid=RID)       # 줄을 지운 뒤 앞 실행의 상태 글을 고쳐 쓴다
        self.assertRegex(e.state(RID, "esx01")["text"], r"The crontab lines of the run were removed \S+ \S+ by run %s$" % rid2)



class ConfirmTests(TcrunBase):
    """S 블록의 확인 목록과 확인(사용자 요청 2026-11-20 "생성기 점프 VM 에 복사 붙여넣기 하면 테스트케이스 이름, 호스트 혹은 VM 명,
    시작 시간, 실행 명령어 모니터링 목록 리스트업되고 확인한 다음 실행되도록하고싶어 확인작업필수", "출력물에서 VM 명은 그린색이나
    노란색으로 표기되도록"). tcrun start 는 목록을 보이고 yes 를 받은 뒤에만 시작한다."""

    PROMPT = "Register these cases in crontab? Type yes or no: "

    def answer(self, text):
        with open(self.e.answer, "w") as f:
            f.write(text)

    def two_cases(self):
        self.two_vms()
        c1 = case("caseA", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_b")], esx_monitors=["hmon"], seq_start="10:14")
        c2 = case("caseB", [vm("esx01ev01", "job_a")], seq_start="+0:16")
        return self.gen([c1, c2])

    def nothing_started(self, out):
        e = self.e
        self.assertEqual(e.state(RID, "esx01"), {}, out)
        self.assertFalse(os.path.exists(os.path.join(e.log, "tcrun-%s-jobs.csv" % RID)), "등록 기록도 만들지 않음")
        self.assertEqual(self.tc_lines("esx01ev01"), [], "crontab 줄 없음")
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())

    def test_start_lists_the_cases_and_stops_on_no(self):
        e = self.e
        res, plan = self.two_cases()
        self.answer("no\n")
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 4, out)
        lc = core._list_cmd
        for word in ("run %s: 2 cases. Check them before they are registered in crontab.\n" % RID,
                     "---- ESX host esx01: 2 cases\n",
                     "case 1/2  caseA   job start 2026-11-20 10:14\n",
                     "  monitoring start 2026-11-20 10:13, stop 2026-11-20 10:18\n",
                     "  vm esx01ev01\n    job: %s\n    monitoring gmon: %s\n" % (lc(self.run_cmds[0]["cmd"]), lc(self.mon_cmds[0]["start"])),
                     "  vm esx01ev02\n    job: %s\n" % lc(self.run_cmds[1]["cmd"]),
                     "  ESX host esx01\n    monitoring hmon: %s\n" % lc(self.mon_cmds[2]["start"]),
                     "case 2/2  caseB   job start 2026-11-20 10:30\n  vm esx01ev01\n    job: %s\n" % lc(self.run_cmds[0]["cmd"]),
                     self.PROMPT, "==== not confirmed. Nothing started."):
            self.assertIn(word, out)
        self.assertLess(out.index("case 2/2"), out.index(self.PROMPT), "목록을 다 보인 뒤에 묻는다")
        self.assertNotIn("\x1b[", out, "터미널이 아니면 색 글자를 쓰지 않음")
        self.nothing_started(out)

    def test_other_answers_ask_again_and_yes_starts(self):
        e = self.e
        res, plan = self.two_cases()
        self.answer("\n   \nmaybe\nYES\n")
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count(self.PROMPT), 4, "빈 줄과 다른 답이면 다시 묻는다")
        self.assertEqual(out.count("     type yes or no"), 1)
        self.assertIn("started: run %s esx esx01" % RID, out)
        # 같은 S 블록을 다시 붙여넣으면 묻지 않고 이미 시작했다고 알린다
        self.answer("")
        rc, out = e.tcrun("start", plan)
        self.assertNotIn(self.PROMPT, out)
        self.assertIn("==== esx01: run %s was already started on this jump VM. Not started again." % RID, out)

    def test_no_terminal_and_no_answer_start_nothing(self):
        e = self.e
        res, plan = self.two_cases()
        missing = os.path.join(e.root, "no_such_tty")
        e.env["TCRUN_TEST_TTY"] = missing
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 4, out)
        self.assertIn("==== no terminal to ask on (%s). Nothing started. Paste block S into a terminal of the jump VM." % missing, out)
        self.assertNotIn(self.PROMPT, out)
        del e.env["TCRUN_TEST_TTY"]                  # 제어 터미널이 없는 실행(시험의 프로세스): /dev/tty 를 열 수 없다
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 4, out)
        self.assertIn("==== no terminal to ask on (/dev/tty). Nothing started.", out)
        e.env["TCRUN_TEST_TTY"] = e.answer
        self.answer("")
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 4, out)
        self.assertIn(self.PROMPT, out)
        self.assertIn("==== no answer. Nothing started.", out)
        self.nothing_started(out)

    def test_vm_names_are_green_on_a_terminal(self):
        """VM 이름은 터미널에서 초록색(ESC [1;32m). 점검 로그 파일에는 색 글자가 들어가지 않는다. TCRUN_COLOR=always 가 터미널을 대신한다."""
        e = self.e
        res, plan = self.two_cases()
        g, n = "\x1b[1;32m", "\x1b[0m"
        e.env["TCRUN_COLOR"] = "always"
        self.answer("no\n")
        rc, out = e.tcrun("start", plan)
        for name in ("esx01ev01", "esx01ev02"):
            self.assertIn("  vm %s%s%s\n" % (g, name, n), out)
        self.assertIn("  ESX host esx01\n", out, "ESX 호스트 이름은 색을 넣지 않음")
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] vm %sesx01ev01%s (esx01ev01): ssh login works" % (g, n), out)
        self.assertIn("-- case caseA   vm %sesx01ev01%s, vm %sesx01ev02%s" % (g, n, g, n), out)
        with open(os.path.join(e.log, "tcrun-%s-check.log" % RID)) as f:
            text = f.read()
        self.assertNotIn("\x1b", text, "점검 로그 파일은 색 글자 없이")
        self.assertIn("-- case caseA   vm esx01ev01, vm esx01ev02", text)
        e.env["TCRUN_COLOR"] = "never"
        self.answer("no\n")
        rc, out = e.tcrun("start", plan)
        self.assertNotIn("\x1b", out)


class StopRuleTests(TcrunBase):
    """모니터링 자동 중지(사용자 결정 2026-11-09 "다음테스트 시작하기 10분전에 설정한 모든 모니터링 종료하도록 만들어줘"): 다음 케이스의
    잡 시작 N분 전. 사용자 기본값 10분은 설정 적용(30분 전)보다 늦으므로, 다음 케이스가 설정 변경 케이스이면 그 VM 종료와 설정 적용
    뒤에 앞 케이스의 중지 줄이 실행된다. 그 줄은 다음 케이스의 설정 변경 때 지워지지 않는다. 시험은 설정 적용 10분 전, 중지 4분 전."""

    SPEED = 6

    def test_stop_line_runs_after_the_settings_change_of_the_next_case(self):
        e = self.e
        self.two_vms()
        c1 = case("s1", [vm("esx01ev01", "job_a", ["gmon"])], esx_monitors=["hmon"], seq_start="10:14")
        c2 = case("s2", [vm("esx01ev01", "job_a", (), ["pin"])], seq_start="10:30")
        res, plan = self.gen([c1, c2], stop_lead_min=4)
        self.assertIn("MSTOP 001 %d 2026-11-20 10:26\n" % epoch("2026-11-20 10:26:00"), res["plan"], "다음 케이스 10:30 의 4분 전")
        self.assertIn("SEG 002 1\n", res["plan"], "다음 케이스는 설정 변경 케이스")
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=900)
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(e.state(RID, "esx01")["state"], "done", self.ctx(out))
        log = self.log()
        self.assertIn("registered: case s2", log)
        self.assertEqual(self.mon_words("esx01", esx=True), [["start", "h"], ["stop", "h"]], "ESX 모니터링은 10:26 에 중지")
        self.assertEqual(self.mon_words("esx01ev01"), [["start", "g"], ["stop", "g"]], "VM 은 다시 켜진 뒤 10:26 에 중지 줄 실행")
        stops = [t for h, t, line in cron.fired if " stop " in line and "mon.sh" in line]
        self.assertEqual(sorted(set(stops)), [epoch("2026-11-20 10:26:00")], cron.fired)


if __name__ == "__main__":
    unittest.main()
