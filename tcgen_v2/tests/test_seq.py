# -*- coding: utf-8 -*-
"""
생성기 core 의 단위 시험(v2.12 의 한 가지 진행 방식): 설정 프로파일 읽기, 검증 오류, 시작 시각과 +간격, plan 생성, 타임라인,
블록의 글자 규칙, v1 데이터 옮겨 오기, 점프 VM 파일 내용. 실행: python -m unittest discover -s tests -v
"""
import ast
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import tcgen_core as core  # noqa: E402
import tcgen_payload as payload  # noqa: E402

NOW = dt.datetime(2026, 11, 16, 14, 30, 0)
RUN = [
    {"app": "AppA", "name": "job_a", "desc": "1 job", "proc": "run_a.sh",
     "cmd": "/home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1"},
    {"app": "AppB", "name": "job_b4", "desc": "4 jobs", "cmd": "/home/tester/bench/run_b.sh -n 4 -tag x%y", "proc": "run_b.sh -n 4"},
]
MON = [
    {"app": "mon", "name": "guest_perf", "desc": "", "where": "VM",
     "start": "/home/tester/mon/start_mon.sh >> /home/tester/log/mon-<vm이름>-<실행시각>.log", "stop": "/home/tester/mon/stop_mon.sh"},
    {"app": "mon", "name": "host_mon", "desc": "", "where": "ESX",
     "start": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start '%H'", "stop": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop"},
    {"app": "mon", "name": "guest_nostop", "desc": "", "where": "VM", "start": "/home/tester/mon/once.sh", "stop": ""},
]
PROF = [
    {"name": "numa0", "target": "ESX", "text": "Numa.LocalityWeightActionAffinity = 0\n/Cpu/HTWholeCoreThreshold = 0", "desc": "호스트"},
    {"name": "pin", "target": "VM", "text": 'sched.mem.pin = "TRUE"\nsched.mem.prealloc = "TRUE"', "desc": ""},
    {"name": "aff", "target": "VM", "text": "sched.cpu.affinity = 0,1,2,3\nSCHED.MEM.PIN = FALSE\nnuma.nodeAffinity =", "desc": ""},
]


def vm(name, job="(없음)", monitors=(), profiles=(), end_ref=False, power=None):
    v = {"name": name, "job": job, "monitors": list(monitors), "profiles": list(profiles), "end_ref": end_ref}
    if power:
        v["power"] = power
    return v


def case(name, esx="esx01", vms=None, esx_monitors=(), esx_profiles=(), max_wait="", applied=False, desc="", group="",
         seq_start="", stop_mode="auto", stop_time=""):
    c = core.new_case(esx, "", 0)
    c.update(name=name, vms=vms if vms is not None else [vm(esx + "ev01", "job_a")], esx_monitors=list(esx_monitors),
             esx_profiles=list(esx_profiles), max_wait=max_wait, applied=applied, desc=desc, group=group, seq_start=seq_start,
             stop_mode=stop_mode, stop_time=stop_time)
    return c


def settings(**over):
    s = dict(core.DEFAULT_SETTINGS)
    s.update(record_csv="/nonexistent/rec.csv", log_dir="/nonexistent")
    s.update(over)
    return s


def with_starts(cases):
    """시작 시각을 적지 않은 잡 케이스에 시각을 넣은 사본: 표의 순서대로 16:00 부터 2시간 간격(v2.12 는 잡 케이스에 시작 시각이
    있어야 한다). 시각을 적은 케이스와 잡이 없는 케이스는 그대로 둔다."""
    out = copy.deepcopy(cases)
    for k, c in enumerate(out):
        has_job = any((v.get("job") or "(없음)") != "(없음)" for v in c.get("vms") or [])
        if has_job and not (c.get("seq_start") or "").strip():
            c["seq_start"] = core.fmt_dt(dt.datetime(2026, 11, 16, 16, 0) + dt.timedelta(hours=2 * k))
    return out


def gen(cases, run=None, mon=None, prof=None, st=None):
    return core.generate(with_starts(cases), copy.deepcopy(run or RUN), copy.deepcopy(mon or MON), st or settings(),
                         now=NOW, write=False, profiles=copy.deepcopy(prof if prof is not None else PROF))


class ProfileTextTests(unittest.TestCase):
    def test_plain_and_vmx_lines(self):
        items, errs = core.parse_profile_text('sched.mem.pin = "TRUE"\nnuma.nodeAffinity=0,1\n  sched.cpu.units  =  "mhz"  \n', "VM")
        self.assertEqual(errs, [])
        self.assertEqual(items, [("sched.mem.pin", "TRUE"), ("numa.nodeAffinity", "0,1"), ("sched.cpu.units", "mhz")])

    def test_blank_comment_and_crlf(self):
        items, errs = core.parse_profile_text("# 설명 줄\r\n\r\nmonitor_control.halt_desched = FALSE\r\n", "VM")
        self.assertEqual((items, errs), ([("monitor_control.halt_desched", "FALSE")], []))

    def test_empty_value_means_delete(self):
        items, errs = core.parse_profile_text('sched.mem.pin =\nnuma.nodeAffinity = ""\n', "VM")
        self.assertEqual((items, errs), ([("sched.mem.pin", ""), ("numa.nodeAffinity", "")], []))

    def test_same_key_twice_last_wins_case_insensitive(self):
        items, errs = core.parse_profile_text("sched.mem.pin = TRUE\nother = 1\nSched.Mem.Pin = FALSE\n", "VM")
        self.assertEqual((items, errs), ([("Sched.Mem.Pin", "FALSE"), ("other", "1")], []))

    def test_value_may_hold_equal_sign_and_spaces(self):
        items, errs = core.parse_profile_text('guestinfo.note = "a = b c"\n', "VM")
        self.assertEqual((items, errs), ([("guestinfo.note", "a = b c")], []))

    def test_errors(self):
        for text, word in (("no equal sign here", "형식"), ("= 1", "키가 비어"), ("a b = 1", "키에는"), ("키 = 1", "키에는"),
                           ('a#b = 1', "키에는"), ('k = a"b', "값에는"), ("k = 값", "값에는"), ('k = "a"b"', "값에는"),
                           ("displayName = x", "바꿀 수 없음"), ("UUID.BIOS = 1", "바꿀 수 없음"),
                           ("scsi0:0.fileName = a.vmdk", "바꿀 수 없음"), ("nvram = a", "바꿀 수 없음")):
            items, errs = core.parse_profile_text(text, "VM")
            self.assertEqual(items, [], text)
            self.assertEqual(len(errs), 1, text)
            self.assertIn(word, errs[0], text)
            self.assertIn("1번째 줄", errs[0])

    def test_esx_option_names(self):
        items, errs = core.parse_profile_text("Numa.LocalityWeightActionAffinity = 0\n/Cpu/HTWholeCoreThreshold = \"800\"\n"
                                              "Misc.Sub.Deep = text value\n", "ESX")
        self.assertEqual(errs, [])
        self.assertEqual(items, [("/Numa/LocalityWeightActionAffinity", "0"), ("/Cpu/HTWholeCoreThreshold", "800"),
                                 ("/Misc/Sub/Deep", "text value")])

    def test_esx_option_errors(self):
        for text, word in (("LocalityWeightActionAffinity = 0", "형식"), ("/Numa = 0", "형식"), ("/Numa//X = 0", "형식"),
                           ("Numa.Locality Weight = 0", "키에는"), ("/Numa/X =", "값이 비어")):
            items, errs = core.parse_profile_text(text, "ESX")
            self.assertEqual((items, len(errs)), ([], 1), text)
            self.assertIn(word, errs[0], text)

    def test_esx_same_option_two_spellings(self):
        items, errs = core.parse_profile_text("Numa.X = 1\n/Numa/X = 2\n", "ESX")
        self.assertEqual((items, errs), ([("/Numa/X", "2")], []))

    def test_merge_order_and_overlap(self):
        a = core.parse_profile_text(PROF[1]["text"], "VM")[0]
        b = core.parse_profile_text(PROF[2]["text"], "VM")[0]
        self.assertEqual(core.merge_profile_items([a, b]), [("SCHED.MEM.PIN", "FALSE"), ("sched.mem.prealloc", "TRUE"),
                                                            ("sched.cpu.affinity", "0,1,2,3"), ("numa.nodeAffinity", "")])
        self.assertEqual(core.merge_profile_items([b, a])[1], ("sched.mem.pin", "TRUE"), "뒤 프로파일의 값")
        self.assertEqual(core.merge_profile_items([[("/A/b", "1")], [("/a/B", "2")]], vm=False), [("/A/b", "1"), ("/a/B", "2")])

    def test_profiles_file_roundtrip(self):
        d = tempfile.mkdtemp(prefix="tcgen_prof_")
        try:
            self.assertEqual(core.load_profiles(d), [])
            rows = PROF + [{"name": " x ", "target": "ESX 호스트", "text": "a = 1\r\nb = 2\r", "desc": None}]
            core.save_profiles(d, rows)
            got = core.load_profiles(d)
            self.assertEqual(got[:3], PROF)
            self.assertEqual(got[3], {"name": "x", "target": "ESX", "text": "a = 1\nb = 2\n", "desc": ""})
            with open(os.path.join(d, core.PROFILES_FILE), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["version"], 1)
            self.assertIn(core.PROFILES_FILE, core.DATA_FILES)
            self.assertIn(core.PROFILES_FILE, core.backup_settings(d, now=NOW)[1])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_default_profiles(self):
        """기본 프로파일 12개: 이름과 순서, 내용이 오류 없이 읽힘, 적용 프로파일과 삭제 프로파일은 같은 키."""
        rows = core.default_profiles()
        self.assertEqual([r["name"] for r in rows],
                         ["lpage_1g_on", "lpage_1g_off", "halt_in_monitor_on", "halt_in_monitor_off", "latency_high", "latency_normal",
                          "vpmc_on", "vpmc_off", "kstats_on", "kstats_off", "ht_wholecore_1", "ht_wholecore_800"])
        self.assertEqual(len(rows), len(core.DEFAULT_PROFILES))
        items = {}
        for r in rows:
            self.assertTrue(core.NAME_RE.match(r["name"]), r["name"])
            self.assertEqual(sorted(r), ["desc", "name", "target", "text"])
            self.assertTrue(r["desc"], r["name"])
            got, errs = core.parse_profile_text(r["text"], r["target"])
            self.assertEqual(errs, [], r["name"])
            self.assertTrue(got, r["name"])
            items[r["name"]] = got
        self.assertEqual(items["lpage_1g_on"], [("sched.mem.lpage.enable1GPage", "TRUE"), ("sched.mem.pin", "TRUE"),
                                                ("sched.mem.prealloc", "TRUE"), ("sched.mem.prealloc.pinnedMainMem", "TRUE"),
                                                ("sched.swap.vmxSwapEnabled", "FALSE")])
        self.assertEqual(items["halt_in_monitor_on"], [("monitor_control.halt_in_monitor", "TRUE"), ("monitor.idleLoopSpinBeforeHalt", "TRUE"),
                                                       ("monitor.idleLoopMinSpinUS", "50"),
                                                       ("monitor_control.disable_pause_loop_exiting", "TRUE")])
        self.assertEqual(items["latency_high"], [("sched.cpu.latencySensitivity", "high")])
        self.assertEqual(items["latency_normal"], [("sched.cpu.latencySensitivity", "normal")])
        self.assertEqual(items["vpmc_on"], [("vpmc.enable", "TRUE")])
        self.assertEqual(items["kstats_on"], [("vmx.buildType", "stats"), ("vmx.log.keepOld", "20")])
        self.assertEqual(items["ht_wholecore_1"], [("/Cpu/HTWholeCoreThreshold", "1")])
        self.assertEqual(items["ht_wholecore_800"], [("/Cpu/HTWholeCoreThreshold", "800")])
        for on, off in (("lpage_1g_on", "lpage_1g_off"), ("halt_in_monitor_on", "halt_in_monitor_off"), ("vpmc_on", "vpmc_off"),
                        ("kstats_on", "kstats_off")):
            self.assertEqual([k for k, _ in items[on]], [k for k, _ in items[off]], "적용과 삭제는 같은 키: %s" % on)
            self.assertTrue(all(v == "" for _, v in items[off]), "삭제 프로파일의 값은 모두 비어 있음: %s" % off)
            self.assertTrue(all(v != "" for _, v in items[on]), on)
        self.assertEqual([r["name"] for r in rows if r["target"] == "ESX"], ["ht_wholecore_1", "ht_wholecore_800"])
        self.assertIsNot(core.default_profiles()[0], rows[0], "호출할 때마다 새 사전")

    def test_default_profiles_seed_and_add(self):
        d = tempfile.mkdtemp(prefix="tcgen_prof_")
        try:
            self.assertEqual(core.load_profiles(d), [], "seed 를 주지 않으면 빈 목록")
            self.assertFalse(os.path.exists(os.path.join(d, core.PROFILES_FILE)), "seed 를 주지 않으면 파일을 만들지 않음")
            self.assertEqual(core.load_profiles(d, seed=True), core.default_profiles(), "처음 실행: 기본 프로파일로 채움")
            self.assertEqual(core.load_profiles(d), core.default_profiles(), "채운 내용을 profiles.json 에 저장")
            core.save_profiles(d, [])
            self.assertEqual(core.load_profiles(d, seed=True), [], "파일이 있으면 비어 있어도 다시 채우지 않음")
            rows = [dict(PROF[1]), {"name": "vpmc_on", "target": "VM", "text": "vpmc.enable = FALSE", "desc": "고친 내용"}]
            added = core.add_default_profiles(rows)
            self.assertEqual(len(added), len(core.DEFAULT_PROFILES) - 1)
            self.assertNotIn("vpmc_on", added)
            self.assertEqual([r["name"] for r in rows[:2]], ["pin", "vpmc_on"], "있던 행은 자리 그대로")
            self.assertEqual(rows[1]["text"], "vpmc.enable = FALSE", "이름이 같은 행은 내용을 덮어쓰지 않음")
            self.assertEqual([r["name"] for r in rows[2:]], added, "없는 이름만 끝에 추가")
            self.assertEqual(core.add_default_profiles(rows), [], "두 번째에는 추가할 것이 없음")
            self.assertEqual(len(rows), len(core.DEFAULT_PROFILES) + 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_default_profiles_go_into_a_plan(self):
        """기본 프로파일을 케이스에 고르면 plan 의 AH, AV 줄에 키와 값이 그대로 들어간다(삭제는 빈 값)."""
        c = case("d1", vms=[vm("esx01ev01", "job_a", profiles=["lpage_1g_on", "halt_in_monitor_off", "latency_normal", "vpmc_on",
                                                                 "kstats_off"])], esx_profiles=["ht_wholecore_800"])
        res = gen([c], prof=core.default_profiles())
        self.assertEqual(res["errors"], [])
        for line in ("AH 001 /Cpu/HTWholeCoreThreshold=800", "AV 001 esx01ev01 sched.mem.lpage.enable1GPage=TRUE",
                     "AV 001 esx01ev01 sched.mem.pin=TRUE", "AV 001 esx01ev01 sched.swap.vmxSwapEnabled=FALSE",
                     "AV 001 esx01ev01 monitor_control.halt_in_monitor=", "AV 001 esx01ev01 monitor.idleLoopMinSpinUS=",
                     "AV 001 esx01ev01 sched.cpu.latencySensitivity=normal", "AV 001 esx01ev01 vpmc.enable=TRUE",
                     "AV 001 esx01ev01 vmx.buildType=", "AV 001 esx01ev01 vmx.log.keepOld="):
            self.assertIn(line + "\n", res["plan"])
        self.assertEqual(len([x for x in res["plan"].split("\n") if x.startswith("AV 001 ")]), 5 + 4 + 1 + 1 + 2)
        on_off = gen([case("d2", vms=[vm("esx01ev01", "job_a", profiles=["lpage_1g_on", "lpage_1g_off"])])], prof=core.default_profiles())
        self.assertEqual(on_off["errors"], [])
        self.assertIn("AV 001 esx01ev01 sched.mem.pin=\n", on_off["plan"], "적용과 삭제를 함께 고르면 표에서 아래쪽인 삭제 프로파일의 값")


class SeqValidationTests(unittest.TestCase):
    def errors(self, cases, **kw):
        res = gen(cases, **kw)
        self.assertEqual(res["plan"], "", "오류가 있으면 plan 과 블록을 만들지 않음")
        self.assertEqual((res["check"], res["run"]), ("", ""))
        return res["errors"]

    def assert_one(self, errs, word):
        self.assertEqual(len(errs), 1, errs)
        self.assertIn(word, errs[0])

    def test_valid_case_has_no_error(self):
        res = gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"], ["pin"])], esx_monitors=["host_mon"],
                        esx_profiles=["numa0"], max_wait="12:00")])
        self.assertEqual((res["errors"], res["warnings"]), ([], []))
        self.assertEqual(res["mode"], "seq")

    def test_process_name_is_not_needed(self):
        """잡이 끝났는지 확인하지 않으므로(v2.12) 탭 1 의 프로세스 이름은 쓰지 않는다. 비어 있어도, 어떤 글이어도 된다."""
        for proc in ("", "sh", "it's", "한글", "x" * 200):
            res = gen([case("c1"), case("c2")], run=[dict(RUN[0], proc=proc), RUN[1]])
            self.assertEqual((res["errors"], res["warnings"]), ([], []), proc)
            self.assertNotIn("\nPROC ", res["plan"])
        mon = [dict(MON[0], start="/opt/perf record -p `pgrep run_a.sh`")]
        res = gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])], max_wait="2:00")], mon=mon)
        self.assertEqual((res["errors"], res["warnings"]), ([], []), "모니터링 명령에 잡 이름이 있어도 알리지 않음")

    def test_settings_rules(self):
        for over, word in (({"jump_log_dir": "relative/dir"}, "로그 폴더"), ({"jump_log_dir": "/var/my log"}, "로그 폴더"),
                           ({"jump_log_dir": "~/로그"}, "로그 폴더"), ({"esx_addr": "esx01"}, "접속 주소"),
                           ({"esx_addr": "esx01=10.0.0.1;bad name=1"}, "접속 주소"), ({"vm_user": "a b"}, "접속 계정"),
                           ({"vm_user": "-oProxyCommand=x"}, "접속 계정"), ({"mon_lead_min": -1}, "'모니터링 시작 N분 전' 은 0 이상의 정수"),
                           ({"seq_poweron_wait_min": 0}, "VM 켜기 대기"), ({"seq_snap_lead_min": 0}, "'구성 스냅샷 N분 전' 은(는) 1 이상의 정수"),
                           ({"seq_prep_lead_min": 0}, "'설정 적용 N분 전' 은(는) 1 이상의 정수"),
                           ({"seq_start_spacing_min": 0}, "'잡 시작 최소 간격' 은(는) 1 이상의 정수"),
                           ({"seq_prep_lead_min": "x"}, "'설정 적용 N분 전'")):
            self.assert_one(self.errors([case("c1")], st=settings(**over)), word)
        for ok in ({"jump_log_dir": "/data/tcgen/log-1"}, {"jump_log_dir": "~/a.b/c_d"}, {"jump_log_dir": ""},
                   {"esx_addr": "esx01=10.0.0.11; esx02=esx02.lab.local"}, {"vm_user": "tester"},
                   {"seq_poweron_wait_min": 120}, {"mon_lead_min": 0},
                   # 예전 판의 설정 값(변경 작업 뒤 대기, 케이스 사이 간격, 잡 종료 확인)은 쓰지 않는다: 어떤 값이어도 오류가 아니다
                   {"seq_settle_min": -1}, {"seq_gap_min": -1}, {"seq_poll_sec": 0}, {"seq_gone_count": 0}, {"seq_snap_gap_min": -1}):
            self.assertEqual(gen([case("c1")], st=settings(**ok))["errors"], [], ok)
        plan = gen([case("c1")], st=settings(seq_poweron_wait_min=120, seq_poll_sec=3600, seq_gap_min=45))["plan"]
        self.assertEqual([x for x in plan.split("\n") if x.startswith("SET ")],
                         ["SET MON_LEAD_MIN 5", "SET SNAP_LEAD_MIN 10", "SET SPACING_MIN 5", "SET PREP_LEAD_MIN 30",
                          "SET POWERON_WAIT_MIN 120", "SET BOOT_WAIT_MIN 30", "SET SHUTDOWN_WAIT_MIN 10"],
                         "plan 의 설정은 일곱 가지다. 예전 판의 대기 설정은 넣지 않는다")
        self.assertEqual(core.DEFAULT_SETTINGS["seq_poweron_wait_min"], 90, "VM 켜기 대기의 기본값은 90분")
        self.assertEqual((core.DEFAULT_SETTINGS["seq_prep_lead_min"], core.DEFAULT_SETTINGS["seq_snap_lead_min"],
                          core.DEFAULT_SETTINGS["mon_lead_min"]), (30, 10, 5),
                         "설정 적용 30분 전, 구성 스냅샷 10분 전(사용자 결정 2026-11-18), 모니터링 시작 5분 전")

    def test_settings_change_comes_before_the_snapshot_and_the_monitoring(self):
        """'설정 적용 N분 전' 은 '구성 스냅샷 N분 전' 보다 커야 하고, 모니터링이 있으면 '모니터링 시작 N분 전' 보다도 커야 한다
        (설정을 변경한 뒤에 줄을 등록하므로, 그보다 먼저 시작할 모니터링은 등록할 수 없다)."""
        mon_case = case("m1", vms=[vm("esx01ev01", "job_a", ["guest_nostop"])])
        self.assert_one(self.errors([case("c1")], st=settings(seq_prep_lead_min=10)),
                        "설정: '설정 적용 N분 전'(10)은 '구성 스냅샷 N분 전'(10)보다 커야 함")
        self.assert_one(self.errors([mon_case], st=settings(mon_lead_min=30)),
                        "설정: '설정 적용 N분 전'(30)은 '모니터링 시작 N분 전'(30)보다 커야 함")
        self.assertEqual(gen([case("c1")], st=settings(mon_lead_min=30))["errors"], [], "모니터링이 없으면 보지 않음")
        self.assertEqual(gen([mon_case], st=settings(mon_lead_min=29))["errors"], [])
        self.assertEqual(gen([mon_case], st=settings(seq_snap_lead_min=5, mon_lead_min=8))["errors"], [],
                         "구성 스냅샷과 모니터링 시작의 앞뒤는 정해져 있지 않다(스냅샷은 읽기만 한다)")

    def test_max_wait_format(self):
        self.assert_one(self.errors([case("c1", max_wait="soon")]), "최대 대기")
        res = gen([case("c1", max_wait="1:30"), case("c2", max_wait="")])
        self.assertEqual(res["errors"], [])
        self.assertNotIn("\nMAXWAIT ", res["plan"], "최대 대기는 모니터링 중지 시각을 정하는 데만 쓴다. plan 에 따로 넣지 않는다")
        self.assertNotIn("\nMSTOP ", res["plan"], "모니터링이 없으면 중지 시각도 없음")

    def test_warnings(self):
        """경고(생성 전에 계속할지 묻는 글)는 건너뛰는 케이스와 빈 프로파일뿐이다. 마지막 케이스의 모니터링 중지는 알림으로 적는다
        (사용자 지적 2026-11-18 "이 경고가필요해? 모니터링 걸지도않았는데 왜띄우는거야")."""
        res = gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])])])
        self.assertEqual((res["errors"], res["warnings"]), ([], []))
        self.assertEqual(res["notes"], ["마지막 케이스(c1)는 모니터링 중지 시각이 없음. 정하려면 모니터링 중지 칸에서 직접 입력을 고르거나 "
                                        "최대 대기 칸에 시간을 적을 것(비워 두면 다음 실행이나 tcrun stop 때 중지)"])
        self.assertEqual(gen([case("c1")])["notes"], [], "모니터링이 없으면 알림도 없음")
        self.assertEqual(gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_nostop"])])])["notes"], [], "중지 명령이 없는 모니터링도 없음")
        self.assertEqual(gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])], max_wait="9:00")])["notes"], [],
                         "최대 대기를 적으면 그 시각에 중지하므로 알리지 않음")
        two = gen([case("a1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])]), case("b1", esx="esx02", esx_monitors=["host_mon"])])
        self.assertEqual(two["notes"], ["마지막 케이스(a1, b1)는 모니터링 중지 시각이 없음. 정하려면 모니터링 중지 칸에서 직접 입력을 "
                                        "고르거나 최대 대기 칸에 시간을 적을 것(비워 두면 다음 실행이나 tcrun stop 때 중지)"],
                         "ESX 호스트마다 마지막 케이스. 글은 하나")
        self.assertEqual(gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])], stop_mode="none")])["notes"], [],
                         "중지 없음을 고른 케이스는 알리지 않음")
        empty = gen([case("c1", vms=[vm("esx01ev01", "job_a", [], ["none"])])], prof=[{"name": "none", "target": "VM", "text": "# x", "desc": ""}])
        self.assertEqual(empty["warnings"], ["탭 3 'none': 설정 내용이 비어 있음"])

    def test_monitoring_without_job_is_error(self):
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "(없음)", ["guest_perf"])])]), "모니터링만 있고 잡이 없음")
        self.assert_one(self.errors([case("c1", vms=[], esx_monitors=["host_mon"])]), "모니터링만 있고 잡이 없음")

    def test_settings_only_case_is_allowed(self):
        res = gen([case("only-set", vms=[vm("esx01ev01", "(없음)", [], ["pin"])], esx_profiles=["numa0"]), case("c2")])
        self.assertEqual(res["errors"], [])
        self.assertIn("CASE 001 esx01 only-set", res["plan"])
        self.assertNotIn("JOB 001", res["plan"])
        self.assertIn("JOB 002", res["plan"])

    def test_case_without_anything_is_skipped(self):
        res = gen([case("empty", vms=[vm("esx01ev01")]), case("c2")])
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertNotIn("empty", res["plan"])

    def test_profile_errors(self):
        prof = [{"name": "bad", "target": "VM", "text": "a b = 1\nk = \"x\"y\"\nok = 1", "desc": ""}]
        errs = self.errors([case("c1", vms=[vm("esx01ev01", "job_a", [], ["bad"])])], prof=prof)
        self.assertEqual(len(errs), 2, errs)
        self.assertTrue(all(e.startswith("탭 3 'bad': ") for e in errs))
        prof = [{"name": "h", "target": "ESX", "text": "NoSection = 1", "desc": ""}]
        self.assert_one(self.errors([case("c1", esx_profiles=["h"])], prof=prof), "/구역/옵션")

    def test_unused_broken_profile_does_not_block(self):
        prof = PROF + [{"name": "broken", "target": "VM", "text": "not a setting", "desc": ""}]
        self.assertEqual(gen([case("c1")], prof=prof)["errors"], [])

    def test_settings_need_esx_host(self):
        self.assert_one(self.errors([case("c1", esx="", vms=[vm("vm-a", "job_a", [], ["pin"])])]), "ESX 호스트")
        self.assert_one(self.errors([case("c1", esx="-", vms=[vm("vm-a", "job_a")], esx_profiles=["numa0"])]), "ESX 호스트")
        self.assertEqual(gen([case("c1", esx="", vms=[vm("vm-a", "job_a")])])["errors"], [], "설정과 ESX 모니터링이 없으면 허용")

    def test_missing_and_wrong_target_profile(self):
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "job_a", [], ["nope"])])]), "탭 3 에 없음")
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "job_a", [], ["numa0"])])]), "대상은 ESX 호스트")
        self.assert_one(self.errors([case("c1", esx_profiles=["pin"])]), "대상은 VM")

    def test_existing_rules_still_apply(self):
        self.assert_one(self.errors([case("bad name")]), "케이스 이름")
        self.assertEqual(len(self.errors([case("dup"), case("dup")])), 2)
        self.assert_one(self.errors([case("c1", vms=[vm("bad vm", "job_a")])]), "이름이 비었거나")
        self.assert_one(self.errors([case("c1", vms=[vm("-oProxyCommand", "job_a")])]), "이름이 비었거나")
        self.assert_one(self.errors([case("c1", vms=[vm("a", "job_a"), vm("a", "job_a")])]), "같은 VM 이름")
        self.assert_one(self.errors([case("c1")], run=[dict(RUN[0], cmd="echo hi!")]), "! 문자")
        self.assert_one(self.errors([case("c1")], run=[dict(RUN[0], cmd="echo\thi")]), "탭 문자")
        self.assert_one(self.errors([case("c1")], run=[dict(RUN[0], cmd="echo 한글")]), "ASCII 밖")
        self.assert_one(self.errors([case("c1")], run=[RUN[0], dict(RUN[0])]), "두 번 이상")
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "nojob")])]), "탭 1 에 없음")
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "job_a", ["nomon"])])]), "탭 2 에 없음")
        self.assert_one(self.errors([case("c1", vms=[vm("esx01ev01", "job_a", ["host_mon"])])]), "실행 위치")
        self.assert_one(self.errors([case("c1")], prof=[PROF[0], dict(PROF[0])]), "두 번 이상")
        self.assert_one(self.errors([case("c1")], prof=[dict(PROF[0], name="이름")]), "프로파일 이름")

    def test_same_vm_under_two_esx_hosts(self):
        self.assert_one(self.errors([case("c1", esx="esx01", vms=[vm("shared", "job_a")]),
                                     case("c2", esx="esx02", vms=[vm("shared", "job_a")])]), "ESX 호스트가 다른 케이스")

    def test_applied_cases_are_left_out(self):
        res = gen([case("done1", applied=True), case("c2")])
        self.assertEqual(res["skipped"], 1)
        self.assertIn("CASE 001 esx01 c2", res["plan"])
        self.assertNotIn("done1", res["plan"])
        self.assertIn("모든 케이스가 적용 체크", gen([case("a", applied=True)])["errors"][0])

    def test_time_mode_only_warns_about_profiles(self):
        c = case("c1", vms=[vm("esx01ev01", "job_a", [], ["pin"])], esx_profiles=["numa0"])
        c.update(start="2026-12-01 10:00:00", interval="1:00")
        res = gen([c], st=settings(run_mode="time"))
        self.assertEqual((res["mode"], res["errors"]), ("time", []))
        self.assertTrue(any("순서 실행에서만 적용" in w for w in res["warnings"]))
        self.assertNotIn("sched.mem.pin", res["vm"])
        self.assertEqual(res["setup"], core.setup_block())


class PlanTests(unittest.TestCase):
    def two(self):
        return [case("caseA", vms=[vm("esx01ev01", "job_a", ["guest_perf", "guest_nostop"], ["pin", "aff"], True),
                                   vm("esx01ev02", "job_b4"), vm("esx01ev03", "(없음)", [], ["pin"]), vm("esx01ev04")],
                     esx_monitors=["host_mon"], esx_profiles=["numa0"], max_wait="12:00", desc="설명, \"따옴표\"\n둘째 줄", group="g1"),
                case("caseB", esx="", vms=[vm("vm-b", "job_a")])]

    def test_command_text_is_the_v14_crontab_command(self):
        """명령 글은 v1.4 의 render_command 결과 + 표식이다. 시각 칸만 점프 VM 이 붙인다."""
        res = gen(self.two())
        for line in res["plan"].split("\n"):
            m = re.match(r"^(MS|JOB|MX) (\d+) (\S+) (.*)$", line)
            if m:
                self.assertTrue(m.group(4).endswith(" # tcgen %s %s" % ("caseA" if m.group(2) == "001" else "caseB", m.group(3))))
        t = dt.datetime(2026, 11, 16, 15, 0)
        self.assertIn("JOB 001 esx01ev02 " + core.cron_line(t, RUN[1]["cmd"], "caseA", "esx01ev02")[len("00 15 16 11 * "):] + "\n",
                      res["plan"])
        self.assertIn("EMS 001 " + core.cron_line(t, MON[1]["start"], "caseA", "esx01", esx=True)[len("00 06 16 11 * "):] + "\n",
                      res["plan"])

    def test_blocks_carry_the_plan(self):
        res = gen(self.two())
        data = res["plan"].encode("utf-8")
        for key, action in (("check", "check"), ("run", "start")):
            lines = res[key].split("\n")
            self.assertEqual((lines[0], lines[-2], lines[-1]), ("sh << TCEOF", "TCEOF", ""))
            a, b = lines.index(core._hd("  tr -d '\\r' << TCB64 | base64 --decode | gzip -dc > $R/plan.new")), lines.index("TCB64")
            self.assertEqual(core.gz_b64_decode(lines[a + 1:b]), data)
            self.assertIn(hashlib.sha256(data).hexdigest(), res[key])
            self.assertIn("sh \\$D/tcrun %s \\$R/plan < /dev/null" % action, res[key])
            self.assertIn('if [ "\\$V" = "%s" ]; then' % core.VERSION, res[key])
        self.assertEqual(gen(self.two())["run"], res["run"], "같은 입력이면 블록이 매번 같음")
        self.assertEqual(gen(self.two())["setup"], res["setup"])

    def test_setup_block_installs_the_jump_files(self):
        text = core.seq_setup_block()
        lines = text.split("\n")
        self.assertEqual((lines[0], lines[-3], lines[-2]), ("sh << TCSETUP", "TCSETUP", "sh ~/.tcgen/postsetup"))
        files = dict(core.jump_files())
        self.assertEqual(sorted(files), ["askpass", "esxssh", "postsetup", "setpw", "tcgen_apply.py", "tcrun", "vmssh"])
        marks = [i for i, x in enumerate(lines) if x == "TCB64"]
        starts = [i for i, x in enumerate(lines) if "<< TCB64" in x]
        self.assertEqual(len(marks), 2)
        for a, b, name in ((starts[0], marks[0], "tcrun"), (starts[1], marks[1], "tcgen_apply.py")):
            body = core.gz_b64_decode(lines[a + 1:b])
            self.assertEqual(body.decode("ascii"), files[name])
            self.assertIn("ck %s %s" % (name, hashlib.sha256(body).hexdigest()), lines[b + 1])
        self.assertIn('ssh-keygen -t rsa -b 4096 -N "" -f \\$D/id_esx', text)
        self.assertIn("/etc/ssh/keys-root/authorized_keys", text)

    def test_blocks_are_safe_to_paste(self):
        """블록은 ASCII 만 쓰고, csh 가 붙여넣는 글에서 따로 처리하는 ! 와 탭이 없고, 줄 끝이 역슬래시가 아니다."""
        res = gen(self.two())
        for key in ("setup", "check", "run", "vm_wipe"):
            text = res[key]
            self.assertTrue(text.endswith("\n"), key)
            self.assertTrue(all(32 <= ord(ch) <= 126 or ch == "\n" for ch in text), key)
            self.assertNotIn("!", text, key)
            for line in text.split("\n"):
                self.assertFalse(line.endswith("\\"), line)
                self.assertLessEqual(len(line), 200, line)
        self.assertIn("설명", res["plan"], "plan 에는 한글 설명이 있지만 블록에는 base64 로 들어감")

    def test_plan_text(self):
        res = gen(self.two(), st=settings(vm_user="tester", esx_addr="esx01=10.0.0.11;esx09=10.0.0.19", jump_log_dir="/data/log"))
        self.assertEqual(res["errors"], [])
        t1 = int(time.mktime(dt.datetime(2026, 11, 16, 16, 0).timetuple()))
        t2 = int(time.mktime(dt.datetime(2026, 11, 16, 18, 0).timetuple()))
        tx = int(time.mktime(dt.datetime(2026, 11, 17, 4, 0).timetuple()))      # 잡 시작 16:00 + 최대 대기 12:00
        want = """TCGEN-PLAN 2
VER 2.15
RUNID 20261116-143000
LOGDIR /data/log
GEN 2026-11-16 14:30:00
SET MON_LEAD_MIN 5
SET SNAP_LEAD_MIN 10
SET SPACING_MIN 5
SET PREP_LEAD_MIN 30
SET POWERON_WAIT_MIN 90
SET BOOT_WAIT_MIN 30
SET SHUTDOWN_WAIT_MIN 10
ADDR esx01 10.0.0.11
CHAIN esx01
CHAIN -
RPTH 테스트 그룹,케이스명,설명,호스트 이름,잡 시작 시각,잡 종료 시각,VM,종료 기준 VM
CFGL h1 구분
CFGL h2 항목
CFGL time 수집 시각
CFGL basic VM 기본 정보
CFGL adv vm advanced config
CFGL guest Guest OS
CFGL aff CPU affinity
CFGL ref_esx 기준 시각 (ESX 호스트 조회)
CFGL ref_guest 기준 시각 (게스트 조회)
CFGL none (없음)
LIST run 20261116-143000: 2 cases. Check them before they are registered in crontab.
LIST      ${TC_HOST} = host name of the VM, ${TC_START} = start time (14 digits): the variables of tcgen
LIST ---- ESX host esx01: 1 case
LIST case 1/2  caseA   job start 2026-11-16 16:00
LIST   monitoring start 2026-11-16 15:55, stop 2026-11-17 04:00
LIST   vm esx01ev01
LIST     job: /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1
LIST     monitoring guest_perf: /home/tester/mon/start_mon.sh >> /home/tester/log/mon-${TC_HOST}-${TC_START}.log
LIST     monitoring guest_nostop: /home/tester/mon/once.sh
LIST   vm esx01ev02
LIST     job: /home/tester/bench/run_b.sh -n 4 -tag x%%y
LIST   vm esx01ev03
LIST     job: (none)
LIST   vm esx01ev04
LIST     job: (none)
LIST   ESX host esx01
LIST     monitoring host_mon: /bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start '%%H'
LIST ---- no ESX host: 1 case
LIST case 2/2  caseB   job start 2026-11-16 18:00
LIST   vm vm-b
LIST     job: /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1
CASE 001 esx01 caseA
SEG 001 1
START 001 %d 2026-11-16 16:00
MSTOP 001 %d 2026-11-17 04:00
RPTA 001 g1,caseA,"설명, ""따옴표"" 둘째 줄",esx01
RPTB 001 esx01ev01;esx01ev02,esx01ev01
VM 001 esx01ev01 tester@esx01ev01
VM 001 esx01ev02 tester@esx01ev02
VM 001 esx01ev03 tester@esx01ev03
VM 001 esx01ev04 tester@esx01ev04
AH 001 /Numa/LocalityWeightActionAffinity=0
AH 001 /Cpu/HTWholeCoreThreshold=0
AV 001 esx01ev01 SCHED.MEM.PIN=FALSE
AV 001 esx01ev01 sched.mem.prealloc=TRUE
AV 001 esx01ev01 sched.cpu.affinity=0,1,2,3
AV 001 esx01ev01 numa.nodeAffinity=
AV 001 esx01ev03 sched.mem.pin=TRUE
AV 001 esx01ev03 sched.mem.prealloc=TRUE
PAT 001 esx01ev01  # tcgen caseA esx01ev01$
MS 001 esx01ev01 TC_HOST=`hostname -s`; TC_START=`date +\\%%Y\\%%m\\%%d\\%%H\\%%M\\%%S`; /home/tester/mon/start_mon.sh >> /home/tester/log/mon-${TC_HOST}-${TC_START}.log # tcgen caseA esx01ev01
MS 001 esx01ev01 /home/tester/mon/once.sh # tcgen caseA esx01ev01
JOB 001 esx01ev01 TC_HOST=`hostname -s`; TC_START=`date +\\%%Y\\%%m\\%%d\\%%H\\%%M\\%%S`; /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1 # tcgen caseA esx01ev01
MX 001 esx01ev01 /home/tester/mon/stop_mon.sh # tcgen caseA esx01ev01
PAT 001 esx01ev02  # tcgen caseA esx01ev02$
JOB 001 esx01ev02 /home/tester/bench/run_b.sh -n 4 -tag x\\%%y # tcgen caseA esx01ev02
EPAT 001  # tcgen caseA esx01$
EMS 001 /bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start '%%H' # tcgen caseA esx01
EMX 001 /bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop # tcgen caseA esx01
END 001
CASE 002 - caseB
SEG 002 1
START 002 %d 2026-11-16 18:00
RPTA 002 ,caseB,,
RPTB 002 vm-b,
VM 002 vm-b tester@vm-b
PAT 002 vm-b  # tcgen caseB vm-b$
JOB 002 vm-b TC_HOST=`hostname -s`; TC_START=`date +\\%%Y\\%%m\\%%d\\%%H\\%%M\\%%S`; /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1 # tcgen caseB vm-b
END 002
""" % (t1, tx, t2)
        self.assertEqual(res["plan"], want)
        self.assertEqual(res["chains"], ["esx01", "-"])
        self.assertEqual(res["wipe_hosts"], ["esx01ev01", "esx01ev02", "vm-b"])
        self.assertEqual(res["run_id"], "20261116-143000")
        for gone in ("NOCHK", "PROC", "MAXWAIT"):
            self.assertNotIn("\n%s " % gone, res["plan"], "v2.12 에서 없어진 줄")

    def test_plan_view_text(self):
        view = gen(self.two())["plan_view"]
        for word in ("실행 ID 20261116-143000", "점프 VM 작업 폴더 ~/.tcgen, 로그 폴더 ~/tcgen_log",
                     "등록 기록 ~/tcgen_log/tcrun-20261116-143000-jobs.csv (케이스, ESX 호스트, VM, 잡 시작, crontab 등록 여부)",
                     "진행 확인 sh ~/.tcgen/tcrun status, 중지 sh ~/.tcgen/tcrun stop (진행을 끝내고 등록한 crontab 줄을 지우고 시작된 "
                     "모니터링을 중지함)",
                     "[타임라인]  T = 케이스의 잡 시작 시각", "설정 변경이 없는 케이스\n  S 블록 실행 직후 : crontab 에 잡 등록\n",
                     "설정 변경이 있는 케이스\n  T-30분 : VM 종료 -> 설정 적용 -> VM 전원 ON -> crontab 에 잡 등록\n",
                     "  T-10분 : 구성 스냅샷\n  T-5분 : 모니터링 시작\n  T : 잡 시작\n  잡 실행 중 : VM 접속 없음\n"
                     "  다음 케이스의 T-10분 : 모니터링 중지\n",
                     "[ESX 호스트 esx01] 케이스 1개", "[ESX 호스트 없음] 케이스 1개",
                     "1. caseA   잡 시작 2026-11-16 16:00\n",
                     "   설정과 VM 전원: 실행을 시작할 때 호스트와 비교한다. 다르면 11-16 15:30 에 VM 종료, 설정 적용, VM 전원 ON",
                     "   VM 전원: esx01ev01 켬, esx01ev02 켬, esx01ev03 켬, esx01ev04 켬",
                     "     - esx01ev02 job_b4: /home/tester/bench/run_b.sh -n 4 -tag x%y",
                     "     - ESX 호스트 /Numa/LocalityWeightActionAffinity = 0", "     - esx01ev01 numa.nodeAffinity (이 키의 줄을 삭제)",
                     '     - esx01ev01 SCHED.MEM.PIN = "FALSE"', "   모니터링 (잡 시작 5분 전에 시작):", "   모니터링 중지 (11-17 04:00):",
                     "1. caseB   잡 시작 2026-11-16 18:00\n"):
            self.assertIn(word, view)
        self.assertEqual(view.count("[타임라인]"), 1, "타임라인은 한 번만")
        for gone in ("공통 규칙", "잡 확인", "프로세스", "자동", "변경 작업 뒤 대기", "스냅샷 뒤 대기"):
            self.assertNotIn(gone, view, "v2.12 에서 없어진 설명")
        self.assertNotIn("설정과 VM 전원", view.split("[ESX 호스트 없음]")[1], "ESX 호스트가 없는 케이스는 설정과 전원 줄이 없음")
        viewd = gen(self.two(), st=settings(jump_dir="/data/tcgen/"))["plan_view"]
        self.assertIn("점프 VM 작업 폴더 /data/tcgen,", viewd)
        self.assertIn("중지 sh /data/tcgen/tcrun stop (", viewd)
        self.assertNotIn("cancel", view + viewd, "v2.15 에서 tcrun cancel 을 없앰")
        # 글의 줄은 짧다(사용자 지적 2026-11-18: 한 줄, 내부 계산식 없이). 명령과 설정 줄을 뺀 설명 줄은 110자 이하
        for line in view.split("\n"):
            if not line.startswith("     - "):
                self.assertLessEqual(len(line), 110, line)

    def test_timeline_table(self):
        """탭 5 의 타임라인(사용자 2026-11-18): 열은 시간 순서의 시점, 줄은 설정 변경이 없는 케이스와 있는 케이스."""
        tb = core.timeline_table(30, 10, 5, True, True, True)
        self.assertEqual(tb["cols"], ["S 블록 실행 직후", "T-30분", "T-10분", "T-5분", "T", "잡 실행 중", "다음 케이스의 T-10분"],
                         "모니터링 중지는 다음 케이스 잡 시작 10분 전(사용자 결정 2026-11-09)")
        self.assertEqual(core.timeline_table(30, 10, 5, True, True, True, stop_lead=15)["cols"][-1], "다음 케이스의 T-15분")
        self.assertEqual(tb["rows"], [
            ("설정 변경이 없는 케이스", ["crontab 에 잡 등록", "", "구성 스냅샷, VM 에서 명령 실행", "모니터링 시작", "잡 시작", "VM 접속 없음",
                                 "모니터링 중지"]),
            ("설정 변경이 있는 케이스", ["", "VM 종료 -> 설정 적용 -> VM 전원 ON -> crontab 에 잡 등록", "구성 스냅샷, VM 에서 명령 실행",
                                 "모니터링 시작", "잡 시작", "VM 접속 없음", "모니터링 중지"])])
        plain = core.timeline_table(30, 10, 5, False, False, False)
        self.assertEqual(plain["cols"], ["S 블록 실행 직후", "T-30분", "T-10분", "T", "잡 실행 중"], "모니터링이 없으면 그 열이 없음")
        self.assertEqual(plain["rows"][0][1], ["crontab 에 잡 등록", "", "구성 스냅샷", "잡 시작", "VM 접속 없음"])
        same = core.timeline_table(30, 10, 10, True, False, False)
        self.assertEqual(same["cols"], ["S 블록 실행 직후", "T-30분", "T-10분", "T", "잡 실행 중"])
        self.assertEqual(same["rows"][1][1][2], "구성 스냅샷, 모니터링 시작", "같은 분이면 한 칸에 적는다")
        early = core.timeline_table(20, 5, 8, True, False, False)
        self.assertEqual(early["cols"], ["S 블록 실행 직후", "T-20분", "T-8분", "T-5분", "T", "잡 실행 중"], "열은 시간 순서")
        zero = core.timeline_table(30, 10, 0, True, False, False)
        self.assertEqual(zero["rows"][0][1][-2], "모니터링 시작, 잡 시작")
        res = gen(self.two(), st=settings(seq_guest_cmd="bash /a/push.sh"))
        self.assertEqual(res["timeline_table"], core.timeline_table(30, 10, 5, True, True, True), "생성 결과에는 그 생성의 값으로")
        self.assertEqual(res["timeline"][0], "[타임라인]  T = 케이스의 잡 시작 시각")
        self.assertEqual(res["changes"]["first"], [{"esx": "esx01", "name": "caseA", "at": dt.datetime(2026, 11, 16, 15, 30),
                                                   "text": "esx01 caseA (11-16 15:30)"}], "ESX 호스트가 없는 케이스는 비교하지 않음")
        self.assertEqual(res["changes"]["diff"], [])

    def test_snapshot_command_goes_into_the_plan(self):
        """스냅샷 때 VM 에서 실행할 명령(게스트 구성을 VCF Operations 에 기록하는 스크립트. 사용자 2026-11-17 "네")."""
        plan = gen(self.two())["plan"]
        self.assertNotIn("\nGCMD ", plan, "명령을 적지 않으면 GCMD 줄이 없음")
        res = gen(self.two(), st=settings(seq_guest_cmd="  bash /home/tester/perf/guest_config_push.sh --target auto "))
        self.assertEqual(res["errors"], [])
        self.assertIn("\nGCMD bash /home/tester/perf/guest_config_push.sh --target auto\n", res["plan"])
        self.assertIn("  T-10분 : 구성 스냅샷, VM 에서 명령 실행\n", res["plan_view"])
        self.assertIn("스냅샷 때 VM 에서 실행할 명령: bash /home/tester/perf/guest_config_push.sh --target auto", res["plan_view"])
        self.assertTrue(all(ord(ch) < 128 for ch in [x for x in res["plan"].split("\n") if x.startswith("GCMD ")][0]))
        for bad, word in (("bash /home/한글/push.sh", "ASCII 밖의 문자"), ("bash /a/b.sh --vm <vm이름>", "변수 <vm이름>, <실행시각> 은 이 명령에 쓸 수 없음"),
                          ("bash /a/b.sh !x", "! 문자는 쓸 수 없음"), ("sh /a/b.sh " + "x" * 500, "500자를 넘음"),
                          ("bash /a/b.sh\techo", "탭 문자가 있음")):
            errs = gen(self.two(), st=settings(seq_guest_cmd=bad))["errors"]
            self.assertTrue(any(e.startswith("설정: 스냅샷 때 VM 에서 실행할 명령") and word in e for e in errs), (bad, errs))

    def test_snapshot_time_goes_into_the_plan(self):
        """구성 스냅샷 시점(사용자 결정 2026-11-18 "config 스냅샷은 잡 시작전 10분에 해줘 모든 케이스에 대해"): 적은 값 그대로 plan 에
        넣는다. 생성기가 값을 조정하지 않는다."""
        self.assertEqual(core.DEFAULT_SETTINGS["seq_snap_lead_min"], 10)
        res = gen(self.two())
        self.assertIn("SET SNAP_LEAD_MIN 10\n", res["plan"])
        for n, lead in ((7, 5), (10, 8), (3, 5), (12, 12)):
            r = gen(self.two(), st=settings(seq_snap_lead_min=n, mon_lead_min=lead, stop_lead_min=lead + 5))
            self.assertEqual((r["errors"], r["warnings"], r["notes"]), ([], [], []), (n, lead))
            self.assertIn("SET SNAP_LEAD_MIN %d\n" % n, r["plan"])
            self.assertIn("  T-%d분 : 구성 스냅샷" % n, r["plan_view"])
        errs = gen(self.two(), st=settings(seq_snap_lead_min=0))["errors"]
        self.assertEqual(errs, ["설정: '구성 스냅샷 N분 전' 은(는) 1 이상의 정수"])

    def test_records(self):
        d = tempfile.mkdtemp(prefix="tcgen_rec_")
        try:
            st = settings(record_csv=os.path.join(d, "rec.csv"), log_dir=d)
            # v1.4 형식의 기록 파일이 이미 있는 경우: 새 열은 끝에 추가하고 원본을 따로 복사한다
            core.append_csv(st["record_csv"], core.RECORD_HEADER, [["old"] + [""] * (len(core.RECORD_HEADER) - 1)])
            res = core.generate(with_starts(self.two()), RUN, MON, st, now=NOW, write=True, profiles=PROF, backup_dir=os.path.join(d, "bk"))
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["record_added"], ["진행 방식", "설정", "실행 ID"])
            self.assertTrue(os.path.exists(res["record_backup"]))
            with open(st["record_csv"], encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], core.RECORD_HEADER_SEQ)
            self.assertEqual(rows[1][0], "old")
            row = dict(zip(rows[0], rows[2]))
            self.assertEqual((row["테스트 케이스 이름"], row["진행 방식"], row["실행 ID"]), ("caseA", "순서 실행", "20261116-143000"))
            self.assertEqual((row["잡 시작(KST)"], row["모니터링 시작(KST)"], row["모니터링 중지(KST)"]),
                             ("2026-11-16 16:00:00", "2026-11-16 15:55:00", "2026-11-17 04:00:00"), "계획한 시각을 적는다")
            self.assertEqual(row["설정"], "esx01:numa0, esx01ev01:pin+aff, esx01ev03:pin")
            with open(res["log"], encoding="utf-8-sig", newline="") as f:
                log = list(csv.reader(f))
            self.assertEqual(log[0], core.LOG_HEADER_SEQ)
            kinds = [r[2] for r in log[1:]]
            self.assertEqual(kinds.count("잡"), 3)
            self.assertEqual(kinds.count("설정"), 8)
            job = [dict(zip(log[0], r)) for r in log[1:] if r[2] == "잡"][0]
            self.assertEqual((job["프로세스 이름"], job["crontab 시각 기준"], job["실행 시각(KST)"]), ("", "순서 실행", "2026-11-16 16:00:00"))
            self.assertTrue(job["crontab 줄"].endswith(" # tcgen caseA esx01ev01"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

class SettingsAndDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tcgen_v2d_")
        self.keys = ("TCGEN_CONFIG_DIR", "TCGEN_DEFAULT_DATA_DIR", "TCGEN_V1_CONFIG_DIR", "TCGEN_V1_DATA_DIR")
        self.saved = {k: os.environ.get(k) for k in self.keys}
        os.environ["TCGEN_CONFIG_DIR"] = os.path.join(self.tmp, "cfg2")
        os.environ["TCGEN_DEFAULT_DATA_DIR"] = os.path.join(self.tmp, "tcgen2_data")
        os.environ["TCGEN_V1_CONFIG_DIR"] = os.path.join(self.tmp, "cfg1")
        os.environ["TCGEN_V1_DATA_DIR"] = os.path.join(self.tmp, "tcgen_data")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_v1(self, d, inside_paths=True):
        """v1.4 가 저장하는 형식의 데이터 폴더를 만든다(프로세스 이름 열과 설정의 새 키가 없다)."""
        os.makedirs(os.path.join(d, "testcases"))
        st = {"record_csv": os.path.join(d, "records", "my.csv") if inside_paths else "", "log_dir": "", "vm_user": "tester",
              "mon_lead_min": 7, "stop_lead_min": 12, "vm_prefix": "ev", "default_vms": 3}
        with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump(st, f)
        with open(os.path.join(d, core.RUN_FILE), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["App명", "명령어 명", "잡 조건 설명", "경로/실행 명령"])
            w.writerow(["AppA", "job_a", "1 job", "/bin/run_a.sh"])
        with open(os.path.join(d, core.MON_FILE), "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(core.MON_HEADERS)
        old_case = {"applied": True, "name": "old1", "esx": "esx01", "esx_monitors": [], "interval": "1:00",
                    "start": "2026-11-01 10:00:00", "stop_mode": "auto", "stop_time": "", "desc": "", "group": "", "end_time": "",
                    "vms": [{"name": "esx01ev01", "job": "job_a", "monitors": [], "end_ref": False}]}
        with open(os.path.join(d, core.CASES_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "cases": [old_case]}, f)
        with open(os.path.join(d, "testcases", "test_record.csv"), "w") as f:
            f.write("keep\n")

    def snapshot(self, d):
        snap = {}
        for cur, _, files in os.walk(d):
            for fn in files:
                p = os.path.join(cur, fn)
                with open(p, "rb") as f:
                    snap[os.path.relpath(p, d)] = (f.read(), os.stat(p).st_mtime_ns)
        return snap

    def test_names_differ_from_v1(self):
        self.assertEqual(core.VERSION, "2.15")
        for k in self.keys:
            os.environ.pop(k, None)
        self.assertEqual(os.path.basename(core.default_data_dir()), "tcgen2_data")
        self.assertEqual(os.path.basename(core.config_dir()), "tcgen2")
        self.assertEqual(os.path.basename(core.v1_data_dir()), "tcgen_data")
        self.assertEqual(os.path.basename(core.v1_config_dir()), "tcgen")
        self.assertNotEqual(core.config_dir(), core.v1_config_dir())

    def test_import_copies_four_files_and_leaves_v1_alone(self):
        v1 = core.v1_data_dir()
        self.make_v1(v1)
        before = self.snapshot(v1)
        new = core.data_dir()
        done, src = core.import_from_v1(new)
        self.assertEqual(sorted(done), sorted(core.V1_FILES))
        self.assertEqual(src, v1)
        self.assertEqual(self.snapshot(v1), before, "v1 폴더의 파일은 내용도 시각도 그대로")
        self.assertFalse(os.path.exists(os.path.join(new, "testcases")), "기록과 로그는 가져오지 않음")
        st = core.load_settings(new)
        self.assertEqual((st["vm_user"], st["mon_lead_min"], st["default_vms"]), ("tester", 7, 3))
        self.assertEqual(st["run_mode"], "seq", "v1 설정에는 진행 방식이 없으므로 기본값")
        self.assertEqual(st["record_csv"], os.path.join(new, "records", "my.csv"), "v1 폴더 안을 가리키던 경로는 v2 폴더로 바꿈")
        self.assertFalse(core._inside(core.resolve_paths(new, st)["log_dir"], v1))
        rows = core.load_rows(os.path.join(new, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)
        self.assertEqual(rows, [{"app": "AppA", "name": "job_a", "desc": "1 job", "cmd": "/bin/run_a.sh", "proc": ""}])
        c = core.load_cases(new)[0]
        self.assertEqual((c["name"], c["applied"], c["esx_profiles"], c["max_wait"], c["vms"][0]["profiles"]),
                         ("old1", True, [], "", []))
        self.assertEqual(core.import_from_v1(new)[0], [], "v2 폴더에 데이터가 있으면 다시 가져오지 않음")
        self.assertEqual(self.snapshot(v1), before)

    def test_import_follows_v1_config_json(self):
        moved = os.path.join(self.tmp, "elsewhere", "v1data")
        self.make_v1(moved, inside_paths=False)
        os.makedirs(os.environ["TCGEN_V1_CONFIG_DIR"])
        with open(os.path.join(os.environ["TCGEN_V1_CONFIG_DIR"], core.CONFIG_FILE), "w", encoding="utf-8") as f:
            json.dump({"data_dir": moved}, f)
        before = self.snapshot(os.environ["TCGEN_V1_CONFIG_DIR"])
        self.assertEqual(core.v1_data_dir(), os.path.normpath(moved))
        done, src = core.import_from_v1(core.data_dir())
        self.assertEqual((len(done), src), (4, os.path.normpath(moved)))
        self.assertEqual(self.snapshot(os.environ["TCGEN_V1_CONFIG_DIR"]), before, "v1 의 config.json 도 그대로")
        self.assertFalse(os.path.exists(os.path.join(os.environ["TCGEN_CONFIG_DIR"], core.CONFIG_FILE)),
                         "가져오기는 v2 의 config.json 을 만들지 않음")

    def test_import_does_nothing_without_v1_data(self):
        self.assertEqual(core.import_from_v1(core.data_dir())[0], [])
        self.assertFalse(os.path.exists(core.data_dir()))
        os.makedirs(core.v1_data_dir())
        self.assertEqual(core.import_from_v1(core.data_dir())[0], [])

    def test_settings_defaults_and_bad_values(self):
        d = os.path.join(self.tmp, "s")
        os.makedirs(d)
        s = core.load_settings(d)
        self.assertEqual((s["run_mode"], s["jump_log_dir"], s["esx_addr"], s["seq_guest_cmd"], s["settings_rev"]),
                         ("seq", "", "", "", core.SETTINGS_REV))
        self.assertEqual((s["seq_prep_lead_min"], s["seq_snap_lead_min"], s["mon_lead_min"], s["seq_start_spacing_min"]), (30, 10, 5, 5))
        with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"run_mode": "fast", "seq_prep_lead_min": "x", "seq_snap_lead_min": "7", "jump_log_dir": None,
                       "seq_guest_cmd": None}, f)
        s = core.load_settings(d)
        self.assertEqual((s["run_mode"], s["seq_prep_lead_min"], s["seq_snap_lead_min"], s["jump_log_dir"], s["seq_guest_cmd"]),
                         ("seq", 30, 7, "", ""))
        # v2.11 까지의 설정 파일(사용자 결정 2026-11-18 "60분전에 설정 적용이 아니고 30분으로"): 예전 기본값 60분이 그대로 있으면
        # 30분으로 고친다(한 번만). 사용자가 고친 값과 v2.12 가 저장한 값은 그대로 둔다
        for saved, want in (({"seq_prep_lead_min": 60}, 30), ({"seq_prep_lead_min": 45}, 45), ({"seq_prep_lead_min": 60, "settings_rev": 1}, 30),
                            ({"seq_prep_lead_min": 60, "settings_rev": 2}, 60), ({"seq_prep_lead_min": 20, "settings_rev": 2}, 20)):
            with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
                json.dump(saved, f)
            s = core.load_settings(d)
            self.assertEqual((s["seq_prep_lead_min"], s["settings_rev"]), (want, 2), saved)
        core.save_settings(d, core.resolve_paths(d, dict(s, seq_prep_lead_min=60)))
        self.assertEqual(core.load_settings(d)["seq_prep_lead_min"], 60, "v2.12 가 저장한 뒤에는 60분도 그대로 둔다")
        # 구성 스냅샷 시점 0(준비가 끝나는 대로. v2.11 까지)은 없어졌다: 10분으로 읽는다
        with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"seq_snap_lead_min": 0, "settings_rev": 1}, f)
        self.assertEqual(core.load_settings(d)["seq_snap_lead_min"], 10)
        # 진행 방식 칸은 없어졌다(v2.12). 예전 파일에 시각 지정(time)이 저장돼 있어도 한 가지 방식으로 연다
        for rev in (1, 2):
            with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
                json.dump({"run_mode": "time", "settings_rev": rev}, f)
            self.assertEqual(core.load_settings(d)["run_mode"], "seq")
        self.assertEqual(core.run_mode({"run_mode": "time"}), "time")
        self.assertEqual(core.run_mode({}), "seq")
        self.assertEqual(core.jump_log_dir({"jump_log_dir": "  "}), "~/tcgen_log")

    def test_case_fields_roundtrip(self):
        d = os.path.join(self.tmp, "c")
        os.makedirs(d)
        c = case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"], ["pin", "pin", ""])], esx_profiles=["numa0"], max_wait="2:00")
        core.save_cases(d, [c])
        got = core.load_cases(d)[0]
        self.assertEqual((got["esx_profiles"], got["max_wait"], got["vms"][0]["profiles"]), (["numa0"], "2:00", ["pin"]))
        self.assertEqual(core.format_case_profiles(got), "esx01:numa0, esx01ev01:pin")


class SeqStartTests(unittest.TestCase):
    """케이스의 잡을 시작할 시각(날짜 시각, 시:분, +간격), 설정을 변경하는 시각, 모니터링 중지 시각 (v2.12. 사용자 결정 2026-11-18)."""

    def test_parse(self):
        now = dt.datetime(2026, 11, 16, 9, 30, 45)
        for auto in ("", "  ", "자동", "auto", "AUTO", "-"):
            self.assertIsNone(core.parse_seq_start(auto, now))
        self.assertEqual(core.parse_seq_start("10:00", now), dt.datetime(2026, 11, 16, 10, 0), "시:분만 적으면 지금 뒤에 처음 오는 그 시각")
        self.assertEqual(core.parse_seq_start("9:30", now), dt.datetime(2026, 11, 17, 9, 30), "지금과 같은 분이면 다음 날")
        self.assertEqual(core.parse_seq_start("08:05", now), dt.datetime(2026, 11, 17, 8, 5))
        self.assertEqual(core.parse_seq_start("23:59:00", now), dt.datetime(2026, 11, 16, 23, 59))
        self.assertEqual(core.parse_seq_start("2026-11-20 10:00", now), dt.datetime(2026, 11, 20, 10, 0))
        self.assertEqual(core.parse_seq_start("2026/11/20 10:00:00", now), dt.datetime(2026, 11, 20, 10, 0))
        for bad in ("24:00", "10:61", "10시", "soon", "2026-11-20", "2026-11-20 10:00:30", "10:00:30"):
            with self.assertRaises(ValueError):
                core.parse_seq_start(bad, now)

    def test_format_for_saving(self):
        self.assertEqual([core.fmt_seq_start(x) for x in ("", " 자동 ", "9:05", "09:05:00", "2026/11/20 10:00")],
                         ["", "", "09:05", "09:05", "2026-11-20 10:00:00"])
        with self.assertRaises(ValueError):
            core.fmt_seq_start("25:00")

    def test_plan_carries_the_start_times(self):
        """잡이 있는 케이스는 모두 plan 에 START 줄이 있다(적은 시각 그대로. tcrun 은 이 시각을 변경하지 않는다). SEG 줄은 설정이나
        VM 전원이 앞 케이스와 다른 케이스(1)와 같은 케이스(0)를 가른다."""
        cases = [case("first", seq_start="15:00"), case("second", seq_start="+2:00"), case("third", seq_start="2026-11-17 08:00")]
        res = gen(cases)
        self.assertEqual(res["errors"], [])
        t1, t2, t3 = dt.datetime(2026, 11, 16, 15, 0), dt.datetime(2026, 11, 16, 17, 0), dt.datetime(2026, 11, 17, 8, 0)
        lines = res["plan"].split("\n")
        self.assertIn("START 001 %d 2026-11-16 15:00" % int(time.mktime(t1.timetuple())), lines)
        self.assertIn("START 002 %d 2026-11-16 17:00" % int(time.mktime(t2.timetuple())), lines)
        self.assertIn("START 003 %d 2026-11-17 08:00" % int(time.mktime(t3.timetuple())), lines)
        self.assertEqual([x for x in lines if x.startswith("SEG ")], ["SEG 001 1", "SEG 002 0", "SEG 003 0"])
        self.assertLess(lines.index("SEG 001 1"), lines.index("START 001 %d 2026-11-16 15:00" % int(time.mktime(t1.timetuple()))))
        view = res["plan_view"]
        self.assertIn("1. first   잡 시작 2026-11-16 15:00\n", view)
        self.assertIn("2. second   잡 시작 2026-11-16 17:00 (윗줄 +2:00)\n", view)
        self.assertIn("   설정과 VM 전원: 앞 케이스와 같다. 변경하지 않는다", view)

    def test_errors_and_warnings(self):
        res = gen([case("bad", seq_start="soon")])
        self.assertTrue(any("bad" in e and "시작 시각" in e and "형식" in e for e in res["errors"]), res["errors"])
        res = gen([case("past", seq_start="2026-11-16 14:00")])                     # NOW 는 2026-11-16 14:30
        self.assertEqual(res["errors"], ["1번째 케이스 past: 시작 시각 2026-11-16 14:00:00 이(가) 이미 지났음 (지금 2026-11-16 14:30:00). "
                                         "시각을 고칠 것"])
        res = gen([case("same-minute", seq_start="2026-11-16 14:30")])
        self.assertTrue(any("이미 지났음" in e for e in res["errors"]))
        # 잡이 있는 케이스는 시작 시각이 있어야 한다. '자동'(프로세스 확인 방식)은 v2.12 에서 뺐다
        res = core.generate([case("empty")], RUN, MON, settings(), now=NOW, write=False, profiles=PROF)
        self.assertEqual(res["errors"], ["1번째 케이스 empty: 시작 시각 칸이 비어 있음. 시각(예: 2026-11-20 10:00)이나 +간격(예: +10:00)을 적을 것"])
        res = gen([case("auto", seq_start="자동")])
        self.assertEqual(res["errors"], ["1번째 케이스 auto: 시작 시각 칸의 '자동' 은 쓸 수 없음. 시각(예: 2026-11-20 10:00)이나 +간격(예: "
                                         "+10:00)을 적을 것"])
        # 같은 ESX 호스트의 케이스는 표의 순서가 시작 시각 순서여야 한다. ESX 호스트가 다르면 비교하지 않는다
        res = gen([case("a", seq_start="18:00"), case("b", seq_start="17:00"), case("c", esx="esx02", seq_start="16:00")])
        self.assertEqual(res["errors"], ["2번째 케이스 b: 시작 시각 2026-11-16 17:00:00 이(가) 같은 ESX 호스트의 앞 케이스(2026-11-16 18:00:00)"
                                         "보다 빠름. 표의 순서를 시작 시각 순서로 맞출 것"])
        # 잡이 없는 케이스(설정과 VM 전원만): 시각을 적으면 그 시각에, 비우면 다음 잡 케이스의 설정 적용 때 한다
        setonly = [vm("esx01ev01", "(없음)", [], ["pin"])]
        res = gen([case("set-only", vms=setonly, seq_start="18:00")])
        self.assertEqual((res["errors"], res["warnings"]), ([], []))
        self.assertNotIn("START ", res["plan"])
        self.assertIn("APPLYAT 001 %d 2026-11-16 18:00\n" % int(time.mktime(dt.datetime(2026, 11, 16, 18, 0).timetuple())), res["plan"])
        res = gen([case("set-first", vms=setonly), case("job", seq_start="20:00")])
        self.assertEqual(res["errors"], [])
        self.assertIn("APPLYAT 001 %d 2026-11-16 19:30\n" % int(time.mktime(dt.datetime(2026, 11, 16, 19, 30).timetuple())), res["plan"],
                      "비우면 다음 잡 케이스의 잡 시작 30분 전")
        self.assertIn("1. set-first   잡 없음. 설정과 VM 전원만 맞춘다\n", res["plan_view"])
        res = gen([case("job", seq_start="20:00"), case("set-last", vms=setonly)])
        self.assertEqual(res["errors"], ["2번째 케이스 set-last: 잡이 없는 케이스이고 뒤에 잡이 있는 케이스도 없음. 시작 시각 칸에 설정을 적용할 "
                                         "시각을 적을 것"])
        res = gen([case("job", seq_start="20:00"), case("set-early", vms=setonly, seq_start="20:01")])
        self.assertTrue(any(e.startswith("2번째 케이스 set-early: 설정을 적용할 시각 11-16 20:01 이(가) 앞 케이스 job 의 잡 시작 11-16 20:00 보다 빠름")
                            for e in res["errors"]), res["errors"])
        applied = gen([case("done", seq_start="2026-11-16 09:00", applied=True), case("next")])
        self.assertEqual(applied["errors"], [], "적용 체크한 케이스의 지난 시각은 보지 않음")
        for e in (gen([case("bad", seq_start="soon")])["errors"] + gen([case("past", seq_start="2026-11-16 14:00")])["errors"]):
            self.assertNotIn("\n", e, "오류는 한 줄")

    def test_start_times_nearer_than_the_spacing_are_an_error(self):
        """모든 잡 시작은 서로 '잡 시작 최소 간격'(기본 5분) 이상 떨어진다: ESX 호스트가 달라도 더 가까운 시작 시각은 오류."""
        res = gen([case("a1", seq_start="18:00"), case("b1", esx="esx02", seq_start="18:00")])
        self.assertTrue(any(e.startswith("2번째 케이스 b1") and "1번째 케이스 a1 의 시작 시각 2026-11-16 18:00:00 과 5분 이상 떨어져 있지 않음" in e
                            for e in res["errors"]), res["errors"])
        res = gen([case("a2", seq_start="18:00"), case("b2", seq_start="2026-11-16 18:00:00")])
        self.assertTrue(any("5분 이상 떨어져 있지 않음" in e for e in res["errors"]), "같은 ESX 호스트도 오류. 적는 형식이 달라도 같은 시각")
        res = gen([case("a3", seq_start="18:00"), case("b3", esx="esx02", seq_start="18:04")])
        self.assertTrue(any("5분 이상 떨어져 있지 않음" in e for e in res["errors"]), "4분 차이는 오류")
        res = gen([case("a3", seq_start="18:00"), case("b3", esx="esx02", seq_start="18:05")])
        self.assertEqual(res["errors"], [], "5분 차이는 됨")
        self.assertIn("SET SPACING_MIN 5\n", res["plan"])
        res = gen([case("a5", seq_start="18:00"), case("b5", esx="esx02", seq_start="18:05")], st=settings(seq_start_spacing_min=10))
        self.assertTrue(any("10분 이상 떨어져 있지 않음" in e for e in res["errors"]), "간격 설정을 따른다")
        res = gen([case("a4", seq_start="2026-11-16 18:00:00", applied=True), case("b4", esx="esx02", seq_start="18:00")])
        self.assertEqual(res["errors"], [], "적용 체크한 케이스는 보지 않음")
        res = gen([case("a6")], st=settings(seq_start_spacing_min=0))
        self.assertTrue(any("'잡 시작 최소 간격' 은(는) 1 이상의 정수" in e for e in res["errors"]), "간격은 1분 이상")

    def test_power_off_vm(self):
        """VM 행의 전원 칸이 끔이면 그 VM 은 케이스 동안 끈다. plan 에 OFF 줄, 실행 계획에 전원 줄."""
        c = case("off1", vms=[vm("esx01ev01", "job_a"), vm("esx01ev02", power="off")])
        res = gen([c])
        self.assertEqual(res["errors"], [])
        self.assertIn("VM 001 esx01ev02 esx01ev02\nOFF 001 esx01ev02\n", res["plan"])
        self.assertIn("   VM 전원: esx01ev01 켬, esx01ev02 끔", res["plan_view"])
        bad = gen([case("off2", vms=[vm("esx01ev01", "job_a", power="off")])])
        self.assertTrue(any("전원 끔인데 잡이나 모니터링이 있음" in e for e in bad["errors"]), bad["errors"])
        noesx = gen([case("off3", esx="", vms=[vm("vm-a", "job_a"), vm("vm-b", power="off")])])
        self.assertTrue(any("VM 전원 끔에는 ESX 호스트가 있어야" in e for e in noesx["errors"]), noesx["errors"])
        only = gen([case("off4", vms=[vm("esx01ev01", power="off")], seq_start="18:00")])
        self.assertEqual(only["errors"], [], "끌 VM 만 있는 케이스도 된다(적은 시각에 VM 을 끈다)")
        self.assertIn("1. off4   잡 없음. 설정과 VM 전원만 맞춘다\n", only["plan_view"])
        self.assertIn("   설정과 VM 전원: 실행을 시작할 때 호스트와 비교한다. 다르면 11-16 18:00 에 VM 종료, 설정 적용, VM 전원 ON",
                      only["plan_view"])
        # 전원이 앞 케이스와 다르면 설정이 다른 케이스로 본다(SEG 1)
        two = gen([case("p1", vms=[vm("esx01ev01", "job_a"), vm("esx01ev02")], seq_start="18:00"),
                   case("p2", vms=[vm("esx01ev01", "job_a"), vm("esx01ev02", power="off")], seq_start="+2")])
        self.assertEqual([x for x in two["plan"].split("\n") if x.startswith("SEG ")], ["SEG 001 1", "SEG 002 1"])

    def test_settings_change_time(self):
        """설정이나 VM 전원이 호스트와 다른 케이스는 잡 시작 '설정 적용 N분 전'(기본 30. 사용자 결정 2026-11-18 "60분전에 설정
        적용이 아니고 30분으로")에 VM 종료, 설정 적용, VM 전원 ON 을 한다. 설정이 앞 케이스와 같은 케이스(SEG 0)는 변경하지 않는다."""
        pin = [vm("esx01ev01", "job_a", [], ["pin"])]
        cases = [case("p1", seq_start="20:00"), case("p2", seq_start="+2"), case("p3", seq_start="+2", vms=pin),
                 case("p4", seq_start="+2", vms=pin), case("p5", seq_start="+2"), case("q1", esx="", vms=[vm("bm1", "job_a")], seq_start="+2"),
                 case("q2", esx="", vms=[vm("bm2", "job_a")], seq_start="+2")]
        res = gen(cases)
        self.assertEqual(res["errors"], [])
        self.assertIn("SET PREP_LEAD_MIN 30\n", res["plan"])
        self.assertEqual([x for x in res["plan"].split("\n") if x.startswith("SEG ")],
                         ["SEG 001 1", "SEG 002 0", "SEG 003 1", "SEG 004 0", "SEG 005 1", "SEG 006 1", "SEG 007 0"],
                         "ESX 호스트의 첫 케이스와 설정이 앞 케이스와 다른 케이스가 1. ESX 호스트가 없는 케이스는 첫 케이스만 1")
        view = res["plan_view"]
        self.assertIn("   설정과 VM 전원: 실행을 시작할 때 호스트와 비교한다. 다르면 11-16 19:30 에 VM 종료, 설정 적용, VM 전원 ON", view)
        self.assertIn("   설정과 VM 전원: 앞 케이스와 다르다. 11-16 23:30 에 호스트와 비교하고, 다르면 VM 종료, 설정 적용, VM 전원 ON", view)
        self.assertEqual(view.count("   설정과 VM 전원: 앞 케이스와 같다. 변경하지 않는다"), 2)
        self.assertEqual([x["text"] for x in res["changes"]["first"]], ["esx01 p1 (11-16 19:30)"])
        self.assertEqual([x["text"] for x in res["changes"]["diff"]], ["esx01 p3 (11-16 23:30)", "esx01 p5 (11-17 03:30)"])
        res = gen(cases, st=settings(seq_prep_lead_min=45))
        self.assertIn("SET PREP_LEAD_MIN 45\n", res["plan"])
        self.assertIn("다르면 11-16 19:15 에 VM 종료", res["plan_view"])
        # 설정이 앞 케이스와 다른 케이스: VM 을 종료하는 시각(잡 시작 30분 전)이 앞 케이스의 잡 시작보다 늦어야 한다
        close = gen([case("c1", seq_start="18:00"), case("c2", seq_start="18:30", vms=pin)])
        self.assertEqual(close["errors"], ["2번째 케이스 c2: 시작 시각을 11-16 18:32 이후로 고칠 것. 설정이 앞 케이스와 달라 잡 시작 30분 전에 "
                                           "VM 을 종료하는데, 그 시각이 앞 케이스 c1 의 잡 시작(11-16 18:00)보다 늦지 않음"])
        self.assertEqual(gen([case("c1", seq_start="18:00"), case("c2", seq_start="18:32", vms=pin)])["errors"], [])
        self.assertEqual(gen([case("c1", seq_start="18:00"), case("c2", seq_start="18:30")])["errors"], [],
                         "설정이 같은 케이스는 VM 을 종료하지 않으므로 보지 않음")

    def test_relative_start_time(self):
        """시작 시각 칸의 '+간격': 표의 바로 윗줄 케이스의 시작 시각에서 그만큼 뒤(사용자 결정 2026-11-18 "그냥 헤깔리니까 바로
        윗줄로할래"). ESX 호스트가 달라도 바로 윗줄이 기준이다."""
        self.assertEqual([core.fmt_seq_start(t) for t in ("+8.0", "+8", "+8:30", "+8.5", " + 12:00 ", "+0:45")],
                         ["+8:00", "+8:00", "+8:30", "+8:30", "+12:00", "+0:45"])
        for bad in ("+0", "+0:00", "+8:75", "+x", "+8.", "+-1", "+"):
            self.assertRaises(ValueError, core.fmt_seq_start, bad)
        self.assertIsNone(core.parse_seq_offset("10:00"))
        self.assertIsNone(core.parse_seq_offset(""))
        cases = [case("a1", seq_start="18:00"), case("b1", esx="esx02", seq_start="18:05"), case("a2", seq_start="+8.0"),
                 case("b2", esx="esx02", seq_start="+8"), case("a3", seq_start="+1:30")]
        res = gen(cases)
        self.assertEqual(res["errors"], [])
        starts = dict((pc["name"], pc["start_at"]) for pc in res["cases"])
        self.assertEqual(starts["a2"], dt.datetime(2026, 11, 17, 2, 5), "바로 윗줄(b1 18:05) + 8시간. ESX 호스트가 달라도 윗줄이 기준")
        self.assertEqual(starts["b2"], dt.datetime(2026, 11, 17, 10, 5), "바로 윗줄(a2 02:05) + 8시간")
        self.assertEqual(starts["a3"], dt.datetime(2026, 11, 17, 11, 35))
        self.assertIn("START 003 %d 2026-11-17 02:05\n" % int(time.mktime(starts["a2"].timetuple())), res["plan"])
        self.assertIn("1. a2   잡 시작 2026-11-17 02:05 (윗줄 +8:00)\n", res["plan_view"].split("[ESX 호스트 esx01]")[1].replace("2. a2", "1. a2"))
        first = gen([case("x1", seq_start="+8")])
        self.assertEqual(first["errors"], ["1번째 케이스 x1: 시작 시각 '+8': 표의 첫 줄에는 +간격을 쓸 수 없음. 날짜 시각(예: 2026-11-20 10:00)을 "
                                           "적을 것"])
        # 바로 윗줄이 적용 체크한 케이스여도 그 시작 시각이 기준이다(실행한 케이스 아래에 케이스를 이어 적는 흐름)
        applied = gen([case("x0", seq_start="2026-11-16 18:00:00", applied=True), case("x1", seq_start="+8")])
        self.assertEqual(applied["errors"], [])
        self.assertEqual(applied["cases"][0]["start_at"], dt.datetime(2026, 11, 17, 2, 0))
        noat = gen([case("y0", vms=[vm("esx01ev01", "(없음)", [], ["pin"])]), case("y1", seq_start="+8")])
        self.assertEqual(noat["errors"], ["2번째 케이스 y1: 시작 시각 '+8' 을(를) 계산할 수 없음. 바로 윗줄 케이스의 시작 시각이 없음"])
        near = gen([case("z0", seq_start="18:00"), case("z1", esx="esx02", seq_start="17:58"), case("z2", esx="esx02", seq_start="+0:04")])
        self.assertTrue(any("5분 이상 떨어져 있지 않음" in e for e in near["errors"]), "계산한 시각에도 잡 시작 최소 간격을 본다")
        text = core.cases_to_csv([case("r1", seq_start="+8:00")])
        self.assertEqual(core.cases_from_csv(text)[0]["seq_start"], "+8:00", "테스트 케이스 파일에 적은 그대로 저장")

    def test_table_starts(self):
        """표의 줄마다 잡 시작 시각(탭 4 의 '잡 시작 (환산)' 칸. 사용자 2026-11-18 "시작시간 +10 +8 등으로 입력하면 옆칸에 시작 시각
        환산되어서 나오면 좋겠고")."""
        cases = [case("r1", seq_start="2026-11-20 10:00:00"), case("r2", seq_start="+10:00"), case("r3", seq_start="+0:30"),
                 case("h1", esx="esx02", seq_start="+0:05"), case("e1", esx="esx03"), case("e2", esx="esx03", seq_start="+1"),
                 case("s1", vms=[vm("esx01ev01", "(없음)", [], ["pin"])], seq_start="2026-11-21 08:00:00"), case("s2", seq_start="+2"),
                 case("bad", seq_start="soon"), case("auto", seq_start="자동")]
        ts = core.table_starts(cases)
        self.assertEqual([t["at"] for t in ts],
                         [dt.datetime(2026, 11, 20, 10, 0), dt.datetime(2026, 11, 20, 20, 0), dt.datetime(2026, 11, 20, 20, 30),
                          dt.datetime(2026, 11, 20, 20, 35), None, None, dt.datetime(2026, 11, 21, 8, 0), dt.datetime(2026, 11, 21, 10, 0),
                          None, None])
        self.assertEqual([t["rel"] for t in ts], ["", "+10:00", "+0:30", "+0:05", "", "", "", "+2:00", "", ""])
        self.assertEqual([t["job"] for t in ts], [True, True, True, True, True, True, False, True, True, True])
        self.assertIn("칸이 비어 있음", ts[4]["why"])
        self.assertIn("바로 윗줄 케이스의 시작 시각이 없음", ts[5]["why"])
        self.assertIn("형식", ts[8]["why"])
        self.assertIn("'자동' 은 쓸 수 없음", ts[9]["why"])
        for t in ts:
            self.assertNotIn("\n", t["why"])
        # 시:분만 적은 칸: 생성할 때(generate) 적용 체크하지 않은 케이스는 지금 뒤에 처음 오는 그 시각이다
        hm = [case("t1", seq_start="18:00"), case("t2", seq_start="+10:00")]
        self.assertEqual([t["at"] for t in core.table_starts(hm, NOW, generate=True)],
                         [dt.datetime(2026, 11, 16, 18, 0), dt.datetime(2026, 11, 17, 4, 0)])
        self.assertEqual([t["at"] for t in core.table_starts(hm)], [None, None], "지금 시각을 주지 않으면 날짜를 알 수 없음")
        self.assertIn("날짜가 없음. 날짜까지 적을 것(예: 2026-11-20 18:00)", core.table_starts(hm)[0]["why"])
        # 생성한 뒤에는 테스트 기록 CSV 의 마지막 생성 기록에서 날짜를 찾는다(시:분이 같을 때만)
        rec = [{"_name": "t1", "_seq": True, "잡 시작(KST)": "2026-11-18 09:00:00"},
               {"_name": "t1", "_seq": True, "잡 시작(KST)": "2026-11-19 18:00:00"},
               {"_name": "t1", "_seq": False, "잡 시작(KST)": "2026-11-21 18:00:00"}]
        self.assertEqual([t["at"] for t in core.table_starts(hm, None, rec)], [dt.datetime(2026, 11, 19, 18, 0), dt.datetime(2026, 11, 20, 4, 0)])
        hm[0]["seq_start"] = "19:00"
        self.assertEqual(core.table_starts(hm, None, rec)[0]["at"], None, "기록의 시:분이 칸과 다르면 쓰지 않음")

    def test_report_schedule(self):
        """리포트용 CSV(사용자 2026-11-18 "+10:00 헀으면 리포트용 csv 저장하면 +10 시간 반영해서 적용이 되어야"): 잡 시작은
        시작 시각 칸으로 계산한 시각이다(table_starts 와 같은 값)."""
        cases = [case("r1", seq_start="2026-11-20 10:00:00"), case("r2", seq_start="+10:00"), case("r3", seq_start="+0:30"),
                 case("h1", esx="esx02", seq_start="2026-11-20 10:05:00"), case("h2", esx="esx02", seq_start="+10"),
                 case("e1", esx="esx04"), case("s1", esx="esx05", vms=[vm("esx05ev01", "(없음)", [], ["pin"])], seq_start="2026-11-22 18:00:00")]
        sched = core.seq_report_sched(cases)
        self.assertEqual([s["job"] for s in sched],
                         [dt.datetime(2026, 11, 20, 10, 0), dt.datetime(2026, 11, 20, 20, 0), dt.datetime(2026, 11, 20, 20, 30),
                          dt.datetime(2026, 11, 20, 10, 5), dt.datetime(2026, 11, 20, 20, 5), None, None])
        self.assertIn("시작 시각 칸이 비어 있음", sched[5]["start_error"])
        self.assertIn("잡이 없는 케이스", sched[6]["start_error"])
        rows, errors, warnings = core.report_rows(cases, sched, [0, 1, 2, 4])
        self.assertEqual(errors, [])
        self.assertEqual([(r[1], r[4]) for r in rows], [("r1", "2026-11-20 10:00:00"), ("r2", "2026-11-20 20:00:00"),
                                                        ("r3", "2026-11-20 20:30:00"), ("h2", "2026-11-20 20:05:00")])
        rows, errors, warnings = core.report_rows(cases, sched, [5])
        self.assertEqual(len(errors), 1)
        self.assertIn("e1", errors[0])
        self.assertIn("시작 시각 칸이 비어 있음", errors[0])
        # 생성한 결과와 같은 값이다
        res = gen([case("g1", seq_start="2026-11-20 18:00:00"), case("g2", seq_start="+10:00")])
        self.assertEqual(res["errors"], [])
        got = core.seq_report_sched([case("g1", seq_start="2026-11-20 18:00:00"), case("g2", seq_start="+10:00")])
        self.assertEqual([s["job"] for s in got], [pc["start_at"] for pc in res["cases"]])

    def test_monitoring_stop_times(self):
        """모니터링 중지(중지 명령이 있는 모니터링. 사용자 결정 2026-11-09): 자동은 같은 ESX 호스트의 다음 잡 케이스의 잡 시작 10분 전
        ('모니터링 중지 N분 전'). 최대 대기를 적었으면 잡 시작 + 최대 대기가 더 이를 때 그 시각. 마지막 케이스는 최대 대기를 적었을 때만.
        plan 의 MSTOP 줄이고 tcrun 이 잡과 함께 crontab 에 등록한다. 잡이 실행되는 동안 VM 에 접속하지 않는다."""
        gp = [vm("esx01ev01", "job_a", ["guest_perf"])]
        cases = [case("a1", seq_start="18:00", esx_monitors=["host_mon"], vms=gp),
                 case("a2", seq_start="+8"), case("a3", seq_start="+8", max_wait="7:00", vms=gp),
                 case("b1", esx="esx02", seq_start="18:05", max_wait="2:00", vms=[vm("esx02ev01", "job_a", ["guest_perf"])]),
                 case("b2", esx="esx02", seq_start="23:00", vms=[vm("esx02ev01", "job_a", ["guest_nostop"])])]
        res = gen(cases)
        self.assertEqual((res["errors"], res["warnings"], res["notes"]), ([], [], []))
        lines = res["plan"].split("\n")

        def ep(*a):
            return int(time.mktime(dt.datetime(*a).timetuple()))
        self.assertIn("MSTOP 001 %d 2026-11-17 01:50" % ep(2026, 11, 17, 1, 50), lines, "다음 잡 케이스 a2(02:00)의 10분 전")
        self.assertEqual([x for x in lines if x.startswith("MSTOP 002")], [], "모니터링이 없는 케이스")
        self.assertIn("MSTOP 003 %d 2026-11-17 17:00" % ep(2026, 11, 17, 17, 0), lines, "마지막 케이스: 잡 시작 10:00 + 최대 대기 7:00")
        self.assertIn("MSTOP 004 %d 2026-11-16 20:05" % ep(2026, 11, 16, 20, 5), lines,
                      "최대 대기(18:05 + 2:00)가 다음 케이스의 10분 전(22:50)보다 이르면 그 시각")
        self.assertEqual([x for x in lines if x.startswith("MSTOP 005")], [], "중지 명령이 없는 모니터링")
        self.assertEqual([pc["mstop"] for pc in res["cases"]],
                         [dt.datetime(2026, 11, 17, 1, 50), None, dt.datetime(2026, 11, 17, 17, 0), dt.datetime(2026, 11, 16, 20, 5), None])
        view = res["plan_view"]
        self.assertIn("   모니터링 중지 (11-17 01:50):", view)
        self.assertIn("   모니터링 중지 (11-17 17:00):", view)
        self.assertIn("   모니터링 중지: 중지 명령 없음", view)
        last = gen([case("k1", seq_start="18:00", vms=gp)])
        self.assertNotIn("\nMSTOP ", last["plan"])
        self.assertIn("   모니터링 중지: 자동으로 중지하지 않음(마지막 케이스. 모니터링 중지 칸에서 직접 입력을 고르거나 '최대 대기' 를 "
                      "적으면 그 시각에 중지)", last["plan_view"])
        self.assertEqual(len(last["notes"]), 1)
        close = gen([case("c1", seq_start="18:00", vms=gp), case("c2", seq_start="18:10")])
        self.assertEqual(close["errors"], ["1번째 케이스 c1: 다음 케이스의 시작 시각을 늦추거나 모니터링 중지 칸을 고칠 것. 자동 중지 시각 "
                                           "11-16 18:00 이(가) 이 케이스의 잡 시작 11-16 18:00 보다 빠르거나 같음"])
        self.assertEqual(gen([case("c1", seq_start="18:00", vms=gp), case("c2", seq_start="18:12")])["errors"], [])

    def test_records_hold_the_planned_times(self):
        """테스트 기록 CSV: 케이스마다 계획한 잡 시작, 모니터링 시작, 모니터링 중지를 적는다. +간격을 적은 케이스는 간격 열에 그 값."""
        def mon_vm(esx):
            return [vm(esx + "ev01", "job_a", ["guest_perf"])]
        cases = [case("a1", seq_start="18:00", esx_monitors=["host_mon"], vms=mon_vm("esx01")),
                 case("a2", seq_start="+8"),
                 case("a3", seq_start="+8", max_wait="7:00", vms=mon_vm("esx01")),
                 case("b1", esx="esx02", seq_start="2026-11-16 18:05:00", vms=mon_vm("esx02"))]
        d = tempfile.mkdtemp(prefix="tcgen_rec_")
        try:
            st = settings(record_csv=os.path.join(d, "rec.csv"), log_dir=d)
            res = core.generate(cases, RUN, MON, st, now=NOW, write=True, profiles=PROF, backup_dir=os.path.join(d, "bk"))
            self.assertEqual(res["errors"], [])
            with open(st["record_csv"], encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            got = {}
            for r in rows[1:]:
                row = dict(zip(rows[0], r))
                got[row["테스트 케이스 이름"]] = (row["잡 시작(KST)"], row["간격"], row["모니터링 시작(KST)"], row["모니터링 중지(KST)"])
            self.assertEqual(got["a1"], ("2026-11-16 18:00:00", "", "2026-11-16 17:55:00", "2026-11-17 01:50:00"),
                             "모니터링 중지 = 다음 케이스 a2(02:00)의 10분 전")
            self.assertEqual(got["a2"], ("2026-11-17 02:00:00", "+8:00", "", ""), "모니터링이 없는 케이스는 잡 시작만")
            self.assertEqual(got["a3"], ("2026-11-17 10:00:00", "+8:00", "2026-11-17 09:55:00", "2026-11-17 17:00:00"),
                             "마지막 케이스: 모니터링 중지 = 잡 시작 + 최대 대기 7:00")
            self.assertEqual(got["b1"], ("2026-11-16 18:05:00", "", "2026-11-16 18:00:00", ""), "마지막 케이스이고 최대 대기가 없음")
            with open(res["log"], encoding="utf-8-sig", newline="") as f:
                log = list(csv.reader(f))
            when = {}
            for r in log[1:]:
                e = dict(zip(log[0], r))
                when.setdefault((e["테스트 케이스 이름"], e["구분"]), set()).add(e["실행 시각(KST)"])
            self.assertEqual(when[("a1", "잡")], {"2026-11-16 18:00:00"})
            self.assertEqual(when[("a1", "모니터링 시작")], {"2026-11-16 17:55:00"})
            self.assertEqual(when[("a1", "모니터링 중지")], {"2026-11-17 01:50:00"})
            self.assertEqual(when[("b1", "모니터링 중지")], {""})
            self.assertEqual(when[("a2", "잡")], {"2026-11-17 02:00:00"})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_time_mode_ignores_the_start_time_of_sequence_mode(self):
        c = case("t1", seq_start="soon")
        c.update(start="2026-11-20 10:00:00")
        res = gen([c], st=settings(run_mode="time"))
        self.assertEqual(res["errors"], [], "시각 지정에서는 순서 실행의 시작 시각 칸을 쓰지 않음")

    def test_case_field_defaults(self):
        self.assertEqual(core.new_case("esx01", "", 1)["seq_start"], "")
        self.assertEqual(core._clean_case({"name": "x", "vms": []})["seq_start"], "", "예전 파일에는 이 칸이 없음")
        self.assertEqual(core._clean_case({"name": "x", "vms": [], "seq_start": None})["seq_start"], "")



class OpsSettingsTests(unittest.TestCase):
    """VCF Operations 주소, 계정, 암호(사용자 결정 2026-11-18 "생성기에서입력하고 싶어 ops 주소 계정 암호", 암호는
    guest_config_push 와 같은 방식으로 암호화). plan 의 OPS 줄과 타임라인."""

    @unittest.skipUnless(shutil.which("openssl"), "openssl 필요")
    def test_encrypted_password_is_decrypted_by_openssl(self):
        """암호화한 글은 guest_config_push 와 tcrun 이 쓰는 openssl 명령으로 풀린다."""
        for pw in ("a", "Pw-12345_!@#$%^&*()", "x" * 16, "p" * 77, "따옴표 \" 역슬래시 \\ 작은따옴표 ' 달러 $"):
            enc, key = core.ops_encrypt(pw)
            self.assertRegex(enc, r"^[A-Za-z0-9+/=]+$")
            self.assertRegex(key, r"^[0-9a-f]{32}$")
            p = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-pass", "pass:" + key,
                                "-base64", "-A"], input=enc.encode("ascii"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual((p.returncode, p.stdout.decode("utf-8")), (0, pw), p.stderr)
        a, b = core.ops_encrypt("same"), core.ops_encrypt("same")
        self.assertNotEqual(a, b, "암호화할 때마다 키와 salt 가 새로 정해진다")

    def test_aes_block_matches_fips197(self):
        """AES-256 한 블록: FIPS 197 부록 C.3 의 값."""
        key = bytes(bytearray(range(32)))
        block = list(bytearray.fromhex("00112233445566778899aabbccddeeff"))
        out = bytes(bytearray(core._aes_encrypt_block(block, core._aes_round_keys(key))))
        self.assertEqual(out.hex(), "8ea2b7ca516745bfeafc49904b496089")

    def test_plan_lines_and_timeline(self):
        enc, key = core.ops_encrypt("pw")
        st = settings(ops_host=" ops.lab.local ", ops_user="admin", ops_pass_enc=enc, ops_enc_key=key)
        res = gen([case("c1", vms=[vm("esx01ev01", "job_a", [], ["pin"])])], st=st)
        self.assertEqual(res["errors"], [])
        lines = res["plan"].split("\n")
        self.assertEqual([x for x in lines if x.startswith("OPS ")],
                         ["OPS HOST ops.lab.local", "OPS USER admin", "OPS ENC " + enc, "OPS KEY " + key, "OPS TLS 0"])
        self.assertLess(lines.index("OPS HOST ops.lab.local"), lines.index("CASE 001 esx01 c1"))
        self.assertIn("  T-10분 : 구성 스냅샷, VM 설정을 Operations 에 기록\n", res["plan_view"])
        self.assertIn("Operations 기록: ops.lab.local (계정 admin). 구성 스냅샷 때 VM 설정은 VM 객체에, 케이스의 호스트 옵션은 호스트 객체에 기록",
                      res["plan_view"])
        self.assertEqual(res["timeline_table"]["rows"][0][1][2], "구성 스냅샷, VM 설정을 Operations 에 기록")
        self.assertNotIn("pw\n", res["plan"])
        self.assertIn("OPS TLS 1", gen([case("c1")], st=dict(st, ops_verify_tls=True))["plan"])
        # 주소를 비우면 기록하지 않는다: OPS 줄이 없고 다른 값은 보지 않는다
        off = gen([case("c1")], st=settings(ops_host="", ops_user="admin", ops_pass_enc="", ops_enc_key=""))
        self.assertEqual(off["errors"], [])
        self.assertNotIn("\nOPS ", off["plan"])
        self.assertNotIn("Operations", off["plan_view"])

    def test_errors_name_the_fields_of_the_screen(self):
        enc, key = core.ops_encrypt("pw")
        ok = dict(ops_host="ops.lab.local", ops_user="admin", ops_pass_enc=enc, ops_enc_key=key)
        for over, want in ((dict(ops_host="bad host"), "설정: 'Operations 주소' 는 이름이나 IP 로 적을 것 (영문, 숫자, . - : 만 사용): 'bad host'"),
                           (dict(ops_host="ops;rm"), "설정: 'Operations 주소' 는 이름이나 IP 로 적을 것 (영문, 숫자, . - : 만 사용): 'ops;rm'"),
                           (dict(ops_user=""), "설정: 'Operations 계정' 을 적을 것 ('Operations 주소' 를 비우면 Operations 에 기록하지 않음)"),
                           (dict(ops_user="a b"), "설정: 'Operations 계정' 에는 공백과 한글을 쓸 수 없음: 'a b'"),
                           (dict(ops_pass_enc=""), "설정: 'Operations 암호' 를 입력할 것 ('Operations 주소' 를 비우면 Operations 에 기록하지 않음)"),
                           (dict(ops_enc_key="k y"), "설정: 저장된 'Operations 암호' 를 읽을 수 없음. 암호를 다시 입력할 것")):
            res = gen([case("c1")], st=settings(**dict(ok, **over)))
            self.assertEqual(res["errors"], [want], over)
            self.assertEqual(res["plan"], "")
        for host in ("10.0.0.5", "ops.lab.local:8443", "https://ops.lab.local", "http://127.0.0.1:8080"):
            self.assertEqual(gen([case("c1")], st=settings(**dict(ok, ops_host=host)))["errors"], [], host)

    def test_settings_file_keeps_only_the_encrypted_password(self):
        d = tempfile.mkdtemp(prefix="tcgen_ops_")
        try:
            s = core.load_settings(d)
            self.assertEqual((s["ops_host"], s["ops_user"], s["ops_pass_enc"], s["ops_enc_key"], s["ops_verify_tls"]), ("", "", "", "", False))
            enc, key = core.ops_encrypt("Secret-Pw-1")
            s.update(ops_host="ops.lab.local", ops_user="admin", ops_pass_enc=enc, ops_enc_key=key)
            core.save_settings(d, core.resolve_paths(d, s))
            with open(os.path.join(d, core.SETTINGS_FILE), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("Secret-Pw-1", text)
            s2 = core.load_settings(d)
            self.assertEqual((s2["ops_host"], s2["ops_user"], s2["ops_pass_enc"], s2["ops_enc_key"]), ("ops.lab.local", "admin", enc, key))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_records_do_not_hold_the_operations_values(self):
        """테스트 기록 CSV 와 로그 CSV 에는 Operations 주소, 계정, 암호가 들어가지 않는다."""
        d = tempfile.mkdtemp(prefix="tcgen_ops_")
        try:
            enc, key = core.ops_encrypt("pw")
            st = settings(record_csv=os.path.join(d, "rec.csv"), log_dir=d, ops_host="ops.lab.local", ops_user="opsadmin",
                          ops_pass_enc=enc, ops_enc_key=key)
            res = core.generate(with_starts([case("c1")]), RUN, MON, st, now=NOW, write=True, profiles=PROF, backup_dir=os.path.join(d, "bk"))
            self.assertEqual(res["errors"], [])
            for p in (st["record_csv"], res["log"]):
                with open(p, encoding="utf-8-sig") as f:
                    text = f.read()
                for word in ("ops.lab.local", "opsadmin", enc, key):
                    self.assertNotIn(word, text, p)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class CaseFileTests(unittest.TestCase):
    """테스트 케이스 파일: 탭 4 의 표를 사용자가 정한 파일에 저장하고 불러온다. 정하지 않으면 데이터 폴더의 test_cases.json 을 쓴다.
    명령과 프로파일과 나머지 설정은 어느 파일을 쓰든 같이 쓴다. 기록 CSV 와 로그 폴더는 파일마다 마지막 값을 기억한다."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tcgen_cf_")
        self.ext = tempfile.mkdtemp(prefix="tcgen_cf_ext_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.ext, ignore_errors=True)

    def raw(self):
        with open(os.path.join(self.d, core.SETTINGS_FILE), encoding="utf-8") as f:
            return json.load(f)

    def save(self, st):
        core.save_settings(self.d, core.resolve_paths(self.d, st))

    def spr_case(self):
        c = case("spr-base", esx="spr01", vms=[vm("spr01ev01", "job_a", ["guest_perf", "guest_nostop"], ["lpage_1g_on", "latency_high"], end_ref=True),
                                               vm("spr01ev02", "job_b4"), vm("spr01ev03", power="off")],
                 esx_monitors=["host_mon"], esx_profiles=["ht_wholecore_1"], applied=True, max_wait="12:00")
        c.update(desc='SPR 기준, "따옴표" 와 쉼표', group="g-spr", interval="1:30", start="2026-11-20 10:00:00", stop_mode="manual",
                 stop_time="2026-11-20 18:00:00", end_time="2026-11-20 11:50:00", seq_start="10:00")
        return c

    def test_defaults_and_old_settings_file(self):
        st = core.load_settings(self.d)
        self.assertEqual((st["case_file"], st["case_file_paths"]), ("", {"": {"record_csv": "", "log_dir": ""}}))
        self.assertEqual((core.case_file(st), core.cases_path(self.d), core.cases_path(self.d, "")),
                         ("", os.path.join(self.d, "test_cases.json"), os.path.join(self.d, "test_cases.json")))
        self.assertEqual(core.case_file_key(self.d, os.path.join(self.d, "test_cases.json")), "", "데이터 폴더의 기본 파일을 고르면 기본 파일")
        # v2.2 까지의 설정 파일(새 키 없음)과, 잠깐 있던 '테스트 종류' 의 키가 있는 파일도 읽는다
        with open(os.path.join(self.d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"record_csv": "rec/a.csv", "vm_user": "tester", "case_set": "EMR", "case_sets": ["EMR"],
                       "case_set_paths": {"EMR": {"record_csv": "x"}}}, f)
        st = core.load_settings(self.d)
        self.assertEqual((st["record_csv"], st["vm_user"], st["case_file"]), ("rec/a.csv", "tester", ""))
        self.assertFalse(any(k.startswith("case_set") for k in st), "쓰지 않는 키는 버림")
        self.save(st)
        raw = self.raw()
        self.assertEqual((raw["case_file"], raw["case_file_paths"], raw["record_csv"]), ("", {}, os.path.join(self.d, "rec", "a.csv")))

    def test_csv_roundtrip_keeps_every_field(self):
        cases = [self.spr_case(), case("second", esx="spr02", vms=[]), case("이름 없는 VM", vms=[vm("", "(없음)")])]
        want = [core._clean_case(c) for c in cases]
        text = core.cases_to_csv(cases)
        self.assertEqual(core.cases_from_csv(text), want)
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], core.CASE_CSV_HEADER)
        self.assertEqual([r[0] for r in rows[1:]], ["케이스", "VM", "VM", "VM", "케이스", "케이스", "VM"])
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual((first["적용"], first["케이스 이름"], first["ESX 호스트"], first["모니터링"], first["설정"], first["최대 대기"],
                          first["모니터링 중지"], first["잡 종료 (리포트)"], first["시작 시각 (순서 실행)"]),
                         ("예", "spr-base", "spr01", "host_mon", "ht_wholecore_1", "12:00", "직접 입력", "2026-11-20 11:50:00", "10:00"))
        self.assertEqual(rows[0][-3:], ["시작 시각 (순서 실행)", "전원", "모니터링 중지 (적용 때 고정)"],
                         "새 열은 맨 뒤 (이 열이 없는 예전 파일도 읽힌다)")
        old = "\r\n".join(",".join(r[:-3]) for r in csv.reader(io.StringIO(core.cases_to_csv([case("old-file")])))) + "\r\n"
        self.assertEqual(core.cases_from_csv(old)[0]["seq_start"], "", "시작 시각 열이 없는 파일은 자동으로 읽는다")
        self.assertEqual(core.cases_from_csv(old)[0]["vms"][0]["power"], "on", "전원 열이 없는 파일의 VM 은 켬")
        self.assertEqual([dict(zip(rows[0], r))["전원"] for r in rows[1:5]], ["", "켬", "켬", "끔"], "케이스 행은 비우고 VM 행은 켬, 끔")
        self.assertEqual(want[0]["vms"][2]["power"], "off")
        v1 = dict(zip(rows[0], rows[2]))
        self.assertEqual((v1["VM 이름"], v1["잡"], v1["모니터링"], v1["설정"], v1["종료 기준 VM"]),
                         ("spr01ev01", "job_a", "guest_perf; guest_nostop", "lpage_1g_on; latency_high", "기준"))
        p = os.path.join(self.ext, "sub", "spr.csv")
        core.save_cases(self.d, cases, p)
        with open(p, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data, "엑셀에서 열리는 UTF-8(BOM) CSV")
        self.assertEqual(core.load_cases(self.d, p), want)
        pj = os.path.join(self.ext, "spr.json")
        core.save_cases(self.d, cases, pj)
        self.assertEqual(core.load_cases(self.d, pj), want, ".json 으로 정하면 JSON")
        with open(pj, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], core.CASES_VERSION)
        self.assertEqual(core.load_cases(self.d, os.path.join(self.ext, "none.csv")), [], "없는 파일은 빈 표")

    def test_csv_reading_is_tolerant_and_rejects_other_files(self):
        text = ("VM 이름,구분,케이스 이름,잡,설정,적용,모르는 열\r\n,케이스,c1,,p1;p2 ,Y,x\r\n\r\nvm1,VM,,job_a,\"[\"\"a;b\"\", \"\"c\"\"]\",,\r\n"
                ",case,c2,,,,\r\n")
        got = core.cases_from_csv(text)
        self.assertEqual([(c["name"], c["applied"], c["esx_profiles"]) for c in got], [("c1", True, ["p1", "p2"]), ("c2", False, [])],
                         "열 순서가 달라도 머리글 이름으로 읽고, 모르는 열과 빈 줄은 건너뜀")
        self.assertEqual(got[0]["vms"], [{"name": "vm1", "job": "job_a", "monitors": [], "end_ref": False, "profiles": ["a;b", "c"],
                                          "power": "on"}],
                         "이름에 ; 이 있으면 JSON 목록으로 적고 읽는다")
        self.assertEqual(core._join_names(["a;b", "c"]), '["a;b", "c"]')
        self.assertEqual(core.cases_from_csv(""), [])
        cp949 = os.path.join(self.ext, "cp949.csv")
        with open(cp949, "wb") as f:
            f.write("구분,케이스 이름,VM 이름,설명\r\n케이스,k1,,한글 설명\r\n".encode("cp949"))
        self.assertEqual(core.load_cases(self.d, cp949)[0]["desc"], "한글 설명", "CP949 로 저장한 파일도 읽는다")
        for bad, word in (("테스트 케이스 이름,생성 시각\nx,y\n", "테스트 기록 CSV"), ("a,b\n1,2\n", "테스트 케이스 파일이 아님"),
                          ("구분,케이스 이름,VM 이름\nVM,,v1\n", "먼저"), ("구분,케이스 이름,VM 이름\n기타,c,\n", "구분 칸")):
            with self.assertRaises(ValueError) as cm:
                core.cases_from_csv(bad)
            self.assertIn(word, str(cm.exception))

    def test_user_scenario_save_as_then_load_back(self):
        """SPR 케이스를 파일로 저장하고, EMR 파일로 작업한 뒤 SPR 파일을 다시 불러오면 케이스와 지정한 설정이 그대로다."""
        spr, emr = os.path.join(self.ext, "spr_cases.csv"), os.path.join(self.ext, "emr_cases.csv")
        st = core.load_settings(self.d)
        core.save_cases(self.d, [self.spr_case()])                  # 파일을 정하지 않은 상태: 데이터 폴더에 자동 저장
        core.save_cases(self.d, core.load_cases(self.d), spr)       # [다른 이름으로 저장]
        st = core.switch_case_file(self.d, st, spr)
        st["record_csv"] = os.path.join(self.ext, "spr_record.csv")
        self.save(st)
        st = core.load_settings(self.d)                             # 생성기를 다시 열었다
        self.assertEqual((st["case_file"], st["record_csv"]), (spr, os.path.join(self.ext, "spr_record.csv")))
        core.save_cases(self.d, [case("emr-base", esx="emr01", vms=[vm("emr01ev01", "job_a", [], ["vpmc_on"])])], emr)
        st = core.switch_case_file(self.d, st, emr)
        self.assertEqual(st["record_csv"], os.path.join(self.ext, "spr_record.csv"), "처음 쓰는 파일은 지금 기록 CSV 를 그대로 씀")
        st["record_csv"] = os.path.join(self.ext, "emr_record.csv")
        self.save(st)
        st = core.switch_case_file(self.d, core.load_settings(self.d), spr)       # [불러오기] 로 SPR 파일
        self.save(st)
        st = core.load_settings(self.d)
        back = core.load_cases(self.d, core.case_file(st))
        self.assertEqual(back, [core._clean_case(self.spr_case())], "돌아오면 케이스 표가 그대로")
        self.assertEqual((back[0]["esx_profiles"], back[0]["vms"][0]["profiles"], back[0]["vms"][0]["monitors"], back[0]["applied"]),
                         (["ht_wholecore_1"], ["lpage_1g_on", "latency_high"], ["guest_perf", "guest_nostop"], True))
        self.assertEqual(st["record_csv"], os.path.join(self.ext, "spr_record.csv"), "그 파일에서 쓰던 기록 CSV 도 돌아옴")
        self.assertEqual([c["name"] for c in core.load_cases(self.d, emr)], ["emr-base"])
        self.assertEqual([c["name"] for c in core.load_cases(self.d)], ["spr-base"], "기본 파일은 다른 이름으로 저장한 시점의 상태로 남음")
        raw = self.raw()
        self.assertEqual(raw["case_file"], spr)
        self.assertEqual(sorted(os.path.basename(k) for k in raw["case_file_paths"]), ["emr_cases.csv", "spr_cases.csv"])
        self.assertEqual(raw["record_csv"], "", "파일의 맨 위 값은 기본 파일을 쓸 때의 값")

    def test_switch_rules(self):
        st = core.load_settings(self.d)
        st["record_csv"] = os.path.join(self.d, "rec", "default.csv")
        a = os.path.join(self.ext, "a.csv")
        core.save_cases(self.d, [], a)
        s2 = core.switch_case_file(self.d, st, a)
        self.assertEqual((s2["case_file"], s2["record_csv"]), (a, os.path.join(self.d, "rec", "default.csv")))
        self.assertEqual(st["case_file"], "", "받은 설정은 바꾸지 않고 새 설정을 반환")
        s2["log_dir"] = os.path.join(self.ext, "logs-a")
        s3 = core.switch_case_file(self.d, s2, "")
        self.assertEqual((s3["case_file"], s3["record_csv"], s3["log_dir"]), ("", os.path.join(self.d, "rec", "default.csv"), ""))
        s4 = core.switch_case_file(self.d, s3, os.path.join(self.d, "sub", "..", "test_cases.json"))
        self.assertEqual(s4["case_file"], "", "데이터 폴더의 test_cases.json 을 고르면 기본 파일")
        s5 = core.switch_case_file(self.d, s4, a)
        self.assertEqual(s5["log_dir"], os.path.join(self.ext, "logs-a"), "전에 쓰던 값을 기억")
        # 공통 설정은 파일을 바꿔도 같다
        s6 = core.switch_case_file(self.d, dict(s5, vm_user="tester", jump_log_dir="/data/log", esx_addr="esx01=10.0.0.11", mon_lead_min=7), "")
        self.assertEqual((s6["vm_user"], s6["jump_log_dir"], s6["esx_addr"], s6["mon_lead_min"]), ("tester", "/data/log", "esx01=10.0.0.11", 7))
        # 없어진 파일의 값은 저장할 때 버린다. 기본 경로와 같은 값은 비워 저장한다
        gone = os.path.join(self.ext, "gone.csv")
        core.save_cases(self.d, [], gone)
        s7 = core.switch_case_file(self.d, s6, gone)
        s7 = core.switch_case_file(self.d, dict(s7, record_csv=core.default_paths(self.d)[0]), a)
        os.remove(gone)
        self.save(s7)
        raw = self.raw()
        self.assertEqual(list(raw["case_file_paths"]), [core.case_file_key(self.d, a)])
        self.assertEqual(raw["case_file"], a)

    def test_load_drops_bad_values(self):
        with open(os.path.join(self.d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"case_file": 5, "case_file_paths": {"": {"record_csv": "no"}, "/x/y.csv": {"record_csv": None, "log_dir": 7}, "/z": "bad"}}, f)
        st = core.load_settings(self.d)
        self.assertEqual(st["case_file"], os.path.normpath("5"))
        self.assertEqual(st["case_file_paths"], {"": {"record_csv": "", "log_dir": ""}, "/x/y.csv": {"record_csv": "", "log_dir": "7"}})
        with open(os.path.join(self.d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"case_file": None, "case_file_paths": []}, f)
        self.assertEqual(core.load_settings(self.d)["case_file"], "")

    def test_generate_writes_the_record_of_the_current_file(self):
        a = os.path.join(self.ext, "a.csv")
        core.save_cases(self.d, [], a)
        st = core.switch_case_file(self.d, core.load_settings(self.d), a)
        st["record_csv"] = os.path.join(self.ext, "rec", "a_record.csv")
        st["log_dir"] = os.path.join(self.ext, "logs")
        res = core.generate(with_starts([case("c1")]), copy.deepcopy(RUN), copy.deepcopy(MON), core.resolve_paths(self.d, st), now=NOW,
                            write=True, profiles=[])
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["record"], os.path.join(self.ext, "rec", "a_record.csv"))
        self.assertTrue(res["log"].startswith(os.path.join(self.ext, "logs", "testcaselogging-")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "testcases", "test_record.csv")))

    def test_auto_backup_twice_a_day(self):
        self.assertIsNone(core.auto_backup(self.d, {}, dt.datetime(2026, 11, 16, 9, 0)), "데이터가 없으면 만들지 않음")
        a = os.path.join(self.ext, "a.csv")
        core.save_cases(self.d, [case("in-file")], a)
        core.save_cases(self.d, [case("default")])
        st = core.switch_case_file(self.d, core.load_settings(self.d), a)
        self.save(st)
        core.save_profiles(self.d, core.default_profiles())
        self.assertEqual((core.auto_backup_slot(dt.datetime(2026, 11, 16, 0, 0)), core.auto_backup_slot(dt.datetime(2026, 11, 16, 11, 59)),
                          core.auto_backup_slot(dt.datetime(2026, 11, 16, 12, 0)), core.auto_backup_slot(dt.datetime(2026, 11, 16, 23, 59))),
                         ("20261116_AM", "20261116_AM", "20261116_PM", "20261116_PM"))
        am = core.auto_backup(self.d, st, dt.datetime(2026, 11, 16, 9, 0))
        self.assertEqual(am, os.path.join(self.d, "backup", "auto", "20261116_AM"))
        self.assertEqual(sorted(os.listdir(am)), ["case_file_a.csv", "profiles.json", "settings.json", "test_cases.json"])
        self.assertEqual([c["name"] for c in core.load_cases(self.d, os.path.join(am, "case_file_a.csv"))], ["in-file"],
                         "사용자가 정한 테스트 케이스 파일도 함께 복사")
        self.assertIsNone(core.auto_backup(self.d, st, dt.datetime(2026, 11, 16, 11, 30)), "같은 시간대에는 다시 만들지 않음")
        pm = core.auto_backup(self.d, st, dt.datetime(2026, 11, 16, 13, 0))
        self.assertEqual(os.path.basename(pm), "20261116_PM")
        self.assertFalse(os.path.exists(pm + ".tmp"))
        # 오래된 백업은 60개(30일치)만 남긴다
        root = os.path.join(self.d, "backup", "auto")
        day = dt.datetime(2026, 11, 17, 9, 0)
        for i in range(70):
            core.auto_backup(self.d, st, day + dt.timedelta(hours=12 * i))
        names = sorted(os.listdir(root))
        self.assertEqual(len(names), core.AUTO_BACKUP_KEEP)
        self.assertNotIn("20261116_AM", names)
        self.assertEqual(names[-1], core.auto_backup_slot(day + dt.timedelta(hours=12 * 69)))
        # 자동 백업 폴더는 [백업 불러오기] 로 되돌릴 수 있다
        core.save_cases(self.d, [case("changed")])
        core.restore_from(os.path.join(root, names[-1]), self.d, now=NOW)
        self.assertEqual([c["name"] for c in core.load_cases(self.d)], ["default"])

    def test_manual_backup_keeps_the_chosen_case_file(self):
        """[설정 백업] 은 사용자가 정한 테스트 케이스 파일도 사본으로 넣고, [백업 불러오기] 는 그 파일을 백업한 때의 내용으로 되돌린다
        (2026-11-17 확인: 사본이 없어 백업을 불러와도 탭 4 의 표가 돌아오지 않았음)."""
        a = os.path.join(self.ext, "a.csv")
        core.save_cases(self.d, [case("at-backup", seq_start="10:00")], a)
        core.save_cases(self.d, [case("default")])
        st = core.switch_case_file(self.d, core.load_settings(self.d), a)
        self.save(st)
        dest, done = core.backup_to(self.d, os.path.join(self.ext, "bk"), now=NOW, s=st)
        self.assertIn("case_file_a.csv (지금 쓰는 테스트 케이스 파일의 사본)", done)
        self.assertEqual(core.backup_case_file(dest), (os.path.normpath(a), os.path.join(dest, "case_file_a.csv")))
        core.save_cases(self.d, [case("changed-later")], a)
        keep, done = core.restore_from(dest, self.d, now=NOW + dt.timedelta(minutes=1))
        self.assertEqual([(c["name"], c["seq_start"]) for c in core.load_cases(self.d, a)], [("at-backup", "10:00")],
                         "탭 4 의 표(지정한 파일)도 백업한 때의 내용으로 돌아옴. 시작 시각 포함")
        self.assertEqual([c["name"] for c in core.load_cases(self.d, os.path.join(keep, "case_file_a.csv"))], ["changed-later"],
                         "바꾸기 전 내용은 before_restore 폴더에 남김")
        self.assertTrue(any(x.startswith("case_file_a.csv (테스트 케이스 파일 ") for x in done), done)
        # 사본이 없는 백업(예전 판이 만든 백업): 지정한 파일은 바꾸지 않는다
        os.remove(os.path.join(dest, "case_file_a.csv"))
        self.assertEqual(core.backup_case_file(dest), (os.path.normpath(a), ""))
        core.save_cases(self.d, [case("now")], a)
        core.restore_from(dest, self.d, now=NOW + dt.timedelta(minutes=2))
        self.assertEqual([c["name"] for c in core.load_cases(self.d, a)], ["now"])
        # 기본 파일을 쓸 때는 데이터 파일(test_cases.json)로 백업된다
        st0 = core.switch_case_file(self.d, core.load_settings(self.d), "")
        self.save(st0)
        dest0, done0 = core.backup_to(self.d, os.path.join(self.ext, "bk0"), now=NOW, s=st0)
        self.assertFalse(any(x.startswith("case_file_") for x in os.listdir(dest0)))
        self.assertIsNone(core.backup_case_file(dest0))

    def test_move_data_folder_remaps_remembered_paths(self):
        other = tempfile.mkdtemp(prefix="tcgen_cf_new_")
        try:
            a = os.path.join(self.ext, "a.csv")
            core.save_cases(self.d, [], a)
            st = core.load_settings(self.d)
            st["log_dir"] = os.path.join(self.d, "logs-default")
            st = core.switch_case_file(self.d, st, a)
            st["record_csv"] = os.path.join(self.d, "rec", "a.csv")
            self.save(st)
            shutil.rmtree(other)
            core.copy_data(self.d, other, dirs=(core.DEFAULT_CASE_DIR,))
            core._remap_settings_paths(self.d, other)
            moved = core.load_settings(other)
            self.assertEqual((moved["case_file"], moved["record_csv"]), (a, os.path.join(other, "rec", "a.csv")),
                             "지금 쓰는 파일의 기록 경로를 새 폴더 기준으로")
            self.assertEqual(moved["case_file_paths"][""]["log_dir"], os.path.join(other, "logs-default"), "기본 파일의 값도 새 폴더 기준으로")
        finally:
            shutil.rmtree(other, ignore_errors=True)


class JumpFileTests(unittest.TestCase):
    def read(self, rel):
        with io.open(os.path.join(BASE, rel), encoding="ascii", newline="") as f:
            return f.read().replace("\r\n", "\n")        # Windows 에서 줄바꿈을 CR+LF 로 받은 경우에도 내용만 비교한다

    def test_payload_is_in_sync_with_sources(self):
        self.assertEqual(payload.TCRUN_SH, self.read(os.path.join("jump", "tcrun.sh")), "python tools/make_payload.py 실행 필요")
        self.assertEqual(payload.APPLY_PY, self.read(os.path.join("jump", "tcgen_apply.py")), "python tools/make_payload.py 실행 필요")

    def test_versions_match(self):
        self.assertIn("\nTCRUN_VERSION=%s\n" % core.VERSION, payload.TCRUN_SH)
        self.assertIn('\nVERSION = "%s"\n' % core.VERSION, payload.APPLY_PY)
        self.assertEqual(core.PLAN_FORMAT, "TCGEN-PLAN 2", "줄을 더하기만 할 때는 형식 번호를 올리지 않음(DEVELOPER.md 5절)")
        self.assertIn('\nPLAN_FMT="%s"\n' % core.PLAN_FORMAT, payload.TCRUN_SH)

    def test_pc_code_keeps_python38_syntax(self):
        """PC 쪽 코드는 Python 3.8 에서 동작해야 한다. 3.9 이후에 생긴 문법과 함수를 쓰지 않는다."""
        for rel in ("tcgen_core.py", "tcgen_app.py", "tcgen_payload.py", os.path.join("tools", "make_payload.py")):
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            if sys.version_info >= (3, 8):
                ast.parse(src, feature_version=(3, 8))
            self.assertNotRegex(src, r"\.removeprefix\(|\.removesuffix\(|\bmatch\s+\w+\s*:\s*$|\bzoneinfo\b|functools\.cache\b|"
                                     r"->\s*(list|dict|tuple|set)\[|:\s*(list|dict|tuple|set)\[", rel)

    def test_jump_files_are_ascii_lf(self):
        for name, text in core.jump_files():
            self.assertTrue(text.endswith("\n"), name)
            self.assertNotIn("\r", text, name)
            self.assertTrue(all(ord(ch) < 127 for ch in text), name)
        self.assertNotIn("\t", payload.TCRUN_SH)

    def test_apply_script_keeps_python35_syntax(self):
        """ESX 의 python3 는 3.5 일 수 있다. f-string, 변수 주석, := 같은 뒤 판 문법이 없어야 한다."""
        tree = ast.parse(payload.APPLY_PY)
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, (ast.JoinedStr, ast.AnnAssign), "f-string 또는 변수 주석: %d번째 줄" % getattr(node, "lineno", 0))
            if hasattr(ast, "NamedExpr"):
                self.assertNotIsInstance(node, ast.NamedExpr)
            if isinstance(node, (ast.FunctionDef, ast.Lambda)):
                self.assertEqual(getattr(node.args, "posonlyargs", []), [])
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name, ("json", "os", "re", "shutil", "signal", "subprocess", "sys", "time"), "표준 라이브러리만")
        self.assertNotIn("ImportFrom", [type(n).__name__ for n in ast.walk(tree)])
        self.assertNotRegex(payload.APPLY_PY, r"subprocess\.run\(|capture_output|text=True|\.removeprefix\(|\bnonlocal\b")

    def test_tcrun_does_not_eval_the_plan(self):
        code = "\n".join(x for x in payload.TCRUN_SH.split("\n") if not x.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(^|[;&|(\s])eval\s")
        self.assertNotRegex(code, r"(^|[;&|\s])(\.|source)\s+\"?\$")
        self.assertNotIn("local ", code, "POSIX sh 에 없는 local 을 쓰지 않음")
        self.assertNotIn("[[", code)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "sh 필요")
    def test_tcrun_syntax_in_sh_and_dash(self):
        src = os.path.join(BASE, "jump", "tcrun.sh")
        for shell in ("sh", "dash", "bash"):
            if shutil.which(shell):
                p = subprocess.run([shell, "-n", src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                self.assertEqual((p.returncode, p.stdout), (0, b""), shell)
                p = subprocess.run([shell, src, "version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
                                   env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})
                self.assertEqual(p.stdout.decode().strip(), core.VERSION, shell)


if __name__ == "__main__":
    unittest.main()


class JumpDirTests(unittest.TestCase):
    """점프 VM 작업 폴더(사용자 요청 2026-11-16 "tcgen 실행 위치 바꿔야되겠어.. 계정홈폴더로하니까 홈폴더 용량제한있음")."""

    def test_default_folder_keeps_the_v14_files(self):
        files = dict(core.jump_files())
        self.assertEqual(files["askpass"], 'cat "$HOME/.tcgen/vmpw"\n')
        self.assertEqual(files["vmssh"], "".join(x + "\n" for x in core.VMSSH_LINES), "기본 폴더의 vmssh 는 v1.4 와 같음")
        self.assertEqual(files["setpw"], "".join(x + "\n" for x in core.SETPW_LINES))
        self.assertIn("-o LogLevel=ERROR -o UserKnownHostsFile=$HOME/.tcgen/known_hosts", files["esxssh"],
                      "ESX 접속은 로그인 배너를 출력하지 않고 known_hosts 를 작업 폴더에 둔다")
        self.assertNotIn("copied:", core.seq_setup_block(), "기본 폴더이면 복사 줄이 없음")

    def test_other_folder(self):
        files = dict(core.jump_files("/data/tcgen"))
        self.assertEqual(files["askpass"], 'cat "/data/tcgen/vmpw"\n')
        self.assertTrue(files["vmssh"].startswith('SSH_ASKPASS="/data/tcgen/askpass";'))
        self.assertTrue(files["setpw"].startswith("D=/data/tcgen\n"))
        self.assertIn('O="-i /data/tcgen/id_esx ', files["esxssh"])
        self.assertIn("usage: sh /data/tcgen/esxssh", files["esxssh"])
        self.assertIn("D=/data/tcgen\n", files["postsetup"])
        self.assertIn("sh /data/tcgen/setpw", files["postsetup"])
        self.assertEqual(dict(core.jump_files("~/work/tc"))["askpass"], 'cat "$HOME/work/tc/vmpw"\n')
        a = core.hd_unescape(core.seq_setup_block("/data/tcgen"))
        self.assertIn("D=/data/tcgen; mkdir -p $D/runs", a)
        self.assertIn('if [ ! -s $D/$F ] && [ -s $HOME/.tcgen/$F ]; then cp -p $HOME/.tcgen/$F $D/$F', a,
                      "~/.tcgen 의 키와 비밀번호를 새 폴더로 복사(ESX 에 키를 다시 등록하지 않게)")
        self.assertTrue(a.endswith("TCSETUP\nsh /data/tcgen/postsetup\n"))

    def test_blocks_use_the_folder(self):
        st = settings(jump_dir="/data/tcgen/")
        res = core.generate(with_starts([case("d1")]), RUN, MON, st, now=NOW, write=False, profiles=PROF)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["jump_dir"], "/data/tcgen")
        for blk in (res["check"], res["run"]):
            self.assertIn("D=/data/tcgen; R=\\$D/runs/%s" % res["run_id"], blk)
        self.assertIn("sh /data/tcgen/vmssh esx01ev01 << TCEOF", res["vm_wipe"])
        self.assertIn("sh /data/tcgen/postsetup", res["setup"])
        res0 = core.generate(with_starts([case("d1")]), RUN, MON, settings(), now=NOW, write=False, profiles=PROF)
        self.assertIn("D=\\$HOME/.tcgen; R=\\$D/runs/", res0["check"])
        self.assertIn("sh ~/.tcgen/vmssh esx01ev01 << TCEOF", res0["vm_wipe"])
        self.assertIn("Check the free space and quota of \\$D", res0["run"])

    def test_bad_folder(self):
        for bad in ("data/tcgen", "/", "~", "~/a b", "~/x/../y", "/data/./t", "/데이터"):
            res = core.generate([case("d1")], RUN, MON, settings(jump_dir=bad), now=NOW, write=False, profiles=PROF)
            self.assertTrue(any("점프 VM 작업 폴더" in e for e in res["errors"]), (bad, res["errors"]))
        self.assertEqual(core.jump_dir({}), "~/.tcgen")
        self.assertEqual(core.jump_dir({"jump_dir": " /data/t/ "}), "/data/t")
        self.assertEqual(core.DEFAULT_SETTINGS["jump_dir"], "")
        self.assertEqual(core.jump_log_dir({"jump_dir": "/data/t/"}), "/data/t/log", "로그 폴더를 비우면 작업 폴더 아래 log")
        self.assertEqual(core.jump_log_dir({"jump_dir": "/data/t", "jump_log_dir": "/x"}), "/x")
        self.assertEqual(core.jump_log_dir({}), "~/tcgen_log")
