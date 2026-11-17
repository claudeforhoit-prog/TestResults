# -*- coding: utf-8 -*-
"""
tcrun(점프 VM 진행 스크립트)과 tcgen_apply.py 의 통합 시험.

가짜 환경(fakeenv.py): 가짜 ssh(VM 별 홈 폴더, ESX 는 키 접속), 가짜 crontab, 등록된 줄을 그 시각에 실행하는 cron 흉내,
가짜 vim-cmd 와 esxcli, 가짜 vmx 폴더, 가상 시계(date 와 sleep 을 가짜 명령으로 바꿔 시간이 빨리 감).
tcrun 과 tcgen_apply.py 에는 시험용 코드가 없다. 경로만 환경 변수(TCGEN_TMP 등)로 바꾼다.

경우: (a) 케이스 2개의 전체 흐름 (b) 값이 이미 같음 (d) 게스트 종료 불가 (e) 전원 켜기 실패와 rollback
(f) 프로세스가 보이지 않음 (g) ssh 일시 실패 (h) stop (i) 없는 VM, 없는 호스트 옵션 (j) 판 불일치
(k) ESX 호스트 2대 (l) 설정만 있는 케이스, 그리고 VM 을 켠 뒤 대기, 시계 차이, 점검(check), 등록 실패, 최대 대기,
VM 전원(꺼진 VM 을 하나씩 켜기, 1GB page backing, 끔인 VM), 시작 시각 준비, 잡 시작 최소 간격,
잡을 확인하지 않는 케이스(시작 시각 지정), 같은 ESX 호스트의 실행 대기(queue), 열린 케이스 닫기, cancel.
"""
import csv
import datetime as dt
import io
import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakeenv  # noqa: E402
from fakeenv import core  # noqa: E402

GEN = dt.datetime(2026, 11, 20, 10, 0, 0)
RID = "20261120-100000"


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

    def setUp(self):
        # 생성기는 시작 시각을 이 PC 의 시간대로 초(epoch)로 바꾼다. 가짜 환경의 시계는 KST 이므로 시험 프로세스도 KST 로 맞춘다
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.e = fakeenv.FakeEnv(dash=self.DASH, jdir=self.JDIR)
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

    def tc_lines(self, name):
        return [x for x in self.e.vm_crontab(name) if " # tcgen " in x]

    def fired(self, cron, host, word):
        return [t for h, t, line in cron.fired if h == host and word in line]


class FullFlowTests(TcrunBase):
    def test_a_two_cases(self):
        """(a) 케이스 2개: 설정 적용, VM 순서대로 켜기, 모니터링, VM 2대의 잡, 종료 판정, 중지, 줄 삭제, 로그 3종."""
        e = self.e
        self.two_vms()
        c1 = case("caseA", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"], desc="첫 케이스, 설명")
        c2 = case("caseB", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"])
        res, plan = self.gen([c1, c2], mon_lead_min=5, seq_settle_min=10)
        orig_vmx = e.vmx("esx01", "esx01ev01")
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        # 단계 순서
        self.assert_order(log, [
            "==== case caseA (1 of 2) begin. VMs: esx01ev01 esx01ev02", "TCGEN-SET esx01ev01 sched.mem.pin: (none) -> \"TRUE\"  [change]",
            "TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 0  [change]", "TCGEN-RESULT changed 3 value(s)",
            "settings: applied. The VMs of this case are powered off now.",
            "esx01ev01: powered on", "vm esx01ev01: 1GB pages are not set for this VM", "vm esx01ev01: ssh login works, ready",
            "esx01ev02: powered on", "vm esx01ev02: ssh login works, ready",
            "VM power: done. Powered on by this case: esx01ev01 esx01ev02", "settle: VMs of this case were powered on again",
            "registered on vm esx01ev01:",
            "registered on vm esx01ev02:", "registered on esx esx01 (UTC):", "times: monitoring start",
            "the process is seen on every VM with a job", "job ended:", "monitoring stop registered on vm esx01ev01:",
            "monitoring stop registered on esx esx01 (UTC):", "removed the lines of this case: vm esx01ev01",
            "removed the lines of this case: esx esx01", "==== case caseA done",
            "==== case caseB (2 of 2) begin", "settings: every value is already as wanted. The VMs are not shut down for the settings.",
            "vm esx01ev01: powered on already", "VM power: done. Every VM was on already", "registered on vm esx01ev01:", "job ended:", "==== case caseB done", "run finished: all 2 case(s) done"])
        self.assertTrue(all(ord(ch) < 128 for ch in log), "로그는 ASCII")
        # 설정: vmx 와 호스트 옵션, VM 을 켠 순서
        self.assertTrue(e.vmx("esx01", "esx01ev01").endswith('sched.mem.pin = "TRUE"\n'))
        self.assertNotIn("numa.nodeAffinity", e.vmx("esx01", "esx01ev01"))
        self.assertNotEqual(e.vmx("esx01", "esx01ev01"), orig_vmx)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.on" in x], ["vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11"])
        self.assertEqual(len([x for x in calls if "power.shutdown" in x]), 2, "두 번째 케이스는 값이 같아 VM 을 끄지 않음")
        # 결과 CSV
        rows = self.results()
        self.assertEqual([(r["case"], r["esx"], r["apply"], r["status"]) for r in rows],
                         [("caseA", "esx01", "changed", "done"), ("caseB", "esx01", "nochange", "done")])
        a, b = rows
        for r in rows:
            self.assertEqual(epoch(r["job_start"]) - epoch(r["monitor_start"]), 300, "잡 시작 = 모니터링 시작 + 5분")
            self.assertEqual(epoch(r["monitor_start"]) % 60, 0)
            self.assertLessEqual(epoch(r["job_start"]), epoch(r["first_seen"]))
            self.assertLess(epoch(r["first_seen"]), epoch(r["job_end"]))
            self.assertLess(epoch(r["job_end"]), epoch(r["monitor_stop"]))
            self.assertLessEqual(epoch(r["monitor_stop"]) + 20, epoch(r["end"]))
        self.assertLessEqual(epoch(a["end"]), epoch(b["begin"]) + 1, "앞 케이스가 끝난 뒤에 다음 케이스 시작")
        # VM 을 다시 켠 케이스: 잡 시작이 VM 켜기 완료 10분 뒤 이후. 다시 켜지 않은 케이스: 그 대기가 없음
        self.assertGreaterEqual(epoch(a["job_start"]) - epoch(a["vms_on"]), 600)
        # 여유 값은 가짜 환경의 접속 지연(시계 차이로 계산됨)과 분 올림을 넉넉히 잡은 것이다
        self.assertLess(epoch(a["job_start"]) - epoch(a["vms_on"]), 600 + 240, "필요한 만큼만 기다림(분 올림과 시계 여유)")
        self.assertLess(epoch(b["job_start"]) - epoch(b["begin"]), 300 + 420, "두 번째 케이스는 등록 뒤 몇 분 안에 모니터링 시작(10분 대기 없음)")
        self.assertEqual(len(re.findall(r"settle: .*first start time moved", log)), 1)
        # cron 이 실행한 줄: 같은 분에 잡 시작, 모니터링 시작과 5분 간격, ESX 는 UTC
        for r, name in ((a, "caseA"), (b, "caseB")):
            mon, job, stop = epoch(r["monitor_start"]), epoch(r["job_start"]), epoch(r["monitor_stop"])
            self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh start g # tcgen %s " % name), [mon])
            self.assertEqual(self.fired(cron, "esx01", "mon.sh start h # tcgen %s " % name), [mon])
            self.assertEqual(self.fired(cron, "esx01ev01", "job.sh simA 100 # tcgen %s " % name), [job])
            self.assertEqual(self.fired(cron, "esx01ev02", "job.sh sim.B+x 150 # tcgen %s " % name), [job])
            self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop g # tcgen %s " % name), [stop])
            self.assertEqual(self.fired(cron, "esx01", "mon.sh stop h # tcgen %s " % name), [stop])
        self.assertEqual(len(cron.fired), 12, cron.fired)
        esx_line = [line for h, t, line in cron.fired if h == "esx01" and "start h # tcgen caseA" in line][0]
        utc = dt.datetime.fromtimestamp(epoch(a["monitor_start"]), dt.timezone.utc)
        self.assertTrue(esx_line.startswith(utc.strftime("%M %H %d %m * ")), esx_line)
        # 잡이 실제로 끝난 뒤에 종료로 판정
        j1, j2 = e.vm_file("esx01ev01", "job.log"), e.vm_file("esx01ev02", "job.log")
        self.assertEqual([x[0] for x in j1], ["start", "end", "start", "end"])
        real_end_a = max(int(j1[1][2]), int(j2[1][2]))
        self.assertGreaterEqual(epoch(a["job_end"]), real_end_a - 1)
        self.assertLess(epoch(a["job_end"]), real_end_a + 150)
        self.assertEqual(int(j1[0][2]) // 60, int(j2[0][2]) // 60, "두 VM 의 잡이 같은 분에 시작")
        m1 = e.vm_file("esx01ev01", "mon.log")
        self.assertEqual([x[0] for x in m1], ["start", "stop", "start", "stop"])
        self.assertGreaterEqual(int(m1[1][2]), real_end_a, "모니터링 중지는 잡 종료 뒤")
        # 줄 삭제: tcgen 줄은 남지 않고 원래 있던 줄은 그대로
        self.assertEqual(e.vm_crontab("esx01ev01"), ["30 9 * * 1 /home/tester/existing.sh > /tmp/x.log 2>&1"])
        self.assertEqual(e.vm_crontab("esx01ev02"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        self.assertFalse(os.path.exists(os.path.join(e.vm_dir("esx01ev01"), ".tcgen_cron.new")))
        # 리포트용 CSV: BOM, 머리글, 실제 시각
        raw, rep = self.report()
        self.assertTrue(raw.startswith("﻿테스트 그룹,케이스명,설명,호스트 이름,잡 시작 시각,잡 종료 시각,VM,종료 기준 VM\r\n"))
        self.assertEqual(rep[1], ["g1", "caseA", "첫 케이스, 설명", "esx01", a["job_start"], a["job_end"], "esx01ev01;esx01ev02", ""])
        self.assertEqual(rep[2][:2] + rep[2][4:6], ["g1", "caseB", b["job_start"], b["job_end"]])
        self.assertEqual(len(rep), 3)
        # status
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : done" % RID, out)
        self.assertIn("run finished: all 2 case(s) done", out)

    def test_b_values_already_the_same(self):
        """(b) 값이 이미 같으면 VM 을 끄지 않는다. 중지 명령이 없는 모니터링은 줄만 지운다."""
        e = self.e
        self.two_vms()
        c = case("same1", [vm("esx01ev01", "job_a", ["gonce"], ["same"]), vm("esx01ev02", "(없음)", [], ["same"])])
        res, plan = self.gen([c])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if any(w in x for w in ("power.shutdown", "power.off", "power.on", "reload", " set "))], [])
        r = self.results()[0]
        self.assertEqual((r["apply"], r["status"], r["vms_on"], r["monitor_stop"]), ("nochange", "done", "", ""))
        self.assertNotIn("settle:", self.log())
        self.assertEqual(len(cron.fired), 2, "모니터링 시작과 잡. 중지 명령은 없음")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(epoch(r["job_start"]) - epoch(r["monitor_start"]), 60)

    def test_q_job_without_monitoring(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("solo", [vm("esx01ev02", "job_a")], esx="")])
        cron = e.start_cron()
        t0 = e.now()
        rc, out = e.tcrun("run", plan, "-")
        log = self.log("noesx")
        self.assertEqual(rc, 0, out + log)
        r = self.results("noesx")[0]
        self.assertEqual((r["esx"], r["apply"], r["monitor_start"], r["monitor_stop"], r["status"]), ("noesx", "none", "", "", "done"))
        self.assertEqual(epoch(r["job_start"]) % 60, 0)
        self.assertGreaterEqual(epoch(r["job_start"]) - t0, 75)
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh simA"), [epoch(r["job_start"])])
        self.assertEqual(e.vm_crontab("esx01ev02"), [])
        self.assertEqual(e.esx_calls("esx01"), [], "ESX 호스트를 적지 않은 케이스는 ESX 에 접속하지 않음")
        self.assertIn("times: job start", log)

    def test_d_guest_shutdown_not_possible(self):
        """(d) 게스트 종료가 안 되면 전원을 끄고 진행한다."""
        e = self.e
        self.two_vms(vm1={"shutdown": "fault"})
        res, plan = self.gen([case("d1", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev02", "job_a")])])
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.off" in x], ["vim-cmd vmsvc/power.off 10"])
        self.assertIn("esx01ev01: guest shutdown is not possible (Cannot complete operation because VMware Tools is not running", log)
        self.assertEqual(self.results()[0]["status"], "done")

    def test_c_vmx_with_the_same_key_on_many_lines(self):
        """(c) 같은 키가 다른 값으로 여러 줄 있고 대소문자가 다른 vmx: 한 줄만 남기고, 값을 비운 키는 지운다."""
        e = self.e
        e.add_vm("esx01ev01")
        odd = ('displayName = "esx01ev01"\nsched.mem.pin = "FALSE"\nSched.Mem.Pin = "maybe"\n# note\n'
               'Numa.NodeAffinity = "3"\nSCHED.MEM.PIN = "TRUE"\nguestOS = "rhel8-64"\n')
        e.add_esx("esx01", vms=[("esx01ev01", {})], vmx={"esx01ev01": odd})
        res, plan = self.gen([case("c1", [vm("esx01ev01", "(없음)", [], ["pin"])])])
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(e.vmx("esx01", "esx01ev01"),
                         'displayName = "esx01ev01"\n# note\nguestOS = "rhel8-64"\nsched.mem.pin = "TRUE"\n')
        self.assertIn('TCGEN-SET esx01ev01 sched.mem.pin: 3 lines: "FALSE", "maybe", "TRUE" -> "TRUE"  [change]', log)
        self.assertIn('TCGEN-SET esx01ev01 numa.nodeAffinity: "3" -> (remove)  [change]', log)
        self.assertEqual(self.results()[0]["status"], "done (settings only)")
        # 다시 실행하면 값이 같아 VM 을 끄지 않는다
        res, plan = self.gen([case("c2", [vm("esx01ev01", "(없음)", [], ["pin"])])], now=GEN + dt.timedelta(seconds=9))
        n = len(e.esx_calls("esx01"))
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, out)
        self.assertEqual([x for x in e.esx_calls("esx01")[n:] if "power.shutdown" in x or "power.o" in x], [])
        self.assertEqual(self.results(rid=res["run_id"])[0]["apply"], "nochange")

    def test_l_settings_only_case_then_job_case(self):
        """(l) 설정만 있는 케이스는 적용만 하고 넘어간다. 그 VM 을 쓰는 다음 케이스의 잡도 VM 켜기 완료 N분 뒤에 시작한다."""
        e = self.e
        self.two_vms()
        c1 = case("setonly", [vm("esx01ev01", "(없음)", [], ["pin"])], esx_profiles=["numa0"])
        c2 = case("after", [vm("esx01ev01", "job_a")])
        res, plan = self.gen([c1, c2], seq_settle_min=6)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        rows = self.results()
        self.assertEqual([(r["case"], r["apply"], r["status"]) for r in rows],
                         [("setonly", "changed", "done (settings only)"), ("after", "none", "done")])
        self.assertEqual((rows[0]["job_start"], rows[0]["monitor_start"]), ("", ""))
        self.assertEqual(rows[1]["vms_on"], rows[0]["vms_on"])
        self.assertGreaterEqual(epoch(rows[1]["job_start"]) - epoch(rows[0]["vms_on"]), 360)
        self.assertEqual([x for x in e.esx_calls("esx01") if "power.on" in x], ["vim-cmd vmsvc/power.on 10"],
                         "설정을 지정하지 않은 VM 은 끄지도 켜지도 않음")
        with open(os.path.join(e.esx_dir("esx01"), "tmp", "apply.log")) as f:
            self.assertIn(" poweron esx01ev01 5400\n", f.read(), "VM 켜기 대기: plan 에 값이 없으면 90분을 초로 넘김")
        self.assertIn("power on wait 90 min", self.log())
        self.assertEqual(len(self.report()[1]), 2, "잡이 없는 케이스는 리포트용 CSV 에 넣지 않음")
        self.assertEqual(len(cron.fired), 1)

    def test_settle_zero_is_the_plain_calculation(self):
        """N 이 0 이면 VM 을 다시 켠 케이스도 기존 계산(등록 뒤 1~2분)과 같다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("s0", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])])], seq_settle_min=0, mon_lead_min=2,
                             seq_poweron_wait_min=7)
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        r = self.results()[0]
        with open(os.path.join(e.esx_dir("esx01"), "tmp", "apply.log")) as f:
            self.assertIn(" poweron esx01ev01 420\n", f.read(), "VM 켜기 대기: 설정값(분)을 초로 넘김")
        self.assertIn("power on wait 7 min", log)
        self.assertNotIn("settle:", log)
        self.assertNotEqual(r["vms_on"], "")
        m = re.search(r"^(\S+ \S+) clock: vm esx01ev01 is", log, re.M)
        probe = epoch(m.group(1))
        self.assertLess(epoch(r["monitor_start"]) - probe, 75 + 60 + 90, "시계를 읽은 시각 + 75초를 분으로 올린 값(10분 대기 없음)")
        self.assertEqual(epoch(r["job_start"]) - epoch(r["monitor_start"]), 120)

    def test_m_clock_difference_between_vms(self):
        """시각은 가장 늦은 시계 기준으로 정하고, VM 시계가 30초 넘게 다르면 경고를 남긴다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02", clock_offset=50)
        e.add_esx("esx01", vms=[("esx01ev01", {}), ("esx01ev02", {})])
        res, plan = self.gen([case("skew", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")])])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertRegex(log, r"WARN the clocks of the VMs differ by [3-9]\d s")
        self.assertRegex(log, r"clock: vm esx01ev02 is [5-9]\d s ahead")          # 50초 + 가짜 환경의 접속 지연
        r = self.results()[0]
        job = epoch(r["job_start"])
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [job])
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [job], "시계가 빠른 VM 에서도 등록 시각이 지나지 않아 실행됨")
        j1, j2 = e.vm_file("esx01ev01", "job.log"), e.vm_file("esx01ev02", "job.log")
        self.assertEqual((j1[0][0], j2[0][0]), ("start", "start"))


class StartTimeAndGapTests(TcrunBase):
    def test_gap_between_cases(self):
        """케이스 사이 간격: 시각을 비운 다음 케이스의 잡은 앞 케이스의 잡이 끝난 시각에서 N분 이상 지난 뒤에 시작한다. 첫 케이스에는 없다."""
        e = self.e
        self.two_vms()
        c1 = case("g1", [vm("esx01ev01", "job_a", ["gmon"])])
        c2 = case("g2", [vm("esx01ev01", "job_a", ["gmon"])])
        res, plan = self.gen([c1, c2], seq_gap_min=15, mon_lead_min=2)        # 가짜 환경에서 간격 없이 걸리는 시간(8분쯤)보다 길게
        self.assertIn("SET GAP_MIN 15\n", res["plan"])
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        a, b = self.results()
        self.assertGreaterEqual(epoch(b["job_start"]) - epoch(a["job_end"]), 15 * 60, "앞 잡 종료에서 15분 이상 지난 뒤 다음 잡 시작")
        self.assertLess(epoch(b["job_start"]) - epoch(a["job_end"]), 15 * 60 + 150, "필요한 만큼만 기다림(분 올림과 시계 여유)")
        self.assertEqual(epoch(b["job_start"]) - epoch(b["monitor_start"]), 120, "간격이 있어도 모니터링은 잡 시작 2분 전에 시작")
        self.assertEqual(len(re.findall(r"gap: the previous job ended .* first start time moved", log)), 1)
        self.assertLess(epoch(a["job_start"]) - epoch(a["begin"]), 75 + 60 + 120 + 90, "첫 케이스에는 간격을 적용하지 않음")
        self.assertIn("15 min or more between the end of a job and the start of the next job", log)
        self.assertNotIn("WARN", log)

    def test_start_time_of_the_plan(self):
        """시작 시각을 적은 케이스는 그 시각에 잡을 시작하고(모니터링은 그 N분 전) 시각을 비운 다음 케이스는 앞 케이스가 끝난 뒤 시작한다."""
        e = self.e
        self.two_vms()
        c1 = case("t1", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_a")], seq_start="10:12")
        c2 = case("t2", [vm("esx01ev01", "job_a")])
        res, plan = self.gen([c1, c2], mon_lead_min=3, seq_gap_min=0)
        self.assertRegex(res["plan"], r"(?m)^START 001 %d 2026-11-20 10:12$" % int(epoch("2026-11-20 10:12:00")))
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        a, b = self.results()
        self.assertEqual((a["job_start"], a["monitor_start"]), ("2026-11-20 10:12:00", "2026-11-20 10:09:00"), self.ctx(out))
        self.assertIn("start time: the plan sets the job start of this case to 2026-11-20 10:12", log)
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh")[0], epoch("2026-11-20 10:12:00"), "cron 이 그 분에 잡을 실행")
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [epoch("2026-11-20 10:12:00")], "잡이 있는 모든 VM 이 같은 분에 시작")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh start")[0], epoch("2026-11-20 10:09:00"))
        self.assertLess(epoch(b["job_start"]) - epoch(a["end"]), 75 + 60 + 90, "시각을 비운 다음 케이스는 앞 케이스가 끝나면 이어서 시작(간격 0)")
        self.assertNotIn("WARN the start time", log)
        self.assertNotIn("gap:", log)

    def test_start_time_that_has_passed_starts_as_soon_as_possible(self):
        """지정한 시각을 지킬 수 없으면(앞 케이스가 그때까지 끝나지 않음) 가능한 가장 이른 시각에 시작하고 WARN 을 남긴다."""
        e = self.e
        self.two_vms()
        c1 = case("p1", [vm("esx01ev01", "job_b")])                # 150초 걸리는 잡
        c2 = case("p2", [vm("esx01ev01", "job_a")], seq_start="10:03")
        res, plan = self.gen([c1, c2], seq_gap_min=30)
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        a, b = self.results()
        self.assertGreater(epoch(a["job_end"]), epoch("2026-11-20 10:03:00"), "앞 케이스가 지정한 시각보다 늦게 끝남")
        self.assertIn("WARN the start time 2026-11-20 10:03 of the plan cannot be met any more", log)
        self.assertLess(epoch(b["job_start"]) - epoch(a["end"]), 75 + 60 + 90, "지킬 수 없으면 가능한 가장 이른 시각에 시작")
        self.assertNotIn("gap:", log, "시각을 적은 케이스에는 케이스 사이 간격을 적용하지 않음")
        self.assertEqual(b["status"], "done (not checked, open)", "시각을 적은 마지막 케이스는 확인하지 않고, 끝나는 시각이 없어 열린 채로 남음")

    def test_check_reports_a_start_time_that_has_passed(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("k1", [vm("esx01ev01", "job_a")], seq_start="09:55"),
                              case("k2", [vm("esx01ev01", "job_a")], seq_start="23:00")], now=dt.datetime(2026, 11, 20, 9, 50, 0))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] case k1: the start time 2026-11-20 09:55 has already passed", out)
        self.assertRegex(out, r"\[ok\] start time: job at 2026-11-20 23:00 \(\d+ min from now\)")
        self.assertRegex(out, r"CHECK RESULT: 1 problem")


    def test_preparation_starts_the_set_minutes_before_the_start_time(self):
        """시작 시각을 적은 케이스는 그 시각 '시작 시각 준비' 분 전까지 기다린 뒤 설정 적용과 VM 전원 맞추기를 한다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("t1", [vm("esx01ev01", "job_a", [], ["pin"])], seq_start="10:20")], seq_prep_lead_min=10)
        self.assertIn("SET PREP_LEAD_MIN 10\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertIn("start time 2026-11-20 10:20: the preparation (settings, VM power) starts 10 min before it, at 2026-11-20 10:10:00", log)
        first = next(x for x in log.splitlines() if "settings wanted by this case:" in x)
        self.assertGreaterEqual(epoch(first[:19]), epoch("2026-11-20 10:10:00"), "설정 적용은 10:10 이후")
        self.assertLess(epoch(first[:19]), epoch("2026-11-20 10:11:00"))
        r = self.results()[0]
        self.assertEqual(r["job_start"], "2026-11-20 10:20:00")


class CheckIntervalTests(TcrunBase):
    def test_few_logins_while_the_job_runs(self):
        """잡이 실행되는 동안 VM 접속을 줄인다: 잡 시작 뒤 한 번 확인하고 그 뒤로는 확인 간격마다 확인한다. 모든 VM 에서 프로세스가
        없어진 뒤에만 1분 간격으로 다시 확인한다. 확인 간격이 2분을 넘으면 리포트용 CSV 의 잡 종료 시각을 비운다."""
        e = self.e
        self.two_vms()
        c1 = case("q1", [vm("esx01ev01", "job_long", ["gmon"])])            # 900초 걸리는 잡
        res, plan = self.gen([c1], seq_poll_sec=600, seq_first_check_sec=60, seq_gone_count=3)
        self.assertIn("SET POLL_SEC 600\nSET FIRST_CHECK_SEC 60\n", res["plan"])
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        r = self.results()[0]
        job = epoch(r["job_start"])
        checks = []
        for m in re.finditer(r"(?m)^(\S+ \S+) check (\d+) \(case q1\) vm esx01ev01: (.+)$", log):
            self.assertEqual(int(m.group(2)), len(checks) + 1, "확인 번호는 1부터 차례로")
            checks.append((epoch(m.group(1)), int(m.group(3).split()[0]) if m.group(3)[0].isdigit() else 0))
        self.assertEqual([n for _, n in checks], [1, 1, 0, 0, 0], "잡이 실행되는 15분 동안 확인은 두 번, 끝난 뒤 세 번\n" + log)
        t = [x - job for x, _ in checks]
        self.assertTrue(60 <= t[0] < 60 + 40, "첫 확인은 잡 시작 60초 뒤: %s" % t)
        self.assertTrue(600 - 5 <= t[1] - t[0] < 600 + 40, "두 번째 확인은 확인 간격(600초) 뒤: %s" % t)
        self.assertTrue(600 - 5 <= t[2] - t[1] < 600 + 40, t)
        self.assertTrue(55 <= t[3] - t[2] < 60 + 40 and 55 <= t[4] - t[3] < 60 + 40, "프로세스가 없어진 뒤에는 1분 간격으로 다시 확인: %s" % t)
        self.assertEqual(len(self.e.vm_file("esx01ev01", "job.log")), 2, "잡은 한 번 시작하고 끝남")
        # 종료 시각: 결과 CSV 에는 프로세스가 없는 것을 처음 확인한 시각, 리포트용 CSV 에는 빈칸(리포트 생성기가 정한다)
        self.assertTrue(0 <= checks[2][0] - epoch(r["job_end"]) <= 30, "job_end 는 프로세스가 없는 것을 처음 확인한 때(세 번째 확인)")
        self.assertIn("The report case file leaves the job end empty", log)
        raw, rows = self.report()
        self.assertEqual(len(rows), 2)
        head, row = rows
        self.assertEqual((row[head.index("잡 시작 시각")], row[head.index("잡 종료 시각")]), (r["job_start"], ""))
        self.assertIn("first check 60 s after the job start and then every 600 s", log)
        # 확인마다 VM 이름과 시각이 있는 줄, 그리고 다음 확인 시각
        self.assertRegex(log, r"(?m)^\S+ \S+ check 1 \(case q1\) vm esx01ev01: 1 process\(es\) running \(seen for the first time\)$")
        self.assertRegex(log, r"(?m)^\S+ \S+ check 2 \(case q1\) vm esx01ev01: 1 process\(es\) running$")
        self.assertRegex(log, r"(?m)^\S+ \S+ check 3 \(case q1\) vm esx01ev01: no process \(1 of 3 in a row\)$")
        done = re.findall(r"(?m)^(\S+ \S+) check (\d) \(case q1\) done\. next check (\S+ \S+)$", log)
        self.assertEqual([k for _, k, _ in done], ["1", "2", "3", "4"], "마지막 확인 뒤에는 다음 확인이 없음")
        self.assertTrue(595 <= epoch(done[0][2]) - epoch(done[0][0]) <= 600, "다음 확인 시각 = 확인을 마친 때 + 확인 간격")
        self.assertTrue(55 <= epoch(done[2][2]) - epoch(done[2][0]) <= 60, "프로세스가 없어진 뒤의 다음 확인은 1분 뒤")
        self.assertNotIn("poll:", log)

    def test_short_interval_keeps_the_end_time_in_the_report_file(self):
        """확인 간격이 2분 이하이면 리포트용 CSV 에 종료로 판정한 시각을 적는다(시각이 정확하므로)."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a")])])      # 가짜 환경의 기본 확인 간격은 20초
        e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        r = self.results()[0]
        raw, rows = self.report()
        head, row = rows
        self.assertEqual(row[head.index("잡 종료 시각")], r["job_end"])
        self.assertNotIn("leaves the job end empty", self.log())


class FailureTests(TcrunBase):
    def test_e_power_on_failure_rolls_back_and_pauses(self):
        """(e) 전원 켜기 실패: 설정을 되돌리고, 켜져 있던 VM 을 순서대로 다시 켜 보고, 멈춘다."""
        e = self.e
        self.two_vms(vm2={"poweron": "fail"})
        orig = e.vmx("esx01", "esx01ev01")
        c1 = case("e1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_a")], esx_profiles=["numa0"])
        c2 = case("e2", [vm("esx01ev01", "job_a")])
        res, plan = self.gen([c1, c2])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("power on of vm esx01ev02 failed", st["text"])
        self.assertIn("Failed to power on VM", st["text"])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig, "vmx 를 백업으로 되돌림")
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130", "호스트 옵션을 이전 값으로")
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if "power.on" in x],
                         ["vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11", "vim-cmd vmsvc/power.on 10", "vim-cmd vmsvc/power.on 11"])
        self.assert_order(log, ["TCGEN-RESULT changed", "power on of vm esx01ev02 failed", "esx01ev01: powering off before the .vmx is put back",
                                "TCGEN-DONE vm esx01ev01: .vmx put back", "TCGEN-DONE host /Numa/LocalityWeightActionAffinity: put back to 130",
                                "rollback: rolledback 1 VM(s), 1 host option(s)", "vm esx01ev01: ssh login works",
                                "WARN power on of vm esx01ev02 failed", "PAUSED:", "Not finished: e1 e2"])
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "off"))
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        rows = self.results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["apply"], "rolledback")
        self.assertTrue(rows[0]["status"].startswith("paused: power on of vm esx01ev02 failed"))
        self.assertEqual(self.report()[1], [], "끝나지 않은 케이스는 리포트용 CSV 에 없음")

    def test_i_unknown_vm_is_an_error_before_any_change(self):
        """(i) ESX 에 없는 VM 이름: 아무것도 바꾸지 않고 멈춘다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx01ev09")
        orig = e.vmx("esx01", "esx01ev01")
        res, plan = self.gen([case("i1", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev09", "job_a", [], ["pin"])],
                                   esx_profiles=["numa0"])])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("VM 'esx01ev09' is not registered on this host", st["text"])
        calls = e.esx_calls("esx01")
        self.assertEqual([x for x in calls if any(w in x for w in ("power.", "reload", " set "))], [])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130")
        self.assertIn("rollback: nothing", self.log())
        self.assertEqual(cron.fired, [])

    def test_i_unknown_host_option_is_an_error_before_any_change(self):
        e = self.e
        self.two_vms()
        orig = e.vmx("esx01", "esx01ev01")
        res, plan = self.gen([case("i2", [vm("esx01ev01", "job_a", [], ["pin"])], esx_profiles=["numa0", "nosuch"])])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        self.assertIn("host option '/Numa/NoSuchOption' cannot be read", e.state(RID, "esx01")["text"])
        self.assertEqual([x for x in e.esx_calls("esx01") if any(w in x for w in ("power.shutdown", "power.o", "reload", " set "))], [])
        self.assertEqual(e.vmx("esx01", "esx01ev01"), orig)
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "130")

    def test_f_process_never_seen(self):
        """(f) 프로세스가 시작 대기 안에 한 번도 보이지 않으면 멈춘다. 잡과 모니터링은 건드리지 않는다."""
        e = self.e
        self.two_vms()
        c1 = case("f1", [vm("esx01ev01", "job_wrong", ["gmon"]), vm("esx01ev02", "job_a")])
        res, plan = self.gen([c1, case("f2", [vm("esx01ev01", "job_a")])], seq_start_wait_min=2)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("never seen within 2 min after the job start on: esx01ev01", st["text"])
        self.assertRegex(log, r"(?m)^\S+ \S+ check 1 \(case f1\) vm esx01ev02: 1 process\(es\) running \(seen for the first time\)$")
        self.assertRegex(log, r"(?m)^\S+ \S+ check 1 \(case f1\) vm esx01ev01: no process yet \(not seen since the job start\)$",
                         "프로세스를 찾지 못한 VM 도 확인마다 한 줄")
        self.assertNotRegex(log, r"vm esx01ev01: \d+ process")
        self.assertRegex(log, r"(?m)^\S+ \S+ check 2 \(case f1\) vm esx01ev01: no process yet")
        r = self.results()[0]
        self.assertGreaterEqual(epoch(r["end"]) - epoch(r["job_start"]), 120)
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop"), [], "모니터링을 중지하지 않음")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "이미 실행된 줄은 그대로 둠")
        self.assertIn("monitoring stop commands of this case", log)
        self.assertIn("mon.sh stop g # tcgen f1 esx01ev01", log)
        self.assertIn("Not finished: f1 f2", log)
        self.assertEqual(len(self.results()), 1)

    def test_g_ssh_failure_is_not_counted_as_ended(self):
        """(g) 확인 중 ssh 가 실패하면 '알 수 없음'이고 종료로 세지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("g1", [vm("esx01ev01", "job_b")])])      # 잡 150초, 연속 2회 없음이면 종료
        e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "the process is seen on every VM" in self.log(), what="프로세스 확인")
        with open(os.path.join(e.vm_dir("esx01ev01"), "ssh_fail"), "w") as f:
            f.write("3")
        self.assertEqual(p.wait(timeout=300), 0, self.ctx())
        log = self.log()
        self.assertRegex(log, r"(?m)^\S+ \S+ check \d+ \(case g1\) vm esx01ev01: unknown \(ssh exit 255: ssh: connect to host esx01ev01 "
                              r"port 22: Connection timed out\)\. Not counted as ended\.$")
        job = e.vm_file("esx01ev01", "job.log")
        r = self.results()[0]
        self.assertEqual(r["status"], "done")
        self.assertGreaterEqual(epoch(r["job_end"]), int(job[1][2]) - 1, "ssh 실패 3회를 '없음'으로 셌다면 잡이 끝나기 전에 종료로 판정했을 것")

    def test_o_registration_failure_removes_the_lines(self):
        """한 VM 에서 등록이 안 되면 다른 VM 에 넣은 줄을 지우고 멈춘다. 일부 VM 만 잡이 시작되는 일이 없다."""
        e = self.e
        self.two_vms()
        open(os.path.join(e.vm_dir("esx01ev02"), "crontab_broken"), "w").close()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_a")])])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertIn("vm esx01ev02: crontab lines were not registered", st["text"])
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertIn("registered on vm esx01ev01:", self.log())
        self.assertIn("removed the lines of this case: vm esx01ev01", self.log())
        mdir = os.path.join(e.tcgen, "minutes")
        self.assertEqual(os.listdir(mdir) if os.path.isdir(mdir) else [], [], "등록이 실패하면 잡았던 잡 시작 분을 돌려줌")
        time.sleep(240 / e.speed)
        self.assertEqual(cron.fired, [], "지운 줄은 실행되지 않음")

    def test_slow_registration_never_leaves_a_half_started_case(self):
        """등록이 늦어져 시작 시각까지 15초가 남지 않으면 줄을 모두 지운다. 지우는 동안 시작 시각이 지났으면 멈추고 알린다."""
        e = self.e
        self.two_vms()
        with open(os.path.join(e.vm_dir("esx01ev02"), "crontab_slow"), "w") as f:
            f.write("140")                       # 두 번째 VM 의 crontab 등록이 140초 걸린다
        res, plan = self.gen([case("slow1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")]),
                              case("slow2", [vm("esx01ev01", "job_a")])])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        log = self.log()
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("took too long: the first start time", st["text"])
        self.assertIn("may have started on some hosts", st["text"])
        self.assert_order(log, ["registered on vm esx01ev01:", "registered on vm esx01ev02:",
                                "WARN registering ended", "removed the lines of this case: vm esx01ev01", "PAUSED:"])
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        self.assertEqual(self.fired(cron, "esx01ev02", "job.sh"), [], "늦게 등록된 VM 에서는 시작되지 않음")
        self.assertNotIn("slow2 (2 of 2) begin", log, "다음 케이스로 넘어가지 않음")

    def test_esx_key_not_registered(self):
        """ESX 에 키가 등록돼 있지 않으면 아무것도 바꾸지 않고 멈춘다(설정 적용, ESX 모니터링 모두)."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {})], authorized=False)
        cron = e.start_cron()
        res, plan = self.gen([case("k1", [vm("esx01ev01", "job_a", [], ["pin"])])])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        self.assertIn("no answer from the ESX host (ssh exit 255: root@esx01: Permission denied", e.state(RID, "esx01")["text"])
        self.assertEqual(e.esx_calls("esx01"), [])
        res, plan = self.gen([case("k2", [vm("esx01ev01", "job_a")], esx_monitors=["hmon"])], now=GEN + dt.timedelta(seconds=5))
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, out)
        self.assertIn("power on of vm esx01ev01 failed: no answer from the ESX host (ssh exit 255: root@esx01: Permission denied",
                      e.state(res["run_id"], "esx01")["text"], "ESX 호스트가 있는 케이스는 VM 전원을 맞추므로 ESX 키 접속이 먼저 필요")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(cron.fired, [])

    def test_p_max_wait(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("p1", [vm("esx01ev01", "job_long", ["gmon"])], max_wait="0:03")])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "paused")
        self.assertIn("did not end within the maximum wait (3 min)", st["text"])
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "잡은 계속 실행 중")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop"), [])
        r = self.results()[0]
        self.assertGreaterEqual(epoch(r["end"]) - epoch(r["job_start"]), 180)

    def test_j_version_mismatch(self):
        """(j) plan 을 만든 생성기의 판과 설치된 tcrun 의 판이 다르면 시작하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("j1", [vm("esx01ev01", "job_a")])])
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
        self.assertFalse(os.path.exists(os.path.join(e.root, "ssh_calls.txt")), "어디에도 접속하지 않음")
        self.assertEqual(cron.fired, [])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] the plan was made by tcgen 9.9, tcrun is %s: paste block A again" % core.VERSION, out)
        with open(plan, "w") as f:
            f.write("something else\n")
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual((rc, out.strip()), (2, "tcrun: not a tcgen plan of format 2: " + plan))


    def test_plan_with_unsafe_names_is_refused(self):
        """plan 은 생성기가 만들고 sha256 으로 확인하지만, tcrun 도 이름에 명령줄에서 뜻이 달라지는 글자가 없는지 다시 본다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("u1", [vm("esx01ev01", "job_a")])])
        with open(plan) as f:
            good = f.read()
        cron = e.start_cron()
        for n, (old, new) in enumerate((("PROC 001 esx01ev01 simA\n", "PROC 001 esx01ev01 sim'A\n"),
                                        ("VM 001 esx01ev01 esx01ev01\n", "VM 001 esx01ev01 -oProxyCommand=x\n"),
                                        ("JOB 001 esx01ev01 ", "JOB 001 esx01*v01 "))):
            self.assertIn(old, good)
            rid = "20261120-10000%d" % (n + 1)
            d = os.path.join(e.tcgen, "runs", rid)
            os.makedirs(d)
            with open(os.path.join(d, "plan"), "w") as f:
                f.write(good.replace(old, new).replace("RUNID " + RID, "RUNID " + rid))
            rc, out = e.tcrun("run", os.path.join(d, "plan"), "esx01")
            self.assertEqual(rc, 1, out)
            st = e.state(rid, "esx01")
            self.assertEqual(st["state"], "paused")
            self.assertIn("a name that tcrun does not accept", st["text"])
            rc, out = e.tcrun("check", os.path.join(d, "plan"))
            self.assertIn("[PROBLEM] case number 001: the plan holds a name that tcrun does not accept", out)
        self.assertFalse(os.path.exists(os.path.join(e.root, "ssh_calls.txt")), "어디에도 접속하지 않음")
        self.assertEqual(cron.fired, [])
        rc, out = e.tcrun("run", plan, "-x")
        self.assertEqual((rc, out.strip()), (2, "tcrun: bad ESX host name: -x"))
        d = os.path.join(e.tcgen, "runs", "20261120-100009")
        os.makedirs(d)
        with open(os.path.join(d, "plan"), "w") as f:
            f.write(good.replace("CHAIN esx01\n", "ADDR esx01 -oProxyCommand=x\nCHAIN esx01\n").replace("RUNID " + RID, "RUNID 20261120-100009"))
        rc, out = e.tcrun("run", os.path.join(d, "plan"), "esx01")
        self.assertEqual((rc, out.strip()), (2, "tcrun: bad ESX address in the plan for esx01"))


class PowerTests(TcrunBase):
    def test_vms_that_are_off_are_powered_on_one_after_another(self):
        """VM 이 모두 꺼져 있으면 표의 순서대로 하나씩 켠다. VM 마다 1GB page backing 이 메모리 GB 수에 닿고 ssh 로그인이 된 뒤 다음 VM 을
        켠다. 잡이 없는 VM 도 켠다. 잡은 마지막 VM 이 준비된 시각에서 '켠 뒤 대기' 이상 지난 뒤에 시작한다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02")
        slow = {"state": "off", "lpage_rate": 0.1, "lpage_cap": 16}
        e.add_esx("esx01", vms=[("esx01ev01", dict(slow)), ("esx01ev02", dict(slow))],
                  vmx={"esx01ev01": fakeenv.vmx_1g("esx01ev01"), "esx01ev02": fakeenv.vmx_1g("esx01ev02")})
        res, plan = self.gen([case("p1", [vm("esx01ev01", "job_a"), vm("esx01ev02")])], seq_settle_min=10)
        self.assertIn("VM 001 esx01ev02 esx01ev02\n", res["plan"], "잡이 없는 VM 도 그 케이스의 VM")
        rc, out = e.tcrun("check", plan)
        self.assertIn("[ok] settings and VM power: 2 VM(s) to power on.", out)
        self.assertIn("[ok] vm esx01ev01: powered off now. The run powers it on and then waits for its ssh login.", out)
        self.assertIn("CHECK RESULT: OK", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, [
            "==== case p1 (1 of 1) begin. VMs: esx01ev01 esx01ev02",
            "VM power: the VMs of this case are powered on one after another when off (power on, 1GB page backing, ssh login): esx01ev01 esx01ev02",
            "esx01ev01: powered on", "1GB page backings, target, failed attempts: 16 16 0",
            "vm esx01ev01: 1GB page backing complete (16 of 16 1GB pages, failed attempts 0)", "vm esx01ev01: ssh login works, ready",
            "esx01ev02: powered on", "vm esx01ev02: 1GB page backing complete (16 of 16 1GB pages, failed attempts 0)",
            "vm esx01ev02: ssh login works, ready", "VM power: done. Powered on by this case: esx01ev01 esx01ev02. The last one was ready at",
            "settle: VMs of this case were powered on again", "times: job start", "==== case p1 done"])
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "on"), "잡이 없는 VM 도 켬")
        self.assertGreaterEqual(vms["esx01ev02"]["on_at"] - vms["esx01ev01"]["on_at"], 160,
                                "둘째 VM 은 첫 VM 의 1GB backing(16개, 초당 0.1개 = 160초)이 끝난 뒤에 켬")
        r = self.results()[0]
        self.assertGreaterEqual(epoch(r["vms_on"]), vms["esx01ev02"]["on_at"] + 150, "준비 시각 = 둘째 VM 의 backing 과 ssh 로그인 뒤")
        self.assertGreaterEqual(epoch(r["job_start"]) - epoch(r["vms_on"]), 600, "잡은 마지막 VM 준비 10분 뒤 이후")
        self.assertIn("vsish -e get /memory/lpage/vmLpage/100011", e.esx_calls("esx01"))

    def test_vm_marked_off_is_shut_down(self):
        """전원 끔인 VM 은 켜져 있으면 끈다. 그 VM 에는 접속하지 않는다. 켜져 있던 VM 만 쓰면 켠 뒤 대기가 없다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a"), vm("esx01ev02", power="off")])], seq_settle_min=10)
        self.assertIn("OFF 001 esx01ev02\n", res["plan"])
        rc, out = e.tcrun("check", plan)
        self.assertIn("TCGEN-POWER esx01ev02 now=on want=off  [change]", out)
        self.assertIn("[ok] settings and VM power: 1 VM(s) to power off.", out)
        self.assertIn("[ok] vm esx01ev02: the case wants it powered off. No login.", out)
        self.assertIn("CHECK RESULT: OK", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assert_order(log, ["==== case o1 (1 of 1) begin. VMs: esx01ev01 esx01ev02 (powered off for this case: esx01ev02)",
                                "vm esx01ev02: shut down (the case wants it off)", "vm esx01ev01: powered on already",
                                "vm esx01ev01: 1GB pages are not set for this VM", "vm esx01ev01: ssh login works",
                                "VM power: done. Every VM was on already", "times: job start", "==== case o1 done"])
        self.assertEqual(e.esx_vms("esx01")["esx01ev02"]["state"], "off")
        with open(os.path.join(e.root, "ssh_calls.txt")) as f:
            self.assertNotIn("vm esx01ev02", f.read().splitlines(), "끔인 VM 에는 접속하지 않음")
        r = self.results()[0]
        self.assertEqual((r["status"], r["vms_on"]), ("done", ""))
        self.assertNotIn("settle:", log, "켜져 있던 VM 만 썼으므로 켠 뒤 대기가 없음")
        self.assertEqual(len(self.fired(cron, "esx01ev01", "job.sh")), 1)

    def test_backing_that_stops_pauses_and_keeps_the_settings(self):
        """1GB page backing 이 늘지 않고 실패 수가 있으면 멈춘다. 설정은 되돌리지 않고 VM 은 켜진 채로 둔다."""
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"lpage_rate": 1, "lpage_cap": 8, "lpage_failed": 3})],
                  vmx={"esx01ev01": fakeenv.vmx_1g("esx01ev01")})
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a")], esx_profiles=["numa0"]), case("s2", [vm("esx01ev01", "job_a")])])
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
        self.assertIn("Not finished: s1 s2", log)
        self.assertEqual(cron.fired, [])
        self.assertEqual(self.results()[0]["apply"], "changed")


class StopTests(TcrunBase):
    def test_h_stop_while_the_job_runs(self):
        """(h) stop: 진행만 끝낸다. 실행 중인 잡과 모니터링은 건드리지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("h1", [vm("esx01ev01", "job_long", ["gmon"])]), case("h2", [vm("esx01ev01", "job_a")])])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "the process is seen on every VM" in self.log(), what="프로세스 확인")
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("step=wait-job", out)
        rc, out = e.tcrun("stop", "esx09")
        self.assertEqual(out.strip(), "no running tcrun for esx09")
        rc, out = e.tcrun("stop")
        self.assertIn("stop requested: run %s esx esx01" % RID, out)
        self.assertEqual(p.wait(timeout=120), 1, self.ctx())
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "stopped")
        self.assertIn("while the job of case h1 was running", st["text"])
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "잡은 계속 실행 중")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop"), [])
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2)
        self.assertTrue(self.results()[0]["status"].startswith("stopped: while the job"))
        self.assertIn("Not finished: h1 h2", self.log())
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "runs", RID, "esx01.pid")))
        out = e.tcrun("stop")[1]
        self.assertTrue(out.startswith("no running tcrun\n"), out)
        self.assertIn("run %s esx esx01: not running (stopped: while the job of case h1 was running" % RID, out,
                      "멈춘 호스트는 상태와 함께 알리고 cancel 을 안내")
        self.assertIn("Case h1 is open: to remove its crontab lines and stop its monitoring: sh %s/tcrun cancel esx01" % e.tcgen, out)
        self.assertIn("esx esx01 : stopped", e.tcrun("status")[1])

    def test_h_stop_after_monitoring_started_before_the_job(self):
        """stop 이 모니터링 시작과 잡 시작 사이에 오면: 잡 줄을 지워 잡이 시작되지 않게 하고, 시작된 모니터링은 중지한다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("h3", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_a")], esx_monitors=["hmon"])],
                             mon_lead_min=5)
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: self.fired(cron, "esx01ev01", "mon.sh start"), what="모니터링 시작")
        e.tcrun("stop")
        self.assertEqual(p.wait(timeout=180), 1, self.ctx())
        log = self.log()
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "stopped")
        self.assertIn("before the job of case h3 started. Its lines were removed.", st["text"])
        self.assert_order(log, ["stop requested before the job start: removing the lines of this case",
                                "the monitoring of this case has started or is about to start: stopping it",
                                "monitoring stop registered on vm esx01ev01:", "monitoring stop time", "STOPPED on request"])
        time.sleep(400 / e.speed)                 # 잡을 등록했던 시각이 지나도
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh") + self.fired(cron, "esx01ev02", "job.sh"), [], "잡은 시작되지 않음")
        self.assertEqual(len(self.fired(cron, "esx01ev01", "mon.sh stop")), 1)
        self.assertEqual(len(self.fired(cron, "esx01", "mon.sh stop")), 1)
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())

    def test_h_stop_before_anything_started(self):
        """stop 이 모니터링 시작 전에 오면 등록한 줄을 지우고 끝낸다. 아무것도 실행되지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("h4", [vm("esx01ev01", "job_a", ["gmon"], ["pin"])])], seq_settle_min=10, mon_lead_min=2)
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "times: monitoring start" in self.log(), seconds=240, what="등록")
        e.tcrun("stop")
        self.assertEqual(p.wait(timeout=120), 1, self.ctx())
        log = self.log()
        self.assertIn("stop requested before the job start: removing the lines of this case", log)
        self.assertNotIn("stopping it", log)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        time.sleep(700 / e.speed)
        self.assertEqual(cron.fired, [])
        self.assertEqual(e.state(RID, "esx01")["state"], "stopped")


class MultiHostAndCheckTests(TcrunBase):
    def test_k_two_esx_hosts_run_separately(self):
        """(k) ESX 호스트 2대: 호스트별로 따로 진행하고 서로 기다리지 않는다. 같은 호스트의 진행은 겹쳐 시작하지 않는다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {})], addr="10.0.0.12")
        c1 = case("k1", [vm("esx01ev01", "job_long", ["gmon"])], esx_monitors=["hmon"])
        c2 = case("k2", [vm("esx02ev01", "job_a")], esx="esx02", esx_monitors=["hmon"])
        res, plan = self.gen([c1, c2], esx_addr="esx02=10.0.0.12")
        self.assertEqual(res["chains"], ["esx01", "esx02"])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        self.assertIn("started: run %s esx esx01   log: %s/tcrun-%s-esx01.log" % (RID, e.log, RID), out)
        self.assertIn("started: run %s esx esx02" % RID, out)
        # 같은 실행을 다시 시작하지 않는다
        rc, out2 = e.tcrun("start", plan)
        self.assertEqual(out2.count("was already started on this jump VM"), 2, out2)
        # 같은 ESX 호스트의 다른 실행은 대기한다(앞 실행이 done 으로 끝나면 시작)
        res2, plan2 = self.gen([case("k3", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(seconds=7))
        rc, out3 = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01   waiting for run %s of this ESX host to end. It starts when that has ended as done"
                      % (res2["run_id"], RID), out3)
        self.assertEqual(e.state(res2["run_id"], "esx01")["state"], "queued")
        e.wait_for(lambda: e.state(RID, "esx02").get("state") == "done", seconds=240, what="esx02 완료")
        self.assertEqual(e.state(RID, "esx01")["state"], "running", "esx02 는 esx01 을 기다리지 않고 끝남")
        self.assertIn("run finished: all 1 case(s) done", self.log("esx02"))
        self.assertIn("esx esx02 (10.0.0.12)", self.log("esx02"))
        self.assertNotIn("==== case k2", self.log("esx01"), "다른 ESX 호스트의 케이스를 진행하지 않음(잡 시작 간격을 맞춘 spacing 줄에는 이름이 나올 수 있음)")
        self.assertEqual([r["case"] for r in self.results("esx02")], ["k2"])
        self.assertEqual(len(self.fired(cron, "esx02", "mon.sh stop h")), 1)
        rc, st = e.tcrun("status")
        self.assertIn("esx esx01 : running", st)
        self.assertIn("esx esx02 : done", st)
        self.assertIn("== run %s  esx esx01 : queued" % res2["run_id"], st)
        rc, out4 = e.tcrun("stop", "esx01")
        self.assertIn("stop requested: run %s esx esx01" % RID, out4)
        self.assertIn("stop requested: run %s esx esx01" % res2["run_id"], out4, "대기 중인 실행에도 stop 을 알림")
        e.wait_for(lambda: e.state(RID, "esx01").get("state") == "stopped", what="esx01 stop")
        e.wait_for(lambda: e.state(res2["run_id"], "esx01").get("state") == "stopped", what="대기 중인 실행 stop")
        self.assertIn("while this run was queued behind run %s. Nothing of this run was started." % RID, e.state(res2["run_id"], "esx01")["text"])
        self.assertEqual(self.tc_lines("esx01ev02"), [], "대기하다 멈춘 실행은 아무것도 등록하지 않음")

    def minutes(self):
        d = os.path.join(self.e.tcgen, "minutes")
        out = {}
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            if not n.isdigit():
                continue
            with open(os.path.join(d, n, "who")) as f:
                out[int(n)] = f.read().strip()
        return out

    def test_job_starts_on_two_esx_hosts_are_five_minutes_apart(self):
        """ESX 호스트 2대를 동시에 시작해 두 진행이 같은 시각을 고르려 해도, 모든 잡 시작은 서로 5분(잡 시작 최소 간격) 이상 떨어진다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_vm("esx02ev02")
        e.add_esx("esx02", vms=[("esx02ev01", {}), ("esx02ev02", {})])
        cases = []
        for n in range(1, 4):                     # 같은 모양의 케이스를 두 호스트에 3개씩: 시각 계산이 같아 같은 분을 고르려 한다
            cases.append(case("a%d" % n, [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")]))
            cases.append(case("b%d" % n, [vm("esx02ev01", "job_a"), vm("esx02ev02", "job_a")], esx="esx02"))
        res, plan = self.gen(cases)
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        e.wait_for(lambda: all(e.state(RID, h).get("state") == "done" for h in ("esx01", "esx02")), seconds=600, what="두 호스트 완료")
        starts = {}
        for h in ("esx01", "esx02"):
            for r in self.results(h):
                self.assertEqual(r["status"], "done", self.ctx())
                starts[r["case"]] = epoch(r["job_start"])
        self.assertEqual(sorted(starts), ["a1", "a2", "a3", "b1", "b2", "b3"])
        ts = sorted(starts.values())
        self.assertTrue(all(b - a >= 300 for a, b in zip(ts, ts[1:])), "잡 시작이 서로 5분 이상: %s" % sorted((t, c) for c, t in starts.items()))
        # cron 이 실제로 실행한 잡의 분: 한 케이스의 VM 2대는 같은 분, 다른 케이스와는 다른 분
        for c, t in starts.items():
            hosts = ("esx01ev01", "esx01ev02") if c.startswith("a") else ("esx02ev01", "esx02ev02")
            self.assertEqual([t in self.fired(cron, hv, "job.sh") for hv in hosts], [True, True], "케이스 %s 의 VM 2대가 같은 분에 시작" % c)
        all_fired = [t for h, t, line in cron.fired if "job.sh" in line]
        self.assertEqual(len(all_fired), 12)
        self.assertEqual(len(set(all_fired)), 6, "12개 잡이 6개 분에 2개씩 (같은 케이스의 VM 끼리만 같은 분)")
        logs = self.log("esx01") + self.log("esx02")
        self.assertIn("spacing: the job of run %s, esx esx0" % RID, logs, "가까운 시각을 고르려던 진행이 5분 뒤로 옮김")
        own = self.minutes()
        self.assertEqual(sorted(own), sorted(starts.values()), "기록된 분 = 잡 시작 분")
        self.assertEqual(sorted(own.values()), sorted("run %s, esx %s, case %s" % (RID, "esx01" if c[0] == "a" else "esx02", c) for c in starts))

    def claim(self, when, who, reg=False):
        d = os.path.join(self.e.tcgen, "minutes", str(int(epoch(when))))
        os.makedirs(d)
        with open(os.path.join(d, "who"), "w") as f:
            f.write(who)
        if reg:
            open(os.path.join(d, "reg"), "w").close()

    def test_start_near_another_run_moves_by_the_spacing(self):
        """다른 실행의 잡 시작과 5분보다 가까운 시작 시각: 점검(P)이 알리고, 잡은 그 잡 시작 5분 뒤로 옮긴다(옮긴 자리도 다시 확인).
        진행이 죽어 남은, 등록하지 않은 미래 기록은 무시하고 지운다. 하루 넘게 지난 기록은 지운다."""
        e = self.e
        self.two_vms()
        self.claim("2026-11-20 10:12:00", "run 20261120-090000, esx esx09, case other")
        self.claim("2026-11-20 10:20:00", "run 20261120-090000, esx esx09, case other2", reg=True)
        self.claim("2026-11-20 10:28:00", "run 20261120-080000, esx esx08, case dead")
        dead = os.path.join(e.tcgen, "runs", "20261120-080000")
        os.makedirs(dead)
        with open(os.path.join(dead, "esx08.state"), "w") as f:
            f.write("state=running\n")
        with open(os.path.join(dead, "esx08.pid"), "w") as f:
            f.write("999999\n")
        self.claim("2026-11-18 10:00:00", "run old, esx esx09, case gone")
        res, plan = self.gen([case("m1", [vm("esx01ev01", "job_a")], seq_start="10:12")])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] case m1: the start time 2026-11-20 10:12 is less than 5 min from the job start 2026-11-20 10:12:00 of "
                      "run 20261120-090000, esx esx09, case other.", out)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertIn("WARN the start time 2026-11-20 10:12 of case m1 is less than 5 min from the job start 2026-11-20 10:12:00 of "
                      "run 20261120-090000, esx esx09, case other.", log)
        self.assert_order(log, ["spacing: the job of run 20261120-090000, esx esx09, case other starts 2026-11-20 10:12:00, less than 5 min "
                                "from 2026-11-20 10:12:00. This job starts at 2026-11-20 10:17:00",
                                "spacing: the job of run 20261120-090000, esx esx09, case other2 starts 2026-11-20 10:20:00, less than 5 min "
                                "from 2026-11-20 10:17:00. This job starts at 2026-11-20 10:25:00",
                                "times: job start 2026-11-20 10:25:00"])
        r = self.results()[0]
        self.assertEqual(r["job_start"], "2026-11-20 10:25:00")
        self.assertEqual(self.fired(cron, "esx01ev01", "job.sh"), [epoch("2026-11-20 10:25:00")])
        own = self.minutes()
        self.assertEqual(own, {int(epoch("2026-11-20 10:12:00")): "run 20261120-090000, esx esx09, case other",
                               int(epoch("2026-11-20 10:20:00")): "run 20261120-090000, esx esx09, case other2",
                               int(epoch("2026-11-20 10:25:00")): "run %s, esx esx01, case m1" % RID},
                         "다른 실행의 기록은 그대로. 죽은 진행의 미래 기록과 하루 넘게 지난 기록은 지움. 쓰지 않은 10:12 기록은 돌려줌")
        self.assertTrue(os.path.exists(os.path.join(e.tcgen, "minutes", str(int(epoch("2026-11-20 10:25:00"))), "reg")),
                        "등록을 마친 잡의 기록에는 reg")
        self.assertFalse(os.path.exists(os.path.join(e.tcgen, "minutes", ".lock")), "잠금은 남지 않음")

    def test_unused_start_times_are_given_back_when_the_run_ends(self):
        """멈춘 실행은 시작하지 않은 케이스의 시작 시각 기록을 지운다. 다시 생성한 실행이 그 시각을 그대로 쓸 수 있다."""
        e = self.e
        self.two_vms(vm1={"poweron": "fail", "state": "off"})
        res, plan = self.gen([case("u1", [vm("esx01ev01", "job_a")], seq_start="10:30"),
                              case("u2", [vm("esx01ev02", "job_a")], seq_start="11:00")], seq_prep_lead_min=25)
        rc, out = e.tcrun("start", plan)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.minutes()), [int(epoch("2026-11-20 10:30:00")), int(epoch("2026-11-20 11:00:00"))],
                         "start 가 적은 시작 시각을 먼저 기록(진행은 10:05 까지 준비를 기다리는 중)")
        e.wait_for(lambda: e.state(RID, "esx01").get("state") == "paused", seconds=240, what="켜기 실패로 멈춤")
        e.wait_for(lambda: not os.path.exists(os.path.join(e.tcgen, "runs", RID, "esx01.pid")), what="진행 끝")
        self.assertEqual(self.minutes(), {}, "멈춘 실행의 기록을 모두 돌려줌(등록한 잡이 없음)")

    def test_runner_that_died_is_reported_and_does_not_block_a_new_run(self):
        """진행 프로세스가 사라지면(점프 VM 재부팅 등) status 가 died 로 알리고, 같은 ESX 호스트의 새 실행을 막지 않는다."""
        import signal
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("z1", [vm("esx01ev01", "job_long")])])
        e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "times: job start" in self.log(), what="등록")
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : died (the runner is not running any more." % RID, out)
        self.assertTrue(e.tcrun("stop")[1].startswith("no running tcrun\n"))
        self.assertIn("open case: z1.", out)
        res2, plan2 = self.gen([case("z2", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(seconds=30))
        rc, out = e.tcrun("start", plan2)
        self.assertIn("==== esx01: not started. run %s of this ESX host ended as died and left case z1 open" % RID, out)
        self.assertIn("Close it first: sh %s/tcrun cancel esx01" % e.tcgen, out)
        self.assertEqual(e.state(res2["run_id"], "esx01"), {}, "시작하지 않았으므로 같은 S 블록을 다시 붙여넣을 수 있음")
        self.assertIn("esx esx01 : died", e.tcrun("status", "-a")[1])
        rc, out = e.tcrun("cancel", "esx01")
        self.assertIn("== run %s esx esx01: cancelling (state died, case z1, step wait-start)" % RID, out)
        self.assertEqual(self.tc_lines("esx01ev01"), [], "죽은 실행이 남긴 줄을 지움")
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % res2["run_id"], out)
        e.wait_for(lambda: e.state(res2["run_id"], "esx01").get("state") == "done", seconds=240, what="새 실행 완료")
        self.assertIn("== run %s  esx esx01 : done" % res2["run_id"], e.tcrun("status")[1])
        self.assertIn("== run %s  esx esx01 : cancelled" % RID, e.tcrun("status", "-a")[1])

    def test_n_check_reports_and_changes_nothing(self):
        """점검: ESX 키 접속, 도구, VM 찾기, 현재 값과 목표 값, VM 접속, 시계 차이를 보이고 아무것도 바꾸지 않는다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_vm("lost01")
        e.add_esx("esx02", vms=[("esx02ev01", {})], authorized=False)
        open(os.path.join(e.vm_dir("lost01"), "down"), "w").close()
        cases = [case("n1", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_a", [], ["same"])],
                      esx_profiles=["numa0"], esx_monitors=["hmon"]),
                 case("n2", [vm("esx02ev01", "job_a", [], ["pin"])], esx="esx02"),
                 case("n3", [vm("lost01", "job_a")], esx="")]
        res, plan = self.gen(cases)
        before = (e.vmx("esx01", "esx01ev01"), e.vm_crontab("esx01ev01"), e.esx_crontab("esx01"))
        rc, out = e.tcrun("check", plan)
        for word in ("Nothing is changed by this check.", "[ok] folder %s: 1 MB can be written" % e.log, "== ESX host esx01",
                     "[ok] esx esx01 (esx01): ssh key login works, host name esx01", "[ok] esx esx01: python3 found",
                     "[ok] esx esx01: vim-cmd found", "[ok] esx esx01: esxcli found",
                     "TCGEN-VM esx01ev01 vmid=10 power=on vmx=", 'TCGEN-SET esx01ev01 sched.mem.pin: (none) -> "TRUE"  [change]',
                     'TCGEN-SET esx01ev02 numa.nodeAffinity: "0" -> "0"  [same]',
                     "TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 0  [change]",
                     "[ok] settings and VM power: 3 value(s) differ. The run changes them before the job",
                     "TCGEN-POWER esx01ev01 now=on want=on  [same]",
                     "[ok] vm esx01ev01 (esx01ev01): ssh login works, clock", "tcgen lines in crontab: 0",
                     "== ESX host esx02", "[PROBLEM] esx esx02 (esx02): ssh key login failed (exit 255: root@esx02: Permission denied",
                     "sh %s/tcrun key" % e.tcgen, "[PROBLEM] settings and VM power of case n2 not checked (no ESX login)",
                     "== ESX host noesx", "(cases without an ESX host: no ESX login. The power of their VMs is not handled",
                     "[PROBLEM] vm lost01 (lost01): ssh login failed (exit 255: ssh: connect to host lost01 port 22: Connection refused)",
                     "CHECK RESULT: 3 problem(s). Fix them before block S.",
                     "saved: %s/tcrun-%s-check.log" % (e.log, RID)):
            self.assertIn(word, out)
        self.assertEqual((e.vmx("esx01", "esx01ev01"), e.vm_crontab("esx01ev01"), e.esx_crontab("esx01")), before)
        self.assertEqual([x for x in e.esx_calls("esx01") if any(w in x for w in ("power.shutdown", "power.o", "reload", " set "))], [])
        with open(os.path.join(e.log, "tcrun-%s-check.log" % RID)) as f:
            self.assertIn("CHECK RESULT: 3 problem(s)", f.read())
        self.assertEqual(e.state(RID, "esx01"), {}, "점검은 진행을 시작하지 않음")
        # 문제가 없으면 OK
        open(os.path.join(e.esx_dir("esx02"), "authorized"), "w").close()
        os.remove(os.path.join(e.vm_dir("lost01"), "down"))
        rc, out = e.tcrun("check", plan)
        self.assertIn("CHECK RESULT: OK", out)
        self.assertNotIn("[PROBLEM]", out)

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
        self.assertIn("usage: sh %s/tcrun check <plan> | start <plan> | run <plan> <esx> | status [-a] | stop [esx] | cancel [esx] | "
                      "key | version" % e.tcgen, out)
        self.assertEqual(e.tcrun("status")[1].strip(), "no run on this jump VM yet")
        rc, out = e.tcrun("run", os.path.join(e.root, "nofile"), "esx01")
        self.assertEqual(rc, 2)

    def test_esxssh_passes_stdin_and_uses_the_key(self):
        e = self.e
        self.two_vms()
        import subprocess
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh"), "esx01", "sh", "-s"], input=b"hostname; echo $TCGEN_TMP\n",
                           env=e.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.stdout.decode().split(), ["esx01", os.path.join(e.esx_dir("esx01"), "tmp")])
        os.remove(os.path.join(e.tcgen, "id_esx"))
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh"), "esx01", "hostname"], env=e.env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.returncode, 255, p.stdout)
        p = subprocess.run(["sh", os.path.join(e.tcgen, "esxssh")], env=e.env, stdout=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 2)


@unittest.skipUnless(fakeenv.HAVE and fakeenv.shutil.which("dash"), "dash 필요")
class CancelAndStatusTests(TcrunBase):
    def test_cancel_while_the_job_runs(self):
        """cancel: 진행을 바로 끝내고 crontab 줄을 지우고, 시작된 모니터링에는 중지 명령을 실행한다. 잡은 끝내지 않고 보인다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("x1", [vm("esx01ev01", "job_long", ["gmon"])], esx_monitors=["hmon"]),
                              case("x2", [vm("esx01ev01", "job_a")], seq_start="13:00")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "the process is seen on every VM" in self.log(), seconds=240, what="잡 실행 중")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "모니터링 시작 줄과 잡 줄")
        rc, out = e.tcrun("cancel")
        self.assertEqual(rc, 0, out)
        p.wait(timeout=60)
        self.assertIn("== run %s esx esx01: cancelling (state running, case x1, step wait-job)" % RID, out)
        self.assertIn("the runner (pid %d) was ended at step wait-job" % p.pid, out)
        self.assertIn("crontab lines of case x1 removed", out)
        self.assertIn("monitoring stop: vm esx01ev01 (exit 0)", out)
        self.assertIn("monitoring stop: esx esx01 (exit 0)", out)
        self.assertIn("job on vm esx01ev01: 1 process(es) match 'simLong'. tcrun does not end jobs.", out)
        self.assertIn("cancelled. Settings that this run applied stay as they are.", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [], "VM 의 줄을 모두 지움")
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines(), "ESX 의 줄도 지움")
        self.assertEqual([x[:2] for x in e.vm_file("esx01ev01", "mon.log")], [["start", "g"], ["stop", "g"]],
                         "VM 모니터링은 중지 명령을 한 번 실행")
        self.assertTrue(os.listdir(os.path.join(e.vm_dir("esx01ev01"), "proc")), "잡은 끝내지 않음")
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual(self.results()[-1]["status"], "cancelled")
        self.assertNotIn(int(epoch("2026-11-20 13:00:00")), self.minutes(), "쓰지 않은 시작 시각 기록을 돌려줌")
        self.assertIn("== run %s  esx esx01 : cancelled" % RID, e.tcrun("status")[1])
        self.assertEqual(e.tcrun("cancel")[1].strip(), "nothing to cancel: no run is running or queued, and no case is open")

    def test_cancel_before_the_job_start(self):
        """잡 시작 전 cancel: 줄을 지워 모니터링과 잡이 시작되지 않는다. 시작 전이므로 중지 명령은 실행하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("y1", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="10:12")])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "times: monitoring start" in self.log(), seconds=240, what="등록")
        rc, out = e.tcrun("cancel", "esx01")
        p.wait(timeout=60)
        self.assertNotIn("monitoring stop:", out)
        self.assertIn("job on vm esx01ev01: 0 process(es) match 'simA'", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.minutes(), {}, "등록했던 시작 시각도 아직 오지 않았으므로 돌려줌")
        time.sleep(max(0, epoch("2026-11-20 10:13:00") - e.now()) / e.speed)
        self.assertEqual(cron.fired, [], "10:11 모니터링, 10:12 잡 모두 시작되지 않음")
        self.assertEqual(e.state(RID, "esx01")["state"], "cancelled")

    def minutes(self):
        return MultiHostAndCheckTests.minutes(self)

    def test_status_shows_every_host_of_the_run(self):
        """status 는 실행의 모든 ESX 호스트를 보인다. 멈춘 호스트는 이유를, 상태 파일이 없는 호스트는 시작하지 못했다고 보인다.
        stop 은 실행 중인 호스트에만 적용하고 나머지는 상태를 알린다."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {"state": "off", "poweron": "fail"})])
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_long")]), case("s2", [vm("esx02ev01", "job_a")], esx="esx02")])
        with open(plan) as f:
            text = f.read()
        with open(plan, "w") as f:      # esx03: 진행이 시작하자마자 끝나 상태 파일을 쓰지 못하는 호스트(주소가 잘못됨)
            f.write(text.replace("CHAIN esx02\n", "CHAIN esx02\nADDR esx03 -bad\nCHAIN esx03\n").replace(
                "END 002\n", "END 002\nCASE 003 esx03 s3\nMAXWAIT 003 0\nVM 003 esx03ev01 esx03ev01\nEND 003\n"))
        e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("==== esx03: the runner did not start:", out)
        e.wait_for(lambda: e.state(RID, "esx02").get("state") == "paused", seconds=240, what="esx02 멈춤")
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("== run %s  esx esx02 : paused" % RID, out)
        self.assertIn("power on of vm esx02ev01 failed", out)
        self.assertIn("== run %s  esx esx03 : not started (no state file." % RID, out)
        self.assertIn("tcrun: bad ESX address in the plan for esx03", out)
        rc, out = e.tcrun("stop")
        self.assertIn("stop requested: run %s esx esx01" % RID, out)
        self.assertIn("run %s esx esx02: not running (paused: power on of vm esx02ev01 failed" % RID, out)
        self.assertIn("run %s esx esx03: not running (not started)" % RID, out)
        e.wait_for(lambda: e.state(RID, "esx01").get("state") == "stopped", seconds=240, what="esx01 멈춤")

    def test_refused_login_is_not_tried_again(self):
        """VM 을 켠 뒤 ssh 로그인이 거부되면(Permission denied) 다시 시도하지 않는다(계정 잠금 방지). 로그인 배너는 출력하지 않는다."""
        e = self.e
        with open(os.path.join(e.root, "banner"), "w") as f:
            f.write("Access is only available to authorized users. Activity is monitored.\n")
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "boot_sec": 0})])
        open(os.path.join(e.vm_dir("esx01ev01"), "deny"), "w").close()
        res, plan = self.gen([case("r1", [vm("esx01ev01", "job_a")])])
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 1, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertIn("vm esx01ev01: ssh login was refused (esx01ev01: Permission denied (publickey,password).). Not tried again", st["text"])
        with open(os.path.join(e.root, "ssh_calls.txt")) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls.count("deny esx01ev01"), 1, "한 번만 시도")
        self.assertNotIn("Access is only available", self.log(), "LogLevel=ERROR 라서 배너가 출력에 섞이지 않음")

    def test_check_shows_the_ssh_error_line(self):
        """ssh 가 실패하면 출력의 첫 줄이 아니라 ssh 의 오류 줄을 보인다."""
        e = self.e
        self.two_vms()
        open(os.path.join(e.vm_dir("esx01ev02"), "down"), "w").close()
        res, plan = self.gen([case("b1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")])])
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] vm esx01ev02 (esx01ev02): ssh login failed (exit 255: ssh: connect to host esx01ev02 port 22: "
                      "Connection refused)", out)

    def test_folders_that_cannot_be_written(self):
        """작업 폴더나 로그 폴더에 1 MB 를 쓸 수 없으면(디스크, quota) start 는 시작하지 않고 check 는 문제로 알린다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("w1", [vm("esx01ev01", "job_a")])])
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


class NoCheckTests(TcrunBase):
    """시작 시각을 적은 케이스(시각, +간격)는 잡을 확인하지 않는다 (사용자 요청 2026-11-17 "시작 시간 내가 지정하는거면 실행 확인
    작업을 안하면 좋겠어", "체크할 때 부하가 꽤 되는거같음")."""

    def vm_logins(self):
        p = os.path.join(self.e.root, "ssh_calls.txt")
        if not os.path.exists(p):
            return 0
        with open(p) as f:
            return len([x for x in f.read().splitlines() if x.startswith("vm ")])

    def test_cases_with_a_start_time_are_not_checked(self):
        """두 케이스 모두 시작 시각을 적으면: 잡이 실행되는 동안 VM 에 접속하지 않고, 모니터링 중지는 시작 줄과 함께 등록하고,
        다음 케이스 준비 2분 전(마지막 케이스는 잡 시작 + 최대 대기)에 케이스를 끝낸다."""
        e = self.e
        self.two_vms()
        c1 = case("t1", [vm("esx01ev01", "job_a", ["gmon"]), vm("esx01ev02", "job_a")], esx_monitors=["hmon"], seq_start="10:12")
        c2 = case("t2", [vm("esx01ev01", "job_a", ["gmon"])], seq_start="+0:16", max_wait="0:05")
        res, plan = self.gen([c1, c2], seq_prep_lead_min=6)
        self.assertIn("NOCHK 001 %d 2026-11-20 10:22\n" % int(epoch("2026-11-20 10:22:00")), res["plan"])
        self.assertIn("NOCHK 002 0 -\n", res["plan"])
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: "The job of case t1 is not checked" in self.log(), seconds=300, what="t1 잡 시작")
        n0, seen = self.vm_logins(), []
        while "end of case t1" not in self.log():
            seen.append(self.vm_logins())
            time.sleep(0.05)
            self.assertIsNone(p.poll(), self.ctx())
        self.assertTrue(len(seen) > 10 and set(seen[:-1]) == {n0}, "잡 시작 뒤 케이스가 끝날 때까지 VM 접속이 없음: %s, %s" % (n0, sorted(set(seen))))
        self.assertEqual(p.wait(timeout=300), 0, self.ctx())
        log = self.log()
        self.assertNotRegex(log, r" check \d+ \(case ", "잡 확인을 하지 않음")
        self.assert_order(log, [
            "registered on vm esx01ev01:", "mon.sh start g # tcgen t1 esx01ev01", "job.sh simA 100 # tcgen t1 esx01ev01",
            "20 10 20 11 * sh %s/mon.sh stop g # tcgen t1 esx01ev01" % e.bin, "registered on esx esx01 (UTC):",
            "20 01 20 11 * sh %s/mon.sh stop h # tcgen t1 esx01" % e.bin,
            "times: monitoring start 2026-11-20 10:11:00, job start 2026-11-20 10:12:00",
            "not checked: the plan sets the start time of this case. End of the case: 2026-11-20 10:20:00",
            "the job start time 2026-11-20 10:12:00 has passed. The job of case t1 is not checked",
            "end of case t1: 2026-11-20 10:20:00", "removed the lines of this case: vm esx01ev01",
            "==== case t1 done (not checked). job start 2026-11-20 10:12:00",
            "start time 2026-11-20 10:28: the preparation (settings, VM power) starts 6 min before it, at 2026-11-20 10:22:00",
            "not checked: the plan sets the start time of this case. End of the case: 2026-11-20 10:33:00",
            "end of case t2: 2026-11-20 10:33:00", "run finished: all 2 case(s) done"])
        a, b = self.results()
        self.assertEqual([(r["case"], r["status"], r["job_start"], r["monitor_stop"], r["first_seen"], r["job_end"]) for r in (a, b)],
                         [("t1", "done (not checked)", "2026-11-20 10:12:00", "2026-11-20 10:20:00", "", ""),
                          ("t2", "done (not checked)", "2026-11-20 10:28:00", "2026-11-20 10:33:00", "", "")])
        for host, word, when in (("esx01ev01", "mon.sh start g # tcgen t1", "10:11"), ("esx01", "mon.sh start h # tcgen t1", "10:11"),
                                 ("esx01ev01", "job.sh simA 100 # tcgen t1", "10:12"), ("esx01ev02", "job.sh simA 100 # tcgen t1", "10:12"),
                                 ("esx01ev01", "mon.sh stop g # tcgen t1", "10:20"), ("esx01", "mon.sh stop h # tcgen t1", "10:20"),
                                 ("esx01ev01", "mon.sh start g # tcgen t2", "10:27"), ("esx01ev01", "job.sh simA 100 # tcgen t2", "10:28"),
                                 ("esx01ev01", "mon.sh stop g # tcgen t2", "10:33")):
            self.assertEqual(self.fired(cron, host, word), [epoch("2026-11-20 %s:00" % when)], (host, word))
        self.assertEqual(len(cron.fired), 9, cron.fired)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        raw, rep = self.report()
        self.assertEqual([r[1:2] + r[4:6] for r in rep[1:]], [["t1", "2026-11-20 10:12:00", ""], ["t2", "2026-11-20 10:28:00", ""]],
                         "리포트용 CSV 의 잡 종료 시각은 비움(성능 리포트 생성기가 정함)")
        self.assertEqual(e.state(RID, "esx01")["text"], "all 2 case(s) finished")

    def test_last_case_without_an_end_time_stays_open(self):
        """마지막 케이스에 최대 대기가 없으면 끝나는 시각이 없다: 잡 시작 뒤 실행은 done 으로 끝나고 케이스는 열린 채로 남는다.
        tcrun cancel 이 닫는다(모니터링 중지, 줄 삭제). 같은 ESX 호스트의 다음 실행은 첫 케이스가 자동이면 시작하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_long", ["gmon"])], esx_monitors=["hmon"], seq_start="10:10")], seq_prep_lead_min=5)
        self.assertIn("NOCHK 001 0 -\n", res["plan"])
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        log = self.log()
        self.assertEqual(rc, 0, self.ctx(out))
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("Case o1 stays open (not checked, no end time): its monitoring is not stopped.", st["text"])
        self.assertIn("case o1 stays open: it has no end time (last case of this ESX host, no maximum wait)", log)
        self.assertEqual(self.results()[0]["status"], "done (not checked, open)")
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2, "모니터링 시작 줄과 잡 줄이 남음(중지 줄은 등록하지 않음)")
        self.assertEqual(self.fired(cron, "esx01ev01", "mon.sh stop"), [])
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : done" % RID, out)
        self.assertIn("open case: o1. Its crontab lines are on the hosts and its monitoring may run. Close it: sh %s/tcrun cancel esx01" % e.tcgen, out)
        # 첫 케이스가 자동인 다음 실행: 앞 잡이 언제 끝나는지 알 수 없으므로 시작하지 않는다
        res2, plan2 = self.gen([case("o2", [vm("esx01ev01", "job_a")])], now=GEN + dt.timedelta(minutes=20))
        rc, out = e.tcrun("start", plan2)
        self.assertIn("==== esx01: not started. case o1 of run %s is still open on this ESX host (it was not checked and has no end time), "
                      "and the first case of this run has no start time: tcrun cannot tell when that job ends." % RID, out)
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[PROBLEM] esx esx01: block S would not start this run. case o1 of run %s is still open" % RID, out)
        self.assertIn("[ok] vm esx01ev01: not logged in to by this check (run %s, see above)" % RID, out)
        self.assertEqual(e.state(res2["run_id"], "esx01"), {})
        # cancel 이 열린 케이스를 닫는다
        n = len(e.vm_file("esx01ev01", "mon.log"))
        rc, out = e.tcrun("cancel")
        self.assertIn("== run %s esx esx01: closing the case that this run left open (o1)" % RID, out)
        self.assertIn("monitoring stop: vm esx01ev01 (exit 0)", out)
        self.assertIn("monitoring stop: esx esx01 (exit 0)", out)
        self.assertIn("job on vm esx01ev01: 1 process(es) match 'simLong'. tcrun does not end jobs.", out)
        self.assertIn("closed. The run stays done.", out)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual([x[:2] for x in e.vm_file("esx01ev01", "mon.log")][n:], [["stop", "g"]])
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("Case o1 was closed", st["text"])
        self.assertEqual(self.results()[-1]["status"], "closed by sh tcrun cancel")
        self.assertEqual(e.tcrun("cancel")[1].strip(), "nothing to cancel: no run is running or queued, and no case is open")
        self.assertNotIn("open case:", e.tcrun("status")[1])
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % res2["run_id"], out)
        e.tcrun("cancel")

    def test_next_run_takes_over_an_open_case(self):
        """끝나는 시각이 없어 열린 채로 남은 케이스는, 같은 ESX 호스트의 다음 실행이 첫 케이스(시작 시각 지정)를 준비하기 직전에 닫는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("o1", [vm("esx01ev01", "job_a", ["gmon"])], esx_monitors=["hmon"], seq_start="10:08")], seq_prep_lead_min=4)
        cron = e.start_cron()
        rc, out = e.tcrun("run", plan, "esx01", timeout=600)
        self.assertEqual(rc, 0, self.ctx(out))
        self.assertEqual(len(self.tc_lines("esx01ev01")), 2)
        res2, plan2 = self.gen([case("n1", [vm("esx01ev01", "job_a")], seq_start="10:24", max_wait="0:03")],
                               now=GEN + dt.timedelta(minutes=9), seq_prep_lead_min=4)
        rid2 = res2["run_id"]
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[ok] esx esx01: case o1 of run %s is still open (not checked, no end time). This run closes it right before its first "
                      "case is prepared." % RID, out)
        self.assertIn("CHECK RESULT: OK", out)
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % rid2, out)
        e.wait_for(lambda: e.state(rid2, "esx01").get("state") == "done", seconds=600, what="다음 실행 완료")
        log2 = self.log(rid=rid2)
        self.assert_order(log2, [
            "run(s) %s left a case open on this ESX host (not checked, no end time). It is closed right before the first case of this run is prepared" % RID,
            "start time 2026-11-20 10:24: the preparation (settings, VM power) starts 4 min before it, at 2026-11-20 10:20:00",
            "run %s left its last case open on this ESX host (not checked, no end time). Closing it now" % RID,
            "removed the lines of this case: vm esx01ev01", "monitoring stop commands run on vm esx01ev01 (exit 0)",
            "monitoring stop commands run on esx esx01 (exit 0)", "case o1 of run %s is closed" % RID,
            "VM power:", "registered on vm esx01ev01:", "==== case n1 done (not checked)"])
        closed = next(x for x in log2.splitlines() if "case o1 of run %s is closed" % RID in x)
        self.assertGreaterEqual(epoch(closed[:19]), epoch("2026-11-20 10:20:00"), "다음 실행의 준비 시작(10:20) 때 닫음")
        self.assertLess(epoch(closed[:19]), epoch("2026-11-20 10:22:00"))
        mon = e.vm_file("esx01ev01", "mon.log")
        self.assertEqual([x[:2] for x in mon], [["start", "g"], ["stop", "g"]], "앞 실행의 모니터링을 중지 명령으로 끝냄")
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "done")
        self.assertIn("Case o1 was closed", st["text"])
        self.assertIn("by run %s before case n1 was prepared" % rid2, st["text"])
        self.assertEqual(self.results()[-1]["status"], "closed by run %s before case n1 was prepared" % rid2)
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertNotIn("open case:", e.tcrun("status", "-a")[1])


class QueueTests(TcrunBase):
    """같은 ESX 호스트의 실행은 한 번에 하나만 진행한다. 진행 중에 붙여넣은 실행은 대기했다가 앞 실행이 done 으로 끝나면 시작한다
    (사용자 보고 2026-11-17 "처음 tcrun 으로 테스트 수행했다가 추가로 실험 계획 추가하거나 할때 잘안되는거같아")."""

    def test_second_run_waits_in_the_queue_and_starts_by_itself(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("q1", [vm("esx01ev01", "job_b", ["gmon"])])])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("started: run %s esx esx01" % RID, out)
        e.wait_for(lambda: "the process is seen on every VM" in self.log(), seconds=300, what="q1 잡 실행 중")
        res2, plan2 = self.gen([case("q2", [vm("esx01ev01", "job_a")])], now=GEN + dt.timedelta(minutes=3))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("check", plan2)
        self.assertIn("[ok] esx esx01: run %s still works for this ESX host. Block S puts this run in the queue: it starts when that run has "
                      "ended as done. This check does not log in to the VMs of this ESX host (a job may run)" % RID, out)
        self.assertIn("[ok] vm esx01ev01: not logged in to by this check (run %s, see above). The run looks at it when it starts." % RID, out)
        self.assertIn("CHECK RESULT: OK", out)
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01   waiting for run %s of this ESX host to end. It starts when that has ended as done" % (rid2, RID), out)
        rc, out = e.tcrun("status")
        self.assertIn("== run %s  esx esx01 : running" % RID, out)
        self.assertIn("== run %s  esx esx01 : queued" % rid2, out)
        self.assertEqual(self.tc_lines("esx01ev01")[-1].split(" # ")[1], "tcgen q1 esx01ev01", "대기 중에는 아무것도 등록하지 않음")
        e.wait_for(lambda: e.state(rid2, "esx01").get("state") == "done", seconds=600, what="대기하던 실행 완료")
        self.assertEqual(e.state(RID, "esx01")["state"], "done")
        log2 = self.log(rid=rid2)
        self.assert_order(log2, ["queued: run(s) %s still work for this ESX host. This run starts when they have ended as done" % RID,
                                 "queued: run %s ended as done" % RID, "==== case q2 (1 of 1) begin", "==== case q2 done"])
        a, b = self.results()[0], self.results(rid=rid2)[0]
        self.assertGreaterEqual(epoch(b["begin"]), epoch(a["end"]), "앞 실행이 끝난 뒤에 시작")
        self.assertEqual((a["status"], b["status"]), ("done", "done"))
        self.assertEqual(len(self.fired(cron, "esx01ev01", "job.sh")), 2)

    def test_queued_run_does_not_start_after_a_paused_run_and_cancel_opens_the_host(self):
        """앞 실행이 done 이 아닌 상태로 끝나면 대기하던 실행은 시작하지 않는다. 앞 실행이 줄을 남긴 채 멈췄으면(열린 케이스)
        cancel 로 닫기 전에는 그 ESX 호스트의 새 실행을 시작하지 않는다."""
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("f1", [vm("esx01ev01", "job_wrong", ["gmon"])])])      # 프로세스 이름이 맞지 않아 멈추는 잡
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        e.wait_for(lambda: "times: monitoring start" in self.log(), seconds=300, what="f1 등록")
        res2, plan2 = self.gen([case("f2", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(minutes=2))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        e.wait_for(lambda: e.state(rid2, "esx01").get("state") == "paused", seconds=600, what="대기하던 실행이 멈춤")
        self.assertEqual(e.state(RID, "esx01")["state"], "paused")
        st2 = e.state(rid2, "esx01")
        self.assertIn("run %s of this ESX host ended as paused, not as done. Nothing of this run was started." % RID, st2["text"])
        self.assertIn("run ended (paused) before its first case. Not finished: f2", self.log(rid=rid2))
        self.assertEqual(self.tc_lines("esx01ev02"), [])
        self.assertEqual(self.results(rid=rid2), [], "시작하지 않은 실행은 결과 행이 없음")
        # 멈춘 실행의 케이스가 열려 있음: 새 실행은 시작하지 않고, 점검도 문제로 알린다
        res3, plan3 = self.gen([case("f3", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(minutes=30))
        rid3 = res3["run_id"]
        rc, out = e.tcrun("start", plan3)
        self.assertIn("==== esx01: not started. run %s of this ESX host ended as paused and left case f1 open (crontab lines on the hosts, "
                      "its monitoring may run). Close it first: sh %s/tcrun cancel esx01" % (RID, e.tcgen), out)
        rc, out = e.tcrun("check", plan3)
        self.assertIn("[PROBLEM] esx esx01: block S would not start this run. run %s of this ESX host ended as paused and left case f1 open" % RID, out)
        self.assertRegex(out, r"CHECK RESULT: 1 problem")
        rc, out = e.tcrun("cancel")
        self.assertIn("== run %s esx esx01: cancelling (state paused, case f1, step wait-job)" % RID, out)
        self.assertIn("monitoring stop: vm esx01ev01 (exit 0)", out)
        self.assertNotIn(rid2, out, "시작하지 않은 실행에는 닫을 것이 없음")
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        rc, out = e.tcrun("start", plan3)
        self.assertIn("started: run %s esx esx01" % rid3, out, "닫은 뒤에는 같은 S 블록으로 시작")
        e.wait_for(lambda: e.state(rid3, "esx01").get("state") == "done", seconds=600, what="새 실행 완료")

    def test_cancel_ends_every_run_newest_first(self):
        """cancel 은 진행 중이거나 대기 중인 실행을 모두 끝낸다(대기 중인 실행부터). 호스트를 주면 그 호스트만."""
        e = self.e
        self.two_vms()
        e.add_vm("esx02ev01")
        e.add_esx("esx02", vms=[("esx02ev01", {})])
        res, plan = self.gen([case("c1", [vm("esx01ev01", "job_long", ["gmon"])]), case("d1", [vm("esx02ev01", "job_long")], esx="esx02")])
        cron = e.start_cron()
        rc, out = e.tcrun("start", plan)
        e.wait_for(lambda: all("the process is seen on every VM" in self.log(h) for h in ("esx01", "esx02")), seconds=300, what="두 호스트 잡 실행 중")
        res2, plan2 = self.gen([case("c2", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(minutes=10))
        rid2 = res2["run_id"]
        rc, out = e.tcrun("start", plan2)
        self.assertIn("queued:  run %s esx esx01" % rid2, out)
        rc, out = e.tcrun("cancel", "esx02")
        self.assertIn("== run %s esx esx02: cancelling (state running, case d1, step wait-job)" % RID, out)
        self.assertNotIn("esx esx01", out, "다른 호스트는 건드리지 않음")
        self.assertEqual(e.state(RID, "esx02")["state"], "cancelled")
        self.assertEqual(e.state(RID, "esx01")["state"], "running")
        self.assertEqual(e.state(rid2, "esx01")["state"], "queued")
        rc, out = e.tcrun("cancel")
        self.assertLess(out.index("== run %s esx esx01: cancelling (state queued" % rid2),
                        out.index("== run %s esx esx01: cancelling (state running, case c1" % RID), "대기 중인 실행을 먼저 끝냄")
        self.assertEqual((e.state(RID, "esx01")["state"], e.state(rid2, "esx01")["state"]), ("cancelled", "cancelled"))
        self.assertEqual(self.tc_lines("esx01ev01"), [])
        self.assertEqual(self.tc_lines("esx01ev02"), [], "대기하던 실행은 시작된 적이 없음")
        self.assertEqual([x[:2] for x in e.vm_file("esx01ev01", "mon.log")], [["start", "g"], ["stop", "g"]])
        time.sleep(120 / e.speed)
        self.assertEqual(e.state(rid2, "esx01")["state"], "cancelled", "취소한 뒤에 대기하던 실행이 시작되지 않음")
        self.assertEqual(e.tcrun("cancel")[1].strip(), "nothing to cancel: no run is running or queued, and no case is open")


class StopDuringPowerTests(TcrunBase):
    """stop 은 VM 을 켜고 기다리는 동안에도 바로 적용된다 (사용자 보고 2026-11-17 "tcrun stop 으로 stop 도 안되는거같고")."""

    def test_stop_while_waiting_for_the_ssh_login(self):
        e = self.e
        e.add_vm("esx01ev01")
        e.add_vm("esx01ev02")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "boot_sec": 1200}), ("esx01ev02", {"state": "off"})])
        res, plan = self.gen([case("s1", [vm("esx01ev01", "job_a"), vm("esx01ev02", "job_a")], esx_profiles=["numa0"])], seq_boot_wait_min=30)
        cron = e.start_cron()
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: e.state(RID, "esx01").get("text", "").startswith("vm esx01ev01 is powered on. waiting for ssh login"), seconds=300,
                   what="ssh 로그인 대기")
        t0 = e.now()
        rc, out = e.tcrun("stop")
        self.assertIn("stop requested: run %s esx esx01" % RID, out)
        self.assertEqual(p.wait(timeout=60), 1, self.ctx())
        self.assertLess(e.now() - t0, 300, "부팅 대기 한도(30분)를 기다리지 않고 멈춤")
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "stopped")
        self.assertIn("while the VMs of case s1 were being powered (stop requested while tcrun waited for the ssh login of vm esx01ev01. "
                      "The VM stays powered on). The settings of the case stay applied. VMs that were not powered on yet stay off.", st["text"])
        self.assertNotIn("rollback:", self.log(), "stop 은 설정을 되돌리지 않음")
        self.assertEqual(e.esx_adv("esx01", "/Numa/LocalityWeightActionAffinity"), "0")
        vms = e.esx_vms("esx01")
        self.assertEqual((vms["esx01ev01"]["state"], vms["esx01ev02"]["state"]), ("on", "off"))
        self.assertEqual(cron.fired, [])

    def test_stop_while_the_power_on_task_runs(self):
        e = self.e
        e.add_vm("esx01ev01")
        e.add_esx("esx01", vms=[("esx01ev01", {"state": "off", "poweron": "slow", "poweron_sec": 25})])
        res, plan = self.gen([case("s2", [vm("esx01ev01", "job_a")])])
        p = e.tcrun_bg("run", plan, "esx01")
        e.wait_for(lambda: e.state(RID, "esx01").get("text") == "power on esx01ev01", seconds=300, what="켜기 작업 중")
        time.sleep(1.0)
        t0 = time.time()
        e.tcrun("stop")
        self.assertEqual(p.wait(timeout=60), 1, self.ctx())
        self.assertLess(time.time() - t0, 15, "켜기 작업(25초)이 끝나기를 기다리지 않고 멈춤")
        st = e.state(RID, "esx01")
        self.assertEqual(st["state"], "stopped")
        self.assertIn("stop requested while vm esx01ev01 was being powered on. Its power-on task goes on to its end on the ESX host", st["text"])


class WorkFolderTests(TcrunBase):
    JDIR = True

    def test_work_folder_outside_the_home(self):
        """작업 폴더를 홈 밖에 두면 진행 스크립트, 키, 비밀번호, 실행 기록, known_hosts 가 모두 그 폴더에 있고 홈에는 쓰지 않는다."""
        e = self.e
        self.assertTrue(e.tcgen.endswith("/data/tcgen"))
        self.two_vms()
        res, plan = self.gen([case("f1", [vm("esx01ev01", "job_a")], esx_profiles=["numa0"])])
        self.assertTrue(plan.startswith(e.tcgen + "/runs/"))
        e.start_cron()
        rc, out = e.tcrun("start", plan)
        self.assertIn("status: sh %s/tcrun status" % e.tcgen, out)
        e.wait_for(lambda: e.state(RID, "esx01").get("state") == "done", seconds=240, what="완료")
        self.assertFalse(os.path.exists(os.path.join(e.jump, ".tcgen")), "홈의 ~/.tcgen 을 쓰지 않음")
        self.assertTrue(os.path.isdir(os.path.join(e.tcgen, "minutes")))
        self.assertIn("== run %s  esx esx01 : done" % RID, e.tcrun("status")[1])


class DashTests(TcrunBase):
    """모든 sh 호출을 dash 로 바꿔 실행한다: tcrun 자신, VM 에 보내는 글, ESX 에 보내는 글, cron 이 실행하는 줄."""
    DASH = True

    def test_flow_with_settings_and_monitoring_in_dash(self):
        e = self.e
        self.two_vms()
        c1 = case("dashA", [vm("esx01ev01", "job_a", ["gmon"], ["pin"]), vm("esx01ev02", "job_b")],
                  esx_monitors=["hmon"], esx_profiles=["numa0"])
        res, plan = self.gen([c1, case("dashB", [vm("esx01ev02", "job_a")])], seq_settle_min=3)
        cron = e.start_cron()
        self.assertEqual(os.path.realpath(fakeenv.shutil.which("sh", path=e.env["PATH"])), os.path.realpath(fakeenv.shutil.which("dash")))
        rc, out = e.tcrun("check", plan)
        self.assertIn("CHECK RESULT: OK", out)
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 0, self.ctx(out))
        rows = self.results()
        self.assertEqual([(r["case"], r["apply"], r["status"]) for r in rows], [("dashA", "changed", "done"), ("dashB", "none", "done")])
        self.assertGreaterEqual(epoch(rows[0]["job_start"]) - epoch(rows[0]["vms_on"]), 180)
        self.assertEqual(len(cron.fired), 7)
        self.assertEqual(self.tc_lines("esx01ev01") + self.tc_lines("esx01ev02"), [])
        self.assertEqual(e.esx_crontab("esx01"), fakeenv.ESX_DEFAULT_CRON.splitlines())
        self.assertTrue(e.vmx("esx01", "esx01ev01").endswith('sched.mem.pin = "TRUE"\n'))
        self.assertTrue(self.report()[0].startswith("﻿"))

    def test_stop_and_pause_in_dash(self):
        e = self.e
        self.two_vms(vm2={"poweron": "fail"})
        res, plan = self.gen([case("dashE", [vm("esx01ev01", "job_a", [], ["pin"]), vm("esx01ev02", "job_a")])])
        rc, out = e.tcrun("run", plan, "esx01")
        self.assertEqual(rc, 1, self.ctx(out))
        self.assertEqual(e.state(RID, "esx01")["state"], "paused")
        self.assertIn("rollback: rolledback 1 VM(s), 0 host option(s)", self.log())


if __name__ == "__main__":
    unittest.main()
