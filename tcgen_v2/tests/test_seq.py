# -*- coding: utf-8 -*-
"""
순서 실행(seq)의 단위 시험: 설정 프로파일 읽기, 검증 오류, plan 생성, 프로세스 이름 처리, 블록의 글자 규칙,
v1 데이터 옮겨 오기, 점프 VM 파일 내용. 실행: python -m unittest discover -s tests -v
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
         seq_start=""):
    c = core.new_case(esx, "", 0)
    c.update(name=name, vms=vms if vms is not None else [vm(esx + "ev01", "job_a")], esx_monitors=list(esx_monitors),
             esx_profiles=list(esx_profiles), max_wait=max_wait, applied=applied, desc=desc, group=group, seq_start=seq_start)
    return c


def settings(**over):
    s = dict(core.DEFAULT_SETTINGS)
    s.update(record_csv="/nonexistent/rec.csv", log_dir="/nonexistent")
    s.update(over)
    return s


def gen(cases, run=None, mon=None, prof=None, st=None):
    return core.generate(copy.deepcopy(cases), copy.deepcopy(run or RUN), copy.deepcopy(mon or MON), st or settings(),
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


class ProcNameTests(unittest.TestCase):
    def test_pattern_escapes_regex_characters(self):
        self.assertEqual(core.proc_pattern("run_a.sh"), r"run_a\.sh")
        self.assertEqual(core.proc_pattern("a+b*c?(x)[y]{2}|z^$"), r"a\+b\*c\?\(x\)\[y]\{2}\|z\^\$")
        self.assertEqual(core.proc_pattern("python3 run sim-1"), "python3 run sim-1")

    def test_pattern_matches_only_the_literal_name(self):
        for name in ("run_a.sh", "sim.v2+x", "a|b", "x[1]", "f(1)", "a{2}", "^start$", "c++ -O2", "what?", "s*r"):
            rx = re.compile(core.proc_pattern(name))
            self.assertTrue(rx.search("/opt/bin/%s --flag" % name), name)
        self.assertFalse(re.compile(core.proc_pattern("run_a.sh")).search("/opt/run_aXsh"))
        self.assertFalse(re.compile(core.proc_pattern("sim.v2+x")).search("simXv22x"))
        self.assertFalse(re.compile(core.proc_pattern("a|b")).search("only a here"))

    def test_name_rules(self):
        self.assertEqual(core.check_proc_name("run_a.sh -n 4", "x"), [])
        for bad in ("it's", "wow!", "a\\b", "한글", "a\tb", "x" * 81):
            self.assertEqual(len(core.check_proc_name(bad, "x")), 1, bad)
        self.assertEqual(core.check_proc_name("x" * 80, "x"), [])

    @unittest.skipUnless(os.name == "posix" and shutil.which("pgrep") and shutil.which("dash"), "pgrep, dash 필요")
    def test_real_pgrep_with_the_poll_script(self):
        """tcrun 이 VM 에 보내는 확인 글과 같은 형태를 실제 pgrep 으로 실행한다. 특수 문자가 든 이름도 글자 그대로 찾는다."""
        tag = "tcgen%dq" % os.getpid()
        name = "%s.v2+x[1]" % tag
        decoy = "%sXv2+x[1]" % tag
        procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", n]) for n in (name, decoy)]
        try:
            time.sleep(0.5)
            script = ("O=`pgrep -f -- '%s'`; R=$?\nC=`printf '%%s\\n' \"$O\" | grep -c .`\n"
                      "if [ $R -eq 0 ]; then echo \"TCGEN-PROC $C\"; elif [ $R -eq 1 ]; then echo \"TCGEN-PROC 0\"; "
                      "else echo \"TCGEN-PROC error $R\"; fi\n")
            for pat, want in ((core.proc_pattern(name), "TCGEN-PROC 1"), (core.proc_pattern(tag + ".nothing"), "TCGEN-PROC 0")):
                p = subprocess.run(["dash", "-s"], input=(script % pat).encode(), stdout=subprocess.PIPE, timeout=20)
                self.assertEqual(p.stdout.decode().split("\n")[0], want, pat)
        finally:
            for p in procs:
                p.kill()
                p.wait()


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

    def test_job_needs_process_name(self):
        run = [dict(RUN[0], proc=""), RUN[1]]
        errs = self.errors([case("c1"), case("c2")], run=run)
        self.assert_one(errs, "프로세스 이름이 비어")

    def test_process_name_rules(self):
        for bad, word in (("it's", "작은따옴표"), ("wow!", "작은따옴표"), ("a\\b", "작은따옴표"), ("한글", "ASCII"), ("x" * 81, "80자")):
            self.assert_one(self.errors([case("c1")], run=[dict(RUN[0], proc=bad)]), word)

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

    def test_settings_rules(self):
        for over, word in (({"jump_log_dir": "relative/dir"}, "로그 폴더"), ({"jump_log_dir": "/var/my log"}, "로그 폴더"),
                           ({"jump_log_dir": "~/로그"}, "로그 폴더"), ({"esx_addr": "esx01"}, "접속 주소"),
                           ({"esx_addr": "esx01=10.0.0.1;bad name=1"}, "접속 주소"), ({"vm_user": "a b"}, "접속 계정"),
                           ({"vm_user": "-oProxyCommand=x"}, "접속 계정"), ({"mon_lead_min": -1}, "모니터링 시작 분"),
                           ({"seq_settle_min": -1}, "최소 분"), ({"seq_poll_sec": 0}, "확인 간격"),
                           ({"seq_gone_count": 0}, "연속 없음"), ({"seq_poweron_wait_min": 0}, "VM 켜기 대기"),
                           ({"seq_gap_min": -1}, "케이스 사이 간격"), ({"seq_first_check_sec": 0}, "첫 확인")):
            self.assert_one(self.errors([case("c1")], st=settings(**over)), word)
        for ok in ({"jump_log_dir": "/data/tcgen/log-1"}, {"jump_log_dir": "~/a.b/c_d"}, {"jump_log_dir": ""},
                   {"esx_addr": "esx01=10.0.0.11; esx02=esx02.lab.local"}, {"seq_settle_min": 0}, {"vm_user": "tester"},
                   {"seq_poweron_wait_min": 120}, {"seq_gap_min": 0}):
            self.assertEqual(gen([case("c1")], st=settings(**ok))["errors"], [], ok)
        self.assertEqual(core.DEFAULT_SETTINGS["seq_gap_min"], 30, "케이스 사이 간격의 기본값은 30분")
        self.assertEqual((core.DEFAULT_SETTINGS["seq_poll_sec"], core.DEFAULT_SETTINGS["seq_first_check_sec"]), (1800, 60),
                         "잡 종료 확인은 잡 시작 1분 뒤 한 번, 그 뒤 30분 간격(잡이 실행되는 동안 VM 접속을 줄임)")
        res = gen([case("c1")], st=settings(seq_poll_sec=3600, seq_first_check_sec=90))
        self.assertIn("SET POLL_SEC 3600\n", res["plan"])
        self.assertIn("SET FIRST_CHECK_SEC 90\n", res["plan"])
        self.assertIn("잡 시작 90초 뒤 한 번, 그 뒤 60분마다", res["plan_view"])
        self.assertIn("리포트용 CSV 의 잡 종료 시각은 비워 둔다", res["plan_view"])
        self.assertNotIn("비워 둔다", gen([case("c1")], st=settings(seq_poll_sec=60))["plan_view"])
        self.assertIn("SET GAP_MIN 45\n", gen([case("c1")], st=settings(seq_gap_min=45))["plan"])
        self.assertEqual(core.DEFAULT_SETTINGS["seq_poweron_wait_min"], 90, "VM 켜기 대기의 기본값은 90분")
        self.assertIn("SET POWERON_WAIT_MIN 120\n", gen([case("c1")], st=settings(seq_poweron_wait_min=120))["plan"])

    def test_max_wait_format(self):
        self.assert_one(self.errors([case("c1", max_wait="soon")]), "최대 대기")
        res = gen([case("c1", max_wait="1:30"), case("c2", max_wait="")])
        self.assertIn("MAXWAIT 001 90\n", res["plan"])
        self.assertIn("MAXWAIT 002 0\n", res["plan"])

    def test_same_vm_under_two_esx_hosts(self):
        self.assert_one(self.errors([case("c1", esx="esx01", vms=[vm("shared", "job_a")]),
                                     case("c2", esx="esx02", vms=[vm("shared", "job_a")])]), "ESX 호스트가 다른 케이스")

    def test_warnings(self):
        res = gen([case("c1")], run=[dict(RUN[0], proc="sh")])
        self.assertEqual(res["errors"], [])
        self.assertTrue(any("3자보다 짧음" in w for w in res["warnings"]))
        mon = [dict(MON[0], start="/opt/perf record -p `pgrep run_a.sh`")]
        res = gen([case("c1", vms=[vm("esx01ev01", "job_a", ["guest_perf"])])], mon=mon)
        self.assertTrue(any("모니터링" in w and "run_a.sh" in w for w in res["warnings"]))

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

    def test_plan_text(self):
        res = gen(self.two(), st=settings(vm_user="tester", esx_addr="esx01=10.0.0.11;esx09=10.0.0.19", jump_log_dir="/data/log"))
        self.assertEqual(res["errors"], [])
        want = """TCGEN-PLAN 2
VER 2.6
RUNID 20261116-143000
LOGDIR /data/log
GEN 2026-11-16 14:30:00
SET MON_LEAD_MIN 5
SET SETTLE_MIN 10
SET GAP_MIN 30
SET SPACING_MIN 5
SET PREP_LEAD_MIN 60
SET POLL_SEC 1800
SET FIRST_CHECK_SEC 60
SET GONE_COUNT 3
SET START_WAIT_MIN 10
SET POWERON_WAIT_MIN 90
SET BOOT_WAIT_MIN 30
SET SHUTDOWN_WAIT_MIN 10
ADDR esx01 10.0.0.11
CHAIN esx01
CHAIN -
RPTH 테스트 그룹,케이스명,설명,호스트 이름,잡 시작 시각,잡 종료 시각,VM,종료 기준 VM
CASE 001 esx01 caseA
MAXWAIT 001 720
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
MS 001 esx01ev01 TC_HOST=`hostname -s`; TC_START=`date +\\%Y\\%m\\%d\\%H\\%M\\%S`; /home/tester/mon/start_mon.sh >> /home/tester/log/mon-${TC_HOST}-${TC_START}.log # tcgen caseA esx01ev01
MS 001 esx01ev01 /home/tester/mon/once.sh # tcgen caseA esx01ev01
JOB 001 esx01ev01 TC_HOST=`hostname -s`; TC_START=`date +\\%Y\\%m\\%d\\%H\\%M\\%S`; /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1 # tcgen caseA esx01ev01
PROC 001 esx01ev01 run_a\\.sh
MX 001 esx01ev01 /home/tester/mon/stop_mon.sh # tcgen caseA esx01ev01
PAT 001 esx01ev02  # tcgen caseA esx01ev02$
JOB 001 esx01ev02 /home/tester/bench/run_b.sh -n 4 -tag x\\%y # tcgen caseA esx01ev02
PROC 001 esx01ev02 run_b\\.sh -n 4
EPAT 001  # tcgen caseA esx01$
EMS 001 /bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start '%H' # tcgen caseA esx01
EMX 001 /bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop # tcgen caseA esx01
END 001
CASE 002 - caseB
MAXWAIT 002 0
RPTA 002 ,caseB,,
RPTB 002 vm-b,
VM 002 vm-b tester@vm-b
PAT 002 vm-b  # tcgen caseB vm-b$
JOB 002 vm-b TC_HOST=`hostname -s`; TC_START=`date +\\%Y\\%m\\%d\\%H\\%M\\%S`; /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1 # tcgen caseB vm-b
PROC 002 vm-b run_a\\.sh
END 002
"""
        self.assertEqual(res["plan"], want)
        self.assertEqual(res["chains"], ["esx01", "-"])
        self.assertEqual(res["wipe_hosts"], ["esx01ev01", "esx01ev02", "vm-b"])
        self.assertEqual(res["run_id"], "20261116-143000")

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

    def test_plan_view_text(self):
        view = gen(self.two())["plan_view"]
        for word in ("실행 ID 20261116-143000", "점프 VM 작업 폴더 ~/.tcgen, 로그 폴더 ~/tcgen_log",
                     "진행 확인 sh ~/.tcgen/tcrun status, 멈추기 sh ~/.tcgen/tcrun stop, 취소 sh ~/.tcgen/tcrun cancel",
                     "[공통 규칙]", "[ESX 호스트 esx01] 케이스 1개", "[ESX 호스트 없음] 케이스 1개",
                     "켬인 VM 은 표의 순서대로 하나씩 켠다. VM 마다 켜기 작업, 1GB page backing 완료",
                     "마지막 VM 의 준비가 끝나고 10분 뒤에 잡을 시작한다", "잡 시작 5분 전에 시작하고",
                     "   VM 전원: esx01ev01 켬, esx01ev02 켬, esx01ev03 켬, esx01ev04 켬",
                     "   시작: 자동. 이 ESX 호스트의 첫 잡이라 준비가 끝나는 대로",
                     "     - esx01ev02 job_b4 (프로세스 이름 run_b.sh -n 4): /home/tester/bench/run_b.sh -n 4 -tag x%y",
                     "     - ESX 호스트 /Numa/LocalityWeightActionAffinity = 0", "     - esx01ev01 numa.nodeAffinity (이 키의 줄을 삭제)",
                     '     - esx01ev01 SCHED.MEM.PIN = "FALSE"', "   모니터링 중지:", "   최대 대기: 12:00", "   최대 대기: 제한 없음"):
            self.assertIn(word, view)
        self.assertEqual(view.count("[공통 규칙]"), 1, "공통 규칙은 한 번만")
        self.assertNotIn("    설정: 없음", view.split("[ESX 호스트 없음]")[1], "ESX 호스트가 없는 케이스는 설정 줄을 뺌")
        view0 = gen(self.two(), st=settings(seq_settle_min=0))["plan_view"]
        self.assertIn("- 켠 뒤 대기: 없음(설정값 0분).", view0)
        viewd = gen(self.two(), st=settings(jump_dir="/data/tcgen/"))["plan_view"]
        self.assertIn("점프 VM 작업 폴더 /data/tcgen,", viewd)
        self.assertIn("취소 sh /data/tcgen/tcrun cancel", viewd)

    def test_settle_setting_goes_into_the_plan(self):
        self.assertIn("SET SETTLE_MIN 10\n", gen(self.two())["plan"])
        self.assertIn("SET SETTLE_MIN 0\n", gen(self.two(), st=settings(seq_settle_min=0))["plan"])
        self.assertIn("SET SETTLE_MIN 25\n", gen(self.two(), st=settings(seq_settle_min=25))["plan"])
        self.assertEqual(core.DEFAULT_SETTINGS["seq_settle_min"], 10)

    def test_records(self):
        d = tempfile.mkdtemp(prefix="tcgen_rec_")
        try:
            st = settings(record_csv=os.path.join(d, "rec.csv"), log_dir=d)
            # v1.4 형식의 기록 파일이 이미 있는 경우: 새 열은 끝에 추가하고 원본을 따로 복사한다
            core.append_csv(st["record_csv"], core.RECORD_HEADER, [["old"] + [""] * (len(core.RECORD_HEADER) - 1)])
            res = core.generate(self.two(), RUN, MON, st, now=NOW, write=True, profiles=PROF, backup_dir=os.path.join(d, "bk"))
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["record_added"], ["진행 방식", "설정", "실행 ID"])
            self.assertTrue(os.path.exists(res["record_backup"]))
            with open(st["record_csv"], encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], core.RECORD_HEADER_SEQ)
            self.assertEqual(rows[1][0], "old")
            row = dict(zip(rows[0], rows[2]))
            self.assertEqual((row["테스트 케이스 이름"], row["진행 방식"], row["실행 ID"], row["잡 시작(KST)"]),
                             ("caseA", "순서 실행", "20261116-143000", ""))
            self.assertEqual(row["설정"], "esx01:numa0, esx01ev01:pin+aff, esx01ev03:pin")
            with open(res["log"], encoding="utf-8-sig", newline="") as f:
                log = list(csv.reader(f))
            self.assertEqual(log[0], core.LOG_HEADER_SEQ)
            kinds = [r[2] for r in log[1:]]
            self.assertEqual(kinds.count("잡"), 3)
            self.assertEqual(kinds.count("설정"), 8)
            job = [dict(zip(log[0], r)) for r in log[1:] if r[2] == "잡"][0]
            self.assertEqual((job["프로세스 이름"], job["crontab 시각 기준"], job["실행 시각(KST)"]), ("run_a.sh", "순서 실행", ""))
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
        self.assertEqual(core.VERSION, "2.6")
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
        self.assertEqual((s["run_mode"], s["jump_log_dir"], s["esx_addr"], s["seq_settle_min"], s["seq_poll_sec"]),
                         ("seq", "", "", 10, 1800))
        with open(os.path.join(d, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"run_mode": "fast", "seq_settle_min": "x", "seq_poll_sec": "30", "jump_log_dir": None}, f)
        s = core.load_settings(d)
        self.assertEqual((s["run_mode"], s["seq_settle_min"], s["seq_poll_sec"], s["jump_log_dir"]), ("seq", 10, 30, ""))
        s["run_mode"] = "time"
        core.save_settings(d, core.resolve_paths(d, s))
        self.assertEqual(core.load_settings(d)["run_mode"], "time")
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
    """순서 실행에서 케이스의 잡을 시작할 시각(비우면 자동)과 케이스 사이 간격."""

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

    def test_plan_carries_the_start_time_and_the_gap(self):
        cases = [case("first", seq_start="15:00"), case("second"), case("third", seq_start="2026-11-17 08:00")]
        res = gen(cases)
        self.assertEqual(res["errors"], [])
        t1, t3 = dt.datetime(2026, 11, 16, 15, 0), dt.datetime(2026, 11, 17, 8, 0)
        lines = res["plan"].split("\n")
        self.assertIn("START 001 %d 2026-11-16 15:00" % int(time.mktime(t1.timetuple())), lines)
        self.assertIn("START 003 %d 2026-11-17 08:00" % int(time.mktime(t3.timetuple())), lines)
        self.assertEqual([x for x in lines if x.startswith("START 002 ")], [], "시각을 비운 케이스에는 START 줄이 없음(자동)")
        self.assertIn("SET GAP_MIN 30", lines)
        self.assertLess(lines.index("MAXWAIT 001 0"), lines.index("START 001 %d 2026-11-16 15:00" % int(time.mktime(t1.timetuple()))))
        view = res["plan_view"]
        self.assertIn("   시작: 지정 2026-11-16 15:00. 준비(설정, VM 전원)는 2026-11-16 14:00 부터", view)
        self.assertIn("   시작: 자동. 앞 케이스의 잡이 끝나고 30분 뒤", view)
        view0 = gen([case("a"), case("b")], st=settings(seq_gap_min=0))["plan_view"]
        self.assertIn("   시작: 자동. 이 ESX 호스트의 첫 잡이라 준비가 끝나는 대로", view0)
        self.assertIn("   시작: 자동. 앞 케이스가 끝나면 바로", view0)

    def test_errors_and_warnings(self):
        res = gen([case("bad", seq_start="soon")])
        self.assertTrue(any("bad" in e and "시작 시각" in e and "형식" in e for e in res["errors"]), res["errors"])
        res = gen([case("past", seq_start="2026-11-16 14:00")])                     # NOW 는 2026-11-16 14:30
        self.assertTrue(any("past" in e and "이미 지났음" in e for e in res["errors"]), res["errors"])
        res = gen([case("same-minute", seq_start="2026-11-16 14:30")])
        self.assertTrue(any("이미 지났음" in e for e in res["errors"]))
        res = gen([case("a", seq_start="18:00"), case("b", seq_start="17:00"), case("c", esx="esx02", seq_start="16:00")])
        self.assertEqual(res["errors"], [])
        self.assertTrue(any(w.startswith("2번째 케이스 b") and "앞 케이스의 시작 시각" in w for w in res["warnings"]), res["warnings"])
        self.assertFalse(any("3번째" in w for w in res["warnings"]), "ESX 호스트가 다른 케이스와는 비교하지 않음")
        res = gen([case("set-only", vms=[vm("esx01ev01", "(없음)", [], ["pin"])], seq_start="18:00")])
        self.assertEqual(res["errors"], [])
        self.assertTrue(any("잡이 없는 케이스" in w for w in res["warnings"]))
        self.assertNotIn("START ", res["plan"])
        applied = gen([case("done", seq_start="2026-11-16 09:00", applied=True), case("next")])
        self.assertEqual(applied["errors"], [], "적용 체크한 케이스의 지난 시각은 보지 않음")

    def test_start_times_nearer_than_the_spacing_are_an_error(self):
        """모든 잡 시작은 서로 '잡 시작 최소 간격'(기본 5분) 이상 떨어진다: ESX 호스트가 달라도 더 가까운 시작 시각은 오류."""
        res = gen([case("a1", seq_start="18:00"), case("b1", esx="esx02", seq_start="18:00")])
        self.assertTrue(any(e.startswith("2번째 케이스 b1") and "1번째 케이스 a1 의 시작 시각 2026-11-16 18:00:00 과 5분 이상 떨어져 있지 않음" in e
                            for e in res["errors"]), res["errors"])
        res = gen([case("a2", seq_start="18:00"), case("b2", seq_start="2026-11-16 18:00:00")])
        self.assertTrue(any("5분 이상 떨어져 있지 않음" in e for e in res["errors"]), "같은 ESX 호스트도 오류. 적는 형식이 달라도 같은 시각")
        res = gen([case("a3", seq_start="18:00"), case("b3", esx="esx02", seq_start="18:04")])
        self.assertTrue(any("5분 이상 떨어져 있지 않음" in e for e in res["errors"]), "4분 차이는 오류")
        res = gen([case("a3", seq_start="18:00"), case("b3", esx="esx02", seq_start="18:05"), case("c3", esx="esx02")])
        self.assertEqual(res["errors"], [], "5분 차이는 됨. 자동인 케이스는 실행할 때 다른 잡과 5분 이상 떨어진 시각을 고른다")
        self.assertIn("모든 잡 시작은 서로 5분 이상 떨어진다(다른 ESX 호스트, 이 점프 VM 의 다른 실행 포함)", res["plan_view"])
        self.assertIn("SET SPACING_MIN 5\n", res["plan"])
        res = gen([case("a5", seq_start="18:00"), case("b5", esx="esx02", seq_start="18:05")], st=settings(seq_start_spacing_min=10))
        self.assertTrue(any("10분 이상 떨어져 있지 않음" in e for e in res["errors"]), "간격 설정을 따른다")
        res = gen([case("a4", seq_start="18:00", applied=True), case("b4", esx="esx02", seq_start="18:00")])
        self.assertEqual(res["errors"], [], "적용 체크한 케이스는 보지 않음")
        res = gen([case("a6")], st=settings(seq_start_spacing_min=0))
        self.assertTrue(any("잡 시작 최소 간격 분" in e for e in res["errors"]), "간격은 1분 이상")

    def test_power_off_vm(self):
        """VM 행의 전원 칸이 끔이면 그 VM 은 케이스 동안 끈다. plan 에 OFF 줄, 실행 계획에 끄는 설명."""
        c = case("off1", vms=[vm("esx01ev01", "job_a"), vm("esx01ev02", power="off")])
        res = gen([c])
        self.assertEqual(res["errors"], [])
        self.assertIn("VM 001 esx01ev02 esx01ev02\nOFF 001 esx01ev02\n", res["plan"])
        self.assertIn("   VM 전원: esx01ev01 켬, esx01ev02 끔", res["plan_view"])
        self.assertIn("끔인 VM 은 끈다(게스트 종료, 10분 안에 꺼지지 않으면 전원 끄기)", res["plan_view"])
        bad = gen([case("off2", vms=[vm("esx01ev01", "job_a", power="off")])])
        self.assertTrue(any("전원 끔인데 잡이나 모니터링이 있음" in e for e in bad["errors"]), bad["errors"])
        noesx = gen([case("off3", esx="", vms=[vm("vm-a", "job_a"), vm("vm-b", power="off")])])
        self.assertTrue(any("VM 전원 끔에는 ESX 호스트가 있어야" in e for e in noesx["errors"]), noesx["errors"])
        only = gen([case("off4", vms=[vm("esx01ev01", power="off")])])
        self.assertEqual(only["errors"], [], "끌 VM 만 있는 케이스도 된다(VM 을 끄고 다음 케이스로)")
        self.assertIn("   시작: 잡 없음. 설정과 VM 전원만 맞추고 다음 케이스로 넘어간다.", only["plan_view"])

    def test_prepare_time_before_a_start_time(self):
        """시작 시각을 적은 케이스는 그 시각 '시작 시각 전 준비 분'(기본 60분) 전에 설정 적용과 VM 전원 맞추기를 시작한다."""
        res = gen([case("p1", seq_start="20:00"), case("p2")])
        self.assertIn("SET PREP_LEAD_MIN 60\n", res["plan"])
        self.assertIn("   시작: 지정 2026-11-16 20:00. 준비(설정, VM 전원)는 2026-11-16 19:00 부터", res["plan_view"])
        self.assertIn("설정 적용과 VM 전원 맞추기는 그 시각 60분 전에 시작한다", res["plan_view"])
        res = gen([case("p3", seq_start="20:00")], st=settings(seq_prep_lead_min=0))
        self.assertIn("SET PREP_LEAD_MIN 0\n", res["plan"])
        self.assertIn("   시작: 지정 2026-11-16 20:00\n", res["plan_view"], "준비 0분이면 준비 시각을 적지 않음")
        self.assertIn("설정 적용과 VM 전원 맞추기는 차례가 오는 대로 한다", res["plan_view"])
        self.assertEqual(core.DEFAULT_SETTINGS["seq_prep_lead_min"], 60)

    def test_time_mode_ignores_the_start_time_of_sequence_mode(self):
        c = case("t1", seq_start="soon")
        c.update(start="2026-11-20 10:00:00")
        res = gen([c], st=settings(run_mode="time"))
        self.assertEqual(res["errors"], [], "시각 지정에서는 순서 실행의 시작 시각 칸을 쓰지 않음")

    def test_case_field_defaults(self):
        self.assertEqual(core.new_case("esx01", "", 1)["seq_start"], "")
        self.assertEqual(core._clean_case({"name": "x", "vms": []})["seq_start"], "", "예전 파일에는 이 칸이 없음")
        self.assertEqual(core._clean_case({"name": "x", "vms": [], "seq_start": None})["seq_start"], "")


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
        self.assertEqual(rows[0][-2:], ["시작 시각 (순서 실행)", "전원"], "새 열은 맨 뒤 (이 열이 없는 예전 파일도 읽힌다)")
        old = "\r\n".join(",".join(r[:-2]) for r in csv.reader(io.StringIO(core.cases_to_csv([case("old-file")])))) + "\r\n"
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
        res = core.generate([case("c1")], copy.deepcopy(RUN), copy.deepcopy(MON), core.resolve_paths(self.d, st), now=NOW, write=True, profiles=[])
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
        self.assertEqual(core.PLAN_FORMAT, "TCGEN-PLAN 2")
        self.assertIn('= "TCGEN-PLAN 2" ]', payload.TCRUN_SH)

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
        res = core.generate([case("d1")], RUN, MON, st, now=NOW, write=False, profiles=PROF)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["jump_dir"], "/data/tcgen")
        for blk in (res["check"], res["run"]):
            self.assertIn("D=/data/tcgen; R=\\$D/runs/%s" % res["run_id"], blk)
        self.assertIn("sh /data/tcgen/vmssh esx01ev01 << TCEOF", res["vm_wipe"])
        self.assertIn("sh /data/tcgen/postsetup", res["setup"])
        res0 = core.generate([case("d1")], RUN, MON, settings(), now=NOW, write=False, profiles=PROF)
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
