# -*- coding: utf-8 -*-
"""
tcrun(점프 VM 진행 스크립트)과 tcgen_apply.py 의 통합 시험.

가짜 환경(fakeenv.py): 가짜 ssh(VM 별 홈 폴더, ESX 는 키 접속), 가짜 crontab, 등록된 줄을 그 시각에 실행하는 cron 흉내,
가짜 vim-cmd 와 esxcli, 가짜 vmx 폴더, 가상 시계(date 와 sleep 을 가짜 명령으로 바꿔 시간이 빨리 감).
tcrun 과 tcgen_apply.py 에는 시험용 코드가 없다. 경로만 환경 변수(TCGEN_TMP 등)로 바꾼다.

경우: (a) 케이스 2개의 전체 흐름 (b) 값이 이미 같음 (d) 게스트 종료 불가 (e) 전원 켜기 실패와 rollback
(f) 프로세스가 보이지 않음 (g) ssh 일시 실패 (h) stop (i) 없는 VM, 없는 호스트 옵션 (j) 판 불일치
(k) ESX 호스트 2대 (l) 설정만 있는 케이스, 그리고 VM 을 다시 켠 뒤 대기, 시계 차이, 점검(check), 등록 실패, 최대 대기.
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


def vm(name, job="(없음)", monitors=(), profiles=()):
    return {"name": name, "job": job, "monitors": list(monitors), "profiles": list(profiles), "end_ref": False}


def case(name, vms, esx="esx01", esx_monitors=(), esx_profiles=(), max_wait="", group="g1", desc="", seq_start=""):
    c = core.new_case(esx, "", 0)
    c.update(name=name, vms=vms, esx_monitors=list(esx_monitors), esx_profiles=list(esx_profiles), max_wait=max_wait,
             group=group, desc=desc, seq_start=seq_start)
    return c


@unittest.skipUnless(fakeenv.HAVE, "perl, sh 가 있는 Linux/macOS 에서만 실행")
class TcrunBase(unittest.TestCase):
    DASH = False

    def setUp(self):
        # 생성기는 시작 시각을 이 PC 의 시간대로 초(epoch)로 바꾼다. 가짜 환경의 시계는 KST 이므로 시험 프로세스도 KST 로 맞춘다
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.e = fakeenv.FakeEnv(dash=self.DASH)
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
            "esx01ev01: powered on", "vm esx01ev01: ssh login works", "esx01ev02: powered on", "vm esx01ev02: ssh login works",
            "settings: done", "settle: VMs of this case were powered on again", "registered on vm esx01ev01:",
            "registered on vm esx01ev02:", "registered on esx esx01 (UTC):", "times: monitoring start",
            "the process is seen on every VM with a job", "job ended:", "monitoring stop registered on vm esx01ev01:",
            "monitoring stop registered on esx esx01 (UTC):", "removed the lines of this case: vm esx01ev01",
            "removed the lines of this case: esx esx01", "==== case caseA done",
            "==== case caseB (2 of 2) begin", "settings: every value is already as wanted. The VMs are not touched.",
            "registered on vm esx01ev01:", "job ended:", "==== case caseB done", "run finished: all 2 case(s) done"])
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
        self.assertEqual(b["status"], "done")

    def test_check_reports_a_start_time_that_has_passed(self):
        e = self.e
        self.two_vms()
        res, plan = self.gen([case("k1", [vm("esx01ev01", "job_a")], seq_start="09:55"),
                              case("k2", [vm("esx01ev01", "job_a")], seq_start="23:00")], now=dt.datetime(2026, 11, 20, 9, 50, 0))
        rc, out = e.tcrun("check", plan)
        self.assertIn("[PROBLEM] case k1: the start time 2026-11-20 09:55 has already passed", out)
        self.assertRegex(out, r"\[ok\] start time: job at 2026-11-20 23:00 \(\d+ min from now\)")
        self.assertRegex(out, r"CHECK RESULT: 1 problem")


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
        self.assertIn("poll: vm esx01ev02 process seen for the first time", log)
        self.assertNotIn("poll: vm esx01ev01 process seen", log)
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
        self.assertIn("poll: vm esx01ev01 unknown (ssh exit 255: ssh: connect to host esx01ev01 port 22: Connection timed out). Not counted as ended.", log)
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
        self.assertIn("esx esx01: ssh key login failed", e.state(res["run_id"], "esx01")["text"])
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
        self.assertEqual(e.tcrun("stop")[1].strip(), "no running tcrun")
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
        # 같은 ESX 호스트의 다른 실행도 시작하지 않는다
        res2, plan2 = self.gen([case("k3", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(seconds=7))
        rc, out3 = e.tcrun("start", plan2)
        self.assertIn("==== esx01: run %s is still running for this ESX host. Not started." % RID, out3)
        self.assertEqual(e.state(res2["run_id"], "esx01"), {})
        e.wait_for(lambda: e.state(RID, "esx02").get("state") == "done", seconds=240, what="esx02 완료")
        self.assertEqual(e.state(RID, "esx01")["state"], "running", "esx02 는 esx01 을 기다리지 않고 끝남")
        self.assertIn("run finished: all 1 case(s) done", self.log("esx02"))
        self.assertIn("esx esx02 (10.0.0.12)", self.log("esx02"))
        self.assertNotIn("k2", self.log("esx01"))
        self.assertEqual([r["case"] for r in self.results("esx02")], ["k2"])
        self.assertEqual(len(self.fired(cron, "esx02", "mon.sh stop h")), 1)
        rc, st = e.tcrun("status")
        self.assertIn("esx esx01 : running", st)
        self.assertIn("esx esx02 : done", st)
        rc, out4 = e.tcrun("stop", "esx01")
        self.assertIn("stop requested: run %s esx esx01" % RID, out4)
        e.wait_for(lambda: e.state(RID, "esx01").get("state") == "stopped", what="esx01 stop")

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
        self.assertEqual(e.tcrun("stop")[1].strip(), "no running tcrun")
        res2, plan2 = self.gen([case("z2", [vm("esx01ev02", "job_a")])], now=GEN + dt.timedelta(seconds=30))
        rc, out = e.tcrun("start", plan2)
        self.assertIn("started: run %s esx esx01" % res2["run_id"], out)
        e.wait_for(lambda: e.state(res2["run_id"], "esx01").get("state") == "done", seconds=240, what="새 실행 완료")
        self.assertIn("== run %s  esx esx01 : done" % res2["run_id"], e.tcrun("status")[1])
        self.assertIn("esx esx01 : died", e.tcrun("status", "-a")[1])

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
        for word in ("Nothing is changed by this check.", "[ok] log folder " + e.log, "== ESX host esx01",
                     "[ok] esx esx01 (esx01): ssh key login works, host name esx01", "[ok] esx esx01: python3 found",
                     "[ok] esx esx01: vim-cmd found", "[ok] esx esx01: esxcli found",
                     "TCGEN-VM esx01ev01 vmid=10 power=on vmx=", 'TCGEN-SET esx01ev01 sched.mem.pin: (none) -> "TRUE"  [change]',
                     'TCGEN-SET esx01ev02 numa.nodeAffinity: "0" -> "0"  [same]',
                     "TCGEN-HOST /Numa/LocalityWeightActionAffinity (integer): 130 -> 0  [change]",
                     "[ok] settings: 3 value(s) differ. The VMs of this case will be shut down and powered on.",
                     "[ok] vm esx01ev01 (esx01ev01): ssh login works, clock", "tcgen lines in crontab: 0",
                     "== ESX host esx02", "[PROBLEM] esx esx02 (esx02): ssh key login failed (exit 255: root@esx02: Permission denied",
                     "sh ~/.tcgen/tcrun key", "[PROBLEM] settings of case n2 not checked (no ESX login)",
                     "== ESX host noesx", "(no settings and no ESX monitoring in the cases of this host: no ESX login needed)",
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
        self.assertIn("sh ~/.tcgen/esxssh <esx host> hostname", out)
        self.assertEqual(e.tcrun("version"), (0, core.VERSION + "\n"))
        rc, out = e.tcrun()
        self.assertEqual(rc, 2)
        self.assertIn("usage: sh ~/.tcgen/tcrun check <plan> | start <plan> | run <plan> <esx> | status [-a] | stop [esx] | key | version", out)
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
