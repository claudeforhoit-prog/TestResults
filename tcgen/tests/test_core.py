# -*- coding: utf-8 -*-
"""tcgen_core 단위 시험. 실행: python -m unittest discover -s tests -v"""
import copy
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tcgen_core as core  # noqa: E402

NOW = dt.datetime(2026, 11, 9, 15, 30, 12)

RUN = [
    {"app": "AppA", "name": "job_a", "desc": "1 job",
     "cmd": "/home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1"},
    {"app": "AppB", "name": "job_b4", "desc": "4 jobs", "cmd": "/home/tester/bench/run_b.sh -n 4 -tag x%y"},
]
MON = [
    {"app": "mon", "name": "guest_perf", "desc": "", "where": "VM",
     "start": "/home/tester/mon/start_mon.sh >> /home/tester/log/mon-<vm이름>-<실행시각>.log",
     "stop": "/home/tester/mon/stop_mon.sh"},
    {"app": "mon", "name": "host_mon", "desc": "", "where": "ESX",
     "start": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start '%H'", "stop": "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop"},
    {"app": "mon", "name": "guest_gpu", "desc": "GPU", "where": "VM",
     "start": "/home/tester/mon/gpu_mon.sh", "stop": "/home/tester/mon/gpu_mon.sh --stop"},
]


def case(name, start, interval="12:00", pvm_job="job_a", svm_job="(없음)", monitors=None, stop_mode="auto", stop_time=""):
    """예전 형식(PVM, SVM 칸)으로 만들어 core._clean_case 로 바꾼다. 예전 형식 변환도 함께 시험된다."""
    old = {"name": name, "esx": "esx01", "pvm": "esx01ev01", "svm": "esx01ev02", "pvm_job": pvm_job, "svm_job": svm_job,
           "interval": interval, "start": start, "stop_mode": stop_mode, "stop_time": stop_time,
           "monitors": copy.deepcopy(monitors) if monitors is not None else
           [{"name": "guest_perf", "targets": ["PVM", "SVM"]}, {"name": "host_mon", "targets": ["ESX"]}]}
    return core._clean_case(old)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="tcgen_test_")
        self.settings = core.resolve_paths(self.base, core.load_settings(self.base))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class TimeTests(unittest.TestCase):
    def test_parse_formats(self):
        self.assertEqual(core.parse_dt("2026-11-09 10:00:00"), dt.datetime(2026, 11, 9, 10, 0))
        self.assertEqual(core.parse_dt("2026-11-09 10:05"), dt.datetime(2026, 11, 9, 10, 5))
        self.assertEqual(core.parse_dt("2026/11/09 9:05"), dt.datetime(2026, 11, 9, 9, 5))
        self.assertEqual(core.fmt_dt(dt.datetime(2026, 11, 9, 9, 5)), "2026-11-09 09:05:00")
        with self.assertRaises(ValueError):
            core.parse_dt("2026-11-09 10:00:30")
        with self.assertRaises(ValueError):
            core.parse_dt("11-09 10:00")

    def test_interval(self):
        self.assertEqual(core.parse_interval("12:30"), dt.timedelta(hours=12, minutes=30))
        self.assertEqual(core.parse_interval("8"), dt.timedelta(hours=8))
        self.assertIsNone(core.parse_interval(""))
        self.assertIsNone(core.parse_interval("-"))
        for bad in ("12:60", "0:00", "a", "1:2:3"):
            with self.assertRaises(ValueError):
                core.parse_interval(bad)
        self.assertEqual(core.fmt_interval(dt.timedelta(hours=36, minutes=5)), "36:05")

    def test_cascade(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", ""), case("C", "")]
        core.recalc_starts(cs)
        self.assertEqual([c["start"] for c in cs], ["2026-11-20 00:00:00", "2026-11-20 12:00:00", "2026-11-21 00:00:00"])
        cs[1]["interval"] = "8:00"
        core.recalc_starts(cs, 1)
        self.assertEqual([c["start"] for c in cs], ["2026-11-20 00:00:00", "2026-11-20 08:00:00", "2026-11-20 20:00:00"])
        cs[1]["start"] = "2026-11-20 09:30:00"           # 잡 시작을 직접 고치면 간격을 맞추고 아래 행이 따라 움직인다
        core.interval_from_starts(cs, 1)
        core.recalc_starts(cs, 2)
        self.assertEqual(cs[1]["interval"], "9:30")
        self.assertEqual(cs[2]["start"], "2026-11-20 21:30:00")
        cs[0]["start"] = "2026-11-20 01:00:00"           # 첫 행을 고치면 간격은 그대로, 전체가 1시간 뒤로
        core.interval_from_starts(cs, 0)
        core.recalc_starts(cs, 1)
        self.assertEqual(cs[0]["interval"], "12:00")
        self.assertEqual([c["start"] for c in cs], ["2026-11-20 01:00:00", "2026-11-20 10:30:00", "2026-11-20 22:30:00"])

    def test_schedule(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "2026-11-20 12:00:00"),
              case("C", "2026-11-21 00:00:00", interval="6:00")]
        s = core.case_schedule(cs, 5, 10)
        self.assertEqual(s[0]["mon_start"], dt.datetime(2026, 11, 19, 23, 55))
        self.assertEqual(s[0]["mon_stop"], dt.datetime(2026, 11, 20, 11, 50))     # 다음 케이스 10분 전
        self.assertEqual(s[2]["mon_stop"], dt.datetime(2026, 11, 21, 5, 50))      # 마지막: 자기 간격 기준
        cs[2]["stop_mode"] = "none"
        self.assertIsNone(core.case_schedule(cs, 5, 10)[2]["mon_stop"])
        cs[2]["stop_mode"] = "manual"
        cs[2]["stop_time"] = "2026-11-21 03:00"
        self.assertEqual(core.case_schedule(cs, 5, 10)[2]["mon_stop"], dt.datetime(2026, 11, 21, 3, 0))
        cs[2]["stop_mode"] = "auto"
        cs[2]["interval"] = ""
        self.assertIn("마지막 케이스", core.case_schedule(cs, 5, 10)[2]["stop_error"])


class CommandTests(unittest.TestCase):
    def test_vm_percent(self):
        r = core.render_command("/x/a.sh -f '%Y' -g \\%m <실행시각>")
        self.assertTrue(r.startswith("TC_START=`date +\\%Y\\%m\\%d\\%H\\%M\\%S`; "))
        self.assertIn("-f '\\%Y' -g \\%m ${TC_START}", r)

    def test_esx_percent_unchanged(self):
        r = core.render_command("/bin/sh /x/m.sh '%H' <vm이름> <실행시각>", esx=True)
        self.assertIn("'%H' ${TC_HOST} ${TC_START}", r)
        self.assertIn("TC_START=`TZ=KST-9 date +", r)
        self.assertIn("TC_HOST=`hostname -s`; ", r)

    def test_variables_spacing(self):
        r = core.render_command("/x/a.sh > /l/a-< VM 이름 >-<실행 시각>.log")
        self.assertIn("/l/a-${TC_HOST}-${TC_START}.log", r)

    def test_cron_line_utc(self):
        t = dt.datetime(2026, 11, 20, 0, 5)
        self.assertTrue(core.cron_line(t, "/x/a.sh", "A", "esx01ev01").startswith("05 00 20 11 * /x/a.sh # tcgen A esx01ev01"))
        self.assertTrue(core.cron_line(t, "/x/m.sh", "A", "esx01", esx=True).startswith("05 15 19 11 * /x/m.sh # tcgen A esx01"))

    def test_check_command(self):
        self.assertEqual(core.check_command("/x/a.sh -o <vm이름>", "c"), [])
        self.assertTrue(core.check_command("", "c"))
        self.assertTrue(any("!" in e for e in core.check_command("/x/a.sh !$", "c")))
        self.assertTrue(any("탭" in e for e in core.check_command("/x/a.sh\t-o", "c")))
        self.assertTrue(any("ASCII" in e for e in core.check_command("/x/한글.sh", "c")))


class EntryTests(TmpDir):
    def test_entries_and_blocks(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "", svm_job="job_b4"),
              case("C", "", svm_job="job_b4", stop_mode="none", monitors=[{"name": "guest_perf", "targets": ["PVM"]}])]
        core.recalc_starts(cs)
        res = core.generate(cs, RUN, MON, self.settings, now=NOW)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["warnings"], [])
        kinds = [(e["case"], e["host"], e["kind"]) for e in res["entries"]]
        self.assertIn(("A", "esx01", "모니터링 시작"), kinds)
        self.assertIn(("C", "esx01ev01", "모니터링 시작"), kinds)
        self.assertNotIn(("C", "esx01ev01", "모니터링 중지"), kinds)          # 중지 없음
        self.assertNotIn(("C", "esx01ev02", "모니터링 시작"), kinds)          # C 는 PVM 만 모니터링
        self.assertEqual(sorted(res["esx"]), ["esx01"])
        for text in [res["vm"], res["setup"], res["vm_wipe"]] + list(res["esx"].values()):
            self.assertTrue(all(ord(ch) < 127 for ch in text), "블록은 ASCII 만")
            self.assertNotIn("!", text)
            for ln in text.splitlines():
                self.assertLessEqual(len(ln), 160)
        self.assertEqual(res["vm"].count(" << TCEOF\n"), 2)
        self.assertEqual(res["vm"].count("\nTCEOF\n"), 2)
        self.assertNotIn("'TCEOF'", res["vm"])
        self.assertIn(" # tcgen A esx01ev01$", core.hd_unescape(res["vm"]))
        self.assertTrue(res["setup"].startswith("sh << TCSETUP\n"))
        self.assertTrue(res["setup"].endswith("\nTCSETUP\nsh ~/.tcgen/setpw\n"))
        self.assertIn("/bin/echo root >> /var/spool/cron/crontabs/cron.update", res["esx"]["esx01"])

    def test_wipe_block_and_conflicts(self):
        cs = [case("OLD", "2026-11-01 00:00:00", stop_mode="none"), case("PEND", "2026-11-20 00:00:00"),
              case("NEW", "2026-11-21 00:00:00")]
        cs.append(core._clean_case({"name": "OTHER", "esx": "esx02", "start": "2026-11-22 00:00:00", "applied": True,
                                    "vms": [{"name": "esx02ev01", "job": "job_a", "monitors": []}]}))
        cs[0]["applied"] = cs[1]["applied"] = True
        self.settings["vm_user"] = "tester"
        res = core.generate(cs, RUN, MON, self.settings, now=NOW, write=False)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["wipe_hosts"], ["esx01ev01", "esx01ev02"], "B 블록과 같은 VM")
        self.assertEqual(res["wipe_conflicts"], ["PEND"],
                         "실행할 시각이 남은 적용 체크 케이스만 (끝난 케이스, 다른 VM 케이스는 제외)")
        w = core.hd_unescape(res["vm_wipe"])
        self.assertEqual(re.findall(r"^sh ~/\.tcgen/vmssh (\S+) << TCEOF$", w, re.M), ["tester@esx01ev01", "tester@esx01ev02"])
        self.assertEqual(w.count("\nTCEOF\n"), 2)
        self.assertEqual(w.count("crontab -r &&"), 2)
        self.assertEqual(w.count("tcgen_crontab_backup-`date +%Y%m%d%H%M%S`.txt"), 2, "지우기 전 내용을 백업")
        self.assertNotIn("esx01\n", w, "ESX crontab 은 지우지 않음")
        self.assertNotIn("!", res["vm_wipe"])
        self.assertTrue(all(ord(ch) < 127 for ch in res["vm_wipe"]))
        self.assertTrue(all(len(ln) <= 160 for ln in res["vm_wipe"].splitlines()))
        res2 = core.generate(cs[2:3], RUN, MON, self.settings, now=NOW, write=False)
        self.assertEqual(res2["wipe_conflicts"], [], "적용 체크한 케이스가 없으면 주의 없음")

    def test_vm_user(self):
        self.settings["vm_user"] = "tester"
        res = core.generate([case("A", "2026-11-20 00:00:00", monitors=[])], RUN, MON, self.settings, now=NOW, write=False)
        self.assertTrue(res["vm"].startswith("sh ~/.tcgen/vmssh tester@esx01ev01 << TCEOF\n"))

    def test_marker_escape(self):
        res = core.generate([case("A.1", "2026-11-20 00:00:00", monitors=[])], RUN, MON, self.settings, now=NOW, write=False)
        self.assertIn("' # tcgen A\\.1 esx01ev01$'", core.hd_unescape(res["vm"]))

    def test_heredoc_escape(self):
        for s in ("a$b", "x`y`z", "back\\slash \\% \\n", "plain 'q' \"dq\" %H"):
            self.assertEqual(core.hd_unescape(core._hd(s)), s)
        self.assertEqual(core._hd("$HOME `d` \\%"), "\\$HOME \\`d\\` \\\\%")

    def test_errors(self):
        st = self.settings
        e = core.generate([case("A", "2026-11-20 00:00:00"), case("A", "2026-11-20 12:00:00")], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("중복" in x for x in e))
        e = core.generate([case("A b", "2026-11-20 00:00:00")], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("케이스 이름" in x for x in e))
        e = core.generate([case("A", "2026-11-20 00:00:00", pvm_job="nojob")], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("탭 1 에 없음" in x for x in e))
        e = core.generate([case("A", "2026-11-20 00:00:00", monitors=[{"name": "host_mon", "targets": ["PVM"]}])],
                          RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("실행 위치는 ESX" in x for x in e))
        c = case("A", "2026-11-20 00:00:00")
        c["vms"][0]["name"] = ""
        e = core.generate([c], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("이름이 비었거나" in x for x in e), e)
        e = core.generate([case("A", "2026-11-20 00:00:00")], RUN + [dict(RUN[0])], MON, st, now=NOW)["errors"]
        self.assertTrue(any("두 번 이상" in x for x in e))
        st2 = dict(st, stop_lead_min=5, mon_lead_min=5)
        e = core.generate([case("A", "2026-11-20 00:00:00")], RUN, MON, st2, now=NOW)["errors"]
        self.assertTrue(any("커야 함" in x for x in e))
        e = core.generate([case("A", "2026-11-20 00:00:00", interval="")], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("마지막 케이스" in x for x in e))
        bad = [dict(RUN[0], cmd="/x/a.sh !!")]
        e = core.generate([case("A", "2026-11-20 00:00:00", monitors=[])], bad, MON, st, now=NOW)["errors"]
        self.assertTrue(any("! 문자" in x for x in e))
        self.assertFalse(os.path.exists(st["record_csv"]), "오류가 있으면 기록하지 않는다")

    def test_past_warning(self):
        res = core.generate([case("A", "2026-11-09 15:00:00", monitors=[])], RUN, MON, self.settings, now=NOW, write=False)
        self.assertTrue(any("지난 시각" in w for w in res["warnings"]))

    def test_monitor_only_and_nothing(self):
        res = core.generate([case("A", "2026-11-20 00:00:00", pvm_job="(없음)")], RUN, MON, self.settings, now=NOW, write=False)
        self.assertEqual(res["errors"], [])
        self.assertFalse(any(e["kind"] == "잡" for e in res["entries"]))
        res = core.generate([case("A", "2026-11-20 00:00:00", pvm_job="(없음)", monitors=[])], RUN, MON, self.settings,
                            now=NOW, write=False)
        self.assertTrue(res["errors"])


class AppliedTests(TmpDir):
    """적용 체크: 체크한 케이스는 생성에서 빠지고, 저장 후 다시 불러와도 남는다."""

    def test_applied_skipped(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "2026-11-20 12:00:00"), case("C", "2026-11-21 00:00:00")]
        cs[0]["applied"] = True
        res = core.generate(cs, RUN, MON, self.settings, now=NOW)
        self.assertEqual(res["errors"], [])
        self.assertEqual(sorted(set(e["case"] for e in res["entries"])), ["B", "C"])
        self.assertEqual(res["skipped"], 1)
        self.assertNotIn(" # tcgen A ", core.hd_unescape(res["vm"]))
        with open(self.settings["record_csv"], encoding="utf-8-sig") as f:
            self.assertEqual([r[0] for r in list(csv.reader(f))[1:]], ["B", "C"])

    def test_applied_row_still_sets_previous_stop(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "2026-11-20 09:00:00")]
        cs[1]["applied"] = True
        res = core.generate(cs, RUN, MON, self.settings, now=NOW, write=False)
        stops = [e for e in res["entries"] if e["case"] == "A" and e["kind"] == "모니터링 중지"]
        self.assertTrue(stops)
        self.assertTrue(all(e["when"] == dt.datetime(2026, 11, 20, 8, 50) for e in stops), "B 잡 시작 10분 전")

    def test_duplicate_name_with_applied(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("A", "2026-11-20 12:00:00")]
        cs[0]["applied"] = True
        e = core.generate(cs, RUN, MON, self.settings, now=NOW, write=False)["errors"]
        self.assertTrue(any("적용 체크한 행 포함" in x for x in e), e)

    def test_all_applied(self):
        cs = [case("A", "2026-11-20 00:00:00")]
        cs[0]["applied"] = True
        e = core.generate(cs, RUN, MON, self.settings, now=NOW, write=False)["errors"]
        self.assertTrue(any("모든 케이스가 적용 체크" in x for x in e), e)

    def test_cases_roundtrip_and_backup(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "2026-11-20 12:00:00", stop_mode="none")]
        cs[0]["applied"] = True
        core.save_cases(self.base, cs)
        got = core.load_cases(self.base)
        self.assertEqual([c["name"] for c in got], ["A", "B"])
        self.assertEqual([c["applied"] for c in got], [True, False])
        self.assertEqual(got[1]["stop_mode"], "none")
        self.assertEqual(got[0]["vms"], cs[0]["vms"])
        self.assertEqual(got[0]["esx_monitors"], ["host_mon"])
        with open(os.path.join(self.base, core.CASES_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "cases": [{"name": "X", "applied": 1, "pvm": "h1ev01",
                                                "monitors": [{"name": "m", "targets": ["PVM", "BAD"]}],
                                                "stop_mode": "odd"}]}, f)
        got = core.load_cases(self.base)
        self.assertEqual(got[0]["applied"], True)
        self.assertEqual(got[0]["vms"][0], {"name": "h1ev01", "job": "(없음)", "monitors": ["m"], "end_ref": False})
        self.assertEqual(got[0]["vms"][1], {"name": "", "job": "(없음)", "monitors": [], "end_ref": False})
        self.assertEqual(got[0]["stop_mode"], "auto")
        dest, copied = core.backup_settings(self.base, now=NOW)
        self.assertIn(core.CASES_FILE, copied)
        self.assertEqual(core.load_cases(os.path.join(self.base, "missing")), [])


class DataDirTests(unittest.TestCase):
    """데이터 폴더 분리, 예전 판 데이터 옮기기, 백업과 불러오기, 데이터 폴더 변경. 실제 사용자 폴더는 건드리지 않는다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tcgen_dd_")
        self.saved_env = {k: os.environ.get(k) for k in ("TCGEN_CONFIG_DIR", "TCGEN_DEFAULT_DATA_DIR")}
        os.environ["TCGEN_CONFIG_DIR"] = os.path.join(self.tmp, "cfg")
        os.environ["TCGEN_DEFAULT_DATA_DIR"] = os.path.join(self.tmp, "data")
        self.prog = os.path.join(self.tmp, "prog")
        os.makedirs(self.prog)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_data(self, d):
        os.makedirs(d, exist_ok=True)
        core.save_settings(d, core.resolve_paths(d, dict(core.load_settings(d), vm_user="tester")))
        core.save_rows(os.path.join(d, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS, RUN)
        os.makedirs(os.path.join(d, "testcases"))
        with open(os.path.join(d, "testcases", "test_record.csv"), "w") as f:
            f.write("a\n")
        os.makedirs(os.path.join(d, "backup", "20261101000000"))
        with open(os.path.join(d, "backup", "20261101000000", core.SETTINGS_FILE), "w") as f:
            f.write("{}")

    def test_default_and_config(self):
        self.assertEqual(core.data_dir(), os.path.normpath(os.path.join(self.tmp, "data")))
        core.save_config({"data_dir": os.path.join(self.tmp, "other")})
        self.assertEqual(core.data_dir(), os.path.normpath(os.path.join(self.tmp, "other")))

    def test_migrate_old_program_folder(self):
        self.write_data(self.prog)
        st = core.load_settings(self.prog)
        st["log_dir"] = os.path.join(self.prog, "mylogs")             # 생성기 폴더 안을 가리키던 경로
        core.save_settings(self.prog, core.resolve_paths(self.prog, st))
        data = core.data_dir()
        done = core.migrate_from(self.prog, data, now=NOW)
        self.assertIn(core.SETTINGS_FILE, done)
        self.assertIn(core.RUN_FILE, done)
        self.assertTrue(os.path.exists(os.path.join(data, "testcases", "test_record.csv")))
        self.assertTrue(os.path.exists(os.path.join(data, "backup", "20261101000000", core.SETTINGS_FILE)))
        self.assertEqual(core.load_settings(data)["vm_user"], "tester")
        self.assertEqual(core.load_settings(data)["log_dir"], os.path.join(data, "mylogs"))
        self.assertFalse(core.has_data(self.prog), "복사한 뒤 생성기 폴더의 데이터 파일은 옆 폴더로 옮김")
        self.assertTrue(core.has_data(os.path.join(self.prog, "moved_to_data_folder_20261109153012")))
        self.assertEqual(core.migrate_from(self.prog, data), [])

    def test_no_migrate_when_data_exists(self):
        self.write_data(self.prog)
        data = core.data_dir()
        os.makedirs(data)
        core.save_rows(os.path.join(data, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS, MON)
        self.assertEqual(core.migrate_from(self.prog, data), [])
        self.assertTrue(core.has_data(self.prog))

    def test_backup_and_restore(self):
        data = core.data_dir()
        self.write_data(data)
        dest, done = core.backup_to(data, os.path.join(self.tmp, "bk"), now=NOW)
        self.assertEqual(os.path.basename(dest), "tcgen_backup_20261109153012")
        self.assertTrue(os.path.exists(os.path.join(dest, core.RUN_FILE)))
        self.assertTrue(os.path.exists(os.path.join(dest, "testcases", "test_record.csv")))
        self.assertFalse(os.path.exists(os.path.join(dest, "backup")), "백업 안에 이전 백업은 넣지 않음")
        core.save_rows(os.path.join(data, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS, RUN[:1])
        with open(os.path.join(data, "testcases", "test_record.csv"), "w") as f:
            f.write("a\nb\n")                                   # 백업 뒤에 쌓인 기록
        keep, _ = core.restore_from(dest, data, now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(core.load_rows(os.path.join(data, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)), 2)
        with open(os.path.join(data, "testcases", "test_record.csv")) as f:
            self.assertEqual(f.read(), "a\nb\n", "기록 파일은 덮어쓰지 않음")
        self.assertEqual(len(core.load_rows(os.path.join(keep, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)), 1,
                         "불러오기 전 데이터를 따로 복사해 둠")
        with self.assertRaises(ValueError):
            core.restore_from(os.path.join(self.tmp, "no_such_backup"), data)
        with self.assertRaises(ValueError):
            core.restore_from(data, data)

    def test_change_data_dir(self):
        data = core.data_dir()
        self.write_data(data)
        new = os.path.join(self.tmp, "moved")
        done = core.change_data_dir(data, new)
        self.assertIn(core.RUN_FILE, done)
        self.assertEqual(core.data_dir(), os.path.normpath(new))
        self.assertTrue(core.has_data(data), "이전 폴더의 파일은 지우지 않음")
        other = os.path.join(self.tmp, "other_with_data")
        os.makedirs(other)
        core.save_rows(os.path.join(other, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS, MON)
        self.assertEqual(core.change_data_dir(new, other, copy_current=False), [])
        self.assertFalse(os.path.exists(os.path.join(other, core.RUN_FILE)), "데이터가 있는 폴더로 바꿀 때는 복사하지 않음")


class VmTests(TmpDir):
    """케이스마다 VM 여러 대: VM 별 잡과 모니터링, ESX 모니터링, 이름 자동 번호, 예전 형식 변환."""

    def vm_case(self, name, start, vms, esx_monitors=(), interval="12:00", stop_mode="auto"):
        return core._clean_case({"name": name, "esx": "esx01", "vms": vms, "esx_monitors": list(esx_monitors),
                                 "interval": interval, "start": start, "stop_mode": stop_mode})

    def read(self, path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    def test_four_vms_each_own_job_and_monitors(self):
        c = self.vm_case("TC-4vm", "2026-11-20 12:00:00", [
            {"name": "esx01ev01", "job": "job_a", "monitors": ["guest_perf"]},
            {"name": "esx01ev02", "job": "job_a", "monitors": []},
            {"name": "esx01ev03", "job": "job_b4", "monitors": ["guest_perf", "guest_gpu"]},
            {"name": "esx01ev04", "job": "(없음)", "monitors": []}], esx_monitors=["host_mon"])
        res = core.generate([c], RUN, MON, self.settings, now=NOW)
        self.assertEqual(res["errors"], [])
        got = set((e["host"], e["kind"], e["name"]) for e in res["entries"])
        self.assertIn(("esx01ev01", "잡", "job_a"), got)
        self.assertIn(("esx01ev03", "모니터링 시작", "guest_gpu"), got)
        self.assertIn(("esx01ev03", "모니터링 중지", "guest_gpu"), got)
        self.assertNotIn(("esx01ev02", "모니터링 시작", "guest_perf"), got, "VM 마다 모니터링이 다름")
        self.assertIn(("esx01", "모니터링 시작", "host_mon"), got)
        self.assertFalse(any(e["host"] == "esx01ev04" for e in res["entries"]), "잡도 모니터링도 없는 VM 은 등록하지 않음")
        vm_text = core.hd_unescape(res["vm"])
        self.assertEqual(vm_text.count("<< TCEOF"), 3)
        for h in ("esx01ev01", "esx01ev02", "esx01ev03"):
            self.assertIn("sh ~/.tcgen/vmssh %s << TCEOF" % h, vm_text)
        rows = self.read(self.settings["record_csv"])
        self.assertEqual(rows[1][3], "esx01ev01=job_a, esx01ev02=job_a, esx01ev03=job_b4, esx01ev04=(없음)")
        self.assertEqual(rows[1][6], "esx01ev01:guest_perf, esx01ev03:guest_perf+guest_gpu, esx01:host_mon")
        log = self.read(res["log"])
        hdr = log[0]
        job3 = [r for r in log[1:] if r[hdr.index("구분")] == "잡" and r[hdr.index("대상")] == "esx01ev03"]
        self.assertEqual(job3[0][hdr.index("실행 위치")], "VM")
        self.assertEqual(job3[0][hdr.index("설명")], "4 jobs")
        esx_rows = [r for r in log[1:] if r[hdr.index("대상")] == "esx01"]
        self.assertTrue(esx_rows and all(r[hdr.index("실행 위치")] == "ESX" and r[hdr.index("crontab 시각 기준")] == "UTC"
                                         for r in esx_rows))

    def test_wrong_place_and_duplicate_vm(self):
        c = self.vm_case("A", "2026-11-20 12:00:00", [{"name": "esx01ev01", "job": "job_a", "monitors": ["host_mon"]}])
        e = core.generate([c], RUN, MON, self.settings, now=NOW, write=False)["errors"]
        self.assertTrue(any("실행 위치는 ESX" in x and "케이스 행" in x for x in e), e)
        c = self.vm_case("A", "2026-11-20 12:00:00", [{"name": "esx01ev01", "job": "job_a"}], esx_monitors=["guest_perf"])
        e = core.generate([c], RUN, MON, self.settings, now=NOW, write=False)["errors"]
        self.assertTrue(any("실행 위치는 VM" in x and "VM 행" in x for x in e), e)
        c = self.vm_case("A", "2026-11-20 12:00:00", [{"name": "esx01ev01", "job": "job_a"}, {"name": "esx01ev01", "job": "job_b4"}])
        e = core.generate([c], RUN, MON, self.settings, now=NOW, write=False)["errors"]
        self.assertTrue(any("같은 VM 이름" in x for x in e), e)
        c = self.vm_case("A", "2026-11-20 12:00:00", [], esx_monitors=["host_mon"])
        res = core.generate([c], RUN, MON, self.settings, now=NOW, write=False)
        self.assertEqual(res["errors"], [])
        self.assertTrue(res["entries"] and all(e["esx"] for e in res["entries"]), "VM 없이 ESX 모니터링만 있는 케이스")

    def test_vm_naming(self):
        c = core.new_case("esx01", "2026-11-20 00:00:00", count=2)
        self.assertEqual([v["name"] for v in c["vms"]], ["esx01ev01", "esx01ev02"])
        self.assertEqual(core.next_vm_number(c), 3)
        c["vms"].append({"name": core.vm_name("esx01", core.next_vm_number(c)), "job": "(없음)", "monitors": []})
        self.assertEqual(c["vms"][-1]["name"], "esx01ev03")
        c["vms"][1]["name"] = "custom-vm"
        core.rename_vms_for_esx(c, "esx01", "esx02")
        self.assertEqual([v["name"] for v in c["vms"]], ["esx02ev01", "custom-vm", "esx02ev03"], "자동 이름만 따라 바뀜")
        e = core.new_case("", "", count=2)
        self.assertEqual([v["name"] for v in e["vms"]], ["", ""])
        core.rename_vms_for_esx(e, "", "esx05")
        self.assertEqual([v["name"] for v in e["vms"]], ["esx05ev01", "esx05ev02"], "빈 이름은 번호를 채움")
        self.assertEqual(core.next_vm_number({"esx": "esx01", "vms": [{"name": "a"}, {"name": "b"}]}), 3)

    def test_upgrade_old_cases_file(self):
        with open(os.path.join(self.base, core.CASES_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "cases": [{"name": "OLD", "esx": "esx01", "pvm": "esx01ev01", "svm": "esx01ev02",
                                                "pvm_job": "job_a", "svm_job": "job_b4",
                                                "monitors": [{"name": "guest_perf", "targets": ["SVM"]},
                                                             {"name": "host_mon", "targets": ["ESX"]}]}]}, f)
        self.assertEqual(core.cases_file_version(self.base), 1)
        dest = core.upgrade_cases_file(self.base, now=NOW)
        self.assertTrue(dest.endswith("20261109153012_before_vm_format"))
        self.assertTrue(os.path.exists(os.path.join(dest, core.CASES_FILE)), "바꾸기 전 원본을 복사해 둠")
        self.assertEqual(core.cases_file_version(self.base), core.CASES_VERSION)
        c = core.load_cases(self.base)[0]
        self.assertEqual(c["vms"], [{"name": "esx01ev01", "job": "job_a", "monitors": [], "end_ref": False},
                                    {"name": "esx01ev02", "job": "job_b4", "monitors": ["guest_perf"], "end_ref": False}])
        self.assertEqual(c["esx_monitors"], ["host_mon"])
        self.assertIsNone(core.upgrade_cases_file(self.base), "이미 새 형식이면 다시 바꾸지 않음")


class RecordTests(TmpDir):
    def read(self, path, enc="utf-8-sig"):
        with open(path, encoding=enc, newline="") as f:
            return list(csv.reader(f))

    def test_new_and_append(self):
        cs = [case("A", "2026-11-20 00:00:00"), case("B", "2026-11-20 12:00:00")]
        r1 = core.generate(cs, RUN, MON, self.settings, now=NOW)
        rows = self.read(self.settings["record_csv"])
        self.assertEqual(rows[0], core.RECORD_HEADER)
        self.assertEqual([r[0] for r in rows[1:]], ["A", "B"])
        self.assertEqual(rows[1][1], "2026-11-09 15:30:12")
        self.assertEqual(rows[1][3], "esx01ev01=job_a, esx01ev02=(없음)")
        self.assertEqual(rows[1][6], "esx01ev01:guest_perf, esx01ev02:guest_perf, esx01:host_mon")
        h = core.RECORD_HEADER
        self.assertEqual([rows[1][h.index(k)] for k in ("설명", "테스트 그룹", "잡 종료(리포트)", "종료 기준 VM")], ["", "", "", ""])
        self.assertTrue(os.path.basename(r1["log"]) == "testcaselogging-20261109153012.csv")
        log = self.read(r1["log"])
        self.assertEqual(log[0], core.LOG_HEADER)
        self.assertEqual(len(log) - 1, len(r1["entries"]))
        core.generate([case("C", "2026-11-21 00:00:00")], RUN, MON, self.settings, now=NOW + dt.timedelta(minutes=1))
        rows = self.read(self.settings["record_csv"])
        self.assertEqual([r[0] for r in rows[1:]], ["A", "B", "C"])
        self.assertEqual(len(os.listdir(self.settings["log_dir"])), 3)    # 기록 CSV 1개 + 로그 2개

    def test_existing_other_header_cp949(self):
        p = self.settings["record_csv"]
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="cp949", newline="") as f:
            f.write("케이스,잡 시작(KST),비고\r\nOLD,2026-11-01 00:00:00,기존 행")      # 마지막 줄바꿈 없음
        bk = os.path.join(self.base, "bk")
        res = core.generate([case("A", "2026-11-20 00:00:00")], RUN, MON, self.settings, now=NOW, backup_dir=bk)
        rows = self.read(p, enc="cp949")
        added = [h for h in core.RECORD_HEADER[1:] if h != "잡 시작(KST)"]
        self.assertEqual(rows[0], ["케이스", "잡 시작(KST)", "비고"] + added, "머리글 끝에 새 열을 덧붙임")
        self.assertEqual(rows[1], ["OLD", "2026-11-01 00:00:00", "기존 행"] + [""] * len(added), "기존 행은 새 칸을 비움")
        self.assertEqual(rows[2][:3], ["A", "2026-11-20 00:00:00", ""])
        self.assertEqual(rows[2][3 + added.index("VM 구성")], "esx01ev01=job_a, esx01ev02=(없음)")
        self.assertEqual(res["record_added"], added)
        self.assertTrue(os.path.basename(res["record_backup"]).startswith("20261109153012_before_header_change_"))
        with open(res["record_backup"], encoding="cp949") as f:
            self.assertTrue(f.read().startswith("케이스,잡 시작(KST),비고"), "덧붙이기 전 원본을 복사해 둠")
        core.generate([case("B", "2026-11-21 00:00:00")], RUN, MON, self.settings, now=NOW + dt.timedelta(minutes=1))
        rows = self.read(p, enc="cp949")
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[3]), len(rows[0]), "두 번째부터는 같은 머리글로 이어서 기록")

    def test_encrypted_record(self):
        p = self.settings["record_csv"]
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"DRM\x00\x01\x02binary")
        with self.assertRaises(ValueError):
            core.generate([case("A", "2026-11-20 00:00:00")], RUN, MON, self.settings, now=NOW)


class ReportTests(TmpDir):
    """성능 리포트 생성기용 케이스 CSV."""

    def make(self):
        def vm(name, job="(없음)", monitors=(), end_ref=False):
            return {"name": name, "job": job, "monitors": list(monitors), "end_ref": end_ref}
        a = core._clean_case({"name": "TC-a", "esx": "esx01", "start": "2026-11-20 00:00:00", "interval": "12:00",
                              "desc": "기준, \"기본\" 설정", "group": "tuning",
                              "vms": [vm("esx01ev01", "job_a", end_ref=True), vm("esx01ev02", "job_a"),
                                      vm("esx01ev03", monitors=["guest_perf"]), vm("esx01ev04", end_ref=True)]})
        b = core._clean_case({"name": "TC-b", "esx": "esx01", "start": "", "interval": "12:00", "desc": "튜닝 적용",
                              "group": "tuning", "end_time": "2026-11-20 21:30:15",
                              "vms": [vm("esx01ev01", "job_b4"), vm("esx01ev02", "job_b4")]})
        c = core._clean_case({"name": "TC-c", "esx": "esx02", "start": "", "interval": "12:00", "vms": [vm("esx02ev01")]})
        cs = [a, b, c]
        core.recalc_starts(cs)
        return cs, core.case_schedule(cs, 5, 10)

    def test_defaults_for_old_data(self):
        c = core._clean_case({"name": "X", "esx": "esx01", "vms": [{"name": "esx01ev01", "job": "job_a", "monitors": []}]})
        self.assertEqual((c["desc"], c["group"], c["end_time"]), ("", "", ""))
        self.assertIs(c["vms"][0]["end_ref"], False)
        n = core.new_case("esx01", "2026-11-20 00:00:00")
        self.assertEqual([v["end_ref"] for v in n["vms"]], [False, False])
        self.assertEqual((n["desc"], n["group"], n["end_time"]), ("", "", ""))

    def test_parse_end(self):
        for t in ("", "  ", "자동", "auto", "AUTO", "-"):
            self.assertIsNone(core.parse_end(t))
        self.assertEqual(core.parse_end("2026/11/20 21:30"), dt.datetime(2026, 11, 20, 21, 30))
        self.assertEqual(core.parse_end("2026-11-20 21:30:15"), dt.datetime(2026, 11, 20, 21, 30, 15), "초까지 쓸 수 있음")
        with self.assertRaises(ValueError):
            core.parse_end("11-20 21:30")

    def test_rows_order_columns(self):
        cs, sched = self.make()
        rows, errors, warnings = core.report_rows(cs, sched, [0, 1], baseline=1)
        self.assertEqual(errors, [])
        self.assertEqual([r[1] for r in rows], ["TC-b", "TC-a"], "비교 기준 케이스를 첫 줄에, 나머지는 표 순서")
        a = dict(zip(core.REPORT_HEADER, rows[1]))
        self.assertEqual(a, {"테스트 그룹": "tuning", "케이스명": "TC-a", "설명": "기준, \"기본\" 설정", "호스트 이름": "esx01",
                             "잡 시작 시각": "2026-11-20 00:00:00", "잡 종료 시각": "",
                             "VM": "esx01ev01;esx01ev02;esx01ev03", "종료 기준 VM": "esx01ev01"})
        b = dict(zip(core.REPORT_HEADER, rows[0]))
        self.assertEqual((b["잡 시작 시각"], b["잡 종료 시각"]), ("2026-11-20 12:00:00", "2026-11-20 21:30:15"),
                         "잡 시작은 계산한 시각, 잡 종료는 입력한 시각")
        self.assertEqual(len(warnings), 1)
        self.assertIn("esx01ev04", warnings[0], "잡과 모니터링이 없는 VM 은 종료 기준에서 빼고 경고")
        rows, errors, warnings = core.report_rows(cs, sched, [0, 1, 2])
        self.assertEqual([r[1] for r in rows], ["TC-a", "TC-b", "TC-c"], "비교 기준을 고르지 않으면 표 순서")
        self.assertEqual(rows[2][6], "")
        self.assertTrue(any("TC-c" in w and "VM 열을 비움" in w for w in warnings))

    def test_rows_errors(self):
        cs, sched = self.make()
        cs[1]["end_time"] = "2026-11-20 11:00:00"
        cs[2]["esx"] = ""
        _, errors, _ = core.report_rows(cs, sched, [1, 2])
        self.assertTrue(any("TC-b" in e and "잡 시작" in e for e in errors), "잡 종료가 잡 시작보다 빠르면 오류")
        self.assertTrue(any("TC-c" in e and "ESX 호스트" in e for e in errors), "호스트 이름이 없으면 오류")
        cs, sched = self.make()
        cs[2]["name"] = "TC-a"
        _, errors, _ = core.report_rows(cs, sched, [0, 2])
        self.assertTrue(any("중복" in e for e in errors), "같은 이름 케이스는 오류")
        cs[0]["start"] = "bad"
        _, errors, _ = core.report_rows(cs, core.case_schedule(cs, 5, 10), [0])
        self.assertTrue(any("잡 시작 시각을 알 수 없음" in e for e in errors))

    def test_write_and_backup(self):
        cs, sched = self.make()
        rows, _, _ = core.report_rows(cs, sched, [0, 1])
        p = os.path.join(self.base, "out", "perfreport_cases_x.csv")
        bk = os.path.join(self.base, "backup")
        self.assertIsNone(core.write_report_csv(p, rows, backup_dir=bk, now=NOW))
        with open(p, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xef\xbb\xbf"), "UTF-8 BOM")
        with open(p, encoding="utf-8-sig", newline="") as f:
            got = list(csv.reader(f))
        self.assertEqual(got[0], core.REPORT_HEADER)
        self.assertEqual(got[1:], rows, "쉼표와 따옴표가 든 설명도 그대로 읽힘")
        keep = core.write_report_csv(p, rows[:1], backup_dir=bk, now=NOW)
        self.assertTrue(os.path.basename(keep).startswith("20261109153012_before_overwrite_"), "덮어쓰기 전 파일을 복사해 둠")
        with open(keep, encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.reader(f))), 3)
        locked = os.path.join(self.base, "out", "locked.csv")
        os.makedirs(locked)                 # 바꿀 수 없는 대상 (엑셀이 연 파일 대신 폴더로 시험)
        with self.assertRaises(OSError):
            core.write_report_csv(locked, rows)
        self.assertFalse(os.path.exists(locked + ".tmp"), "쓰지 못하면 임시 파일을 남기지 않음")

    def test_record_has_report_fields(self):
        cs, _ = self.make()
        core.generate(cs[:2], RUN, MON, self.settings, now=NOW)
        with open(self.settings["record_csv"], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        h = rows[0]
        a, b = rows[1], rows[2]
        self.assertEqual([a[h.index(k)] for k in ("설명", "테스트 그룹", "잡 종료(리포트)", "종료 기준 VM")],
                         ["기준, \"기본\" 설정", "tuning", "", "esx01ev01"])
        self.assertEqual(b[h.index("잡 종료(리포트)")], "2026-11-20 21:30:15")

    def test_blocks_unchanged_by_report_fields(self):
        cs, _ = self.make()
        plain = copy.deepcopy(cs)
        for c in plain:
            c.update(desc="", group="", end_time="")
            for v in c["vms"]:
                v["end_ref"] = False
        r1 = core.generate(cs[:2], RUN, MON, self.settings, now=NOW, write=False)
        r2 = core.generate(plain[:2], RUN, MON, self.settings, now=NOW, write=False)
        self.assertEqual((r1["vm"], r1["esx"]), (r2["vm"], r2["esx"]), "리포트용 칸은 붙여넣기 블록에 영향 없음")


class FileTests(TmpDir):
    def test_settings_roundtrip(self):
        s = core.load_settings(self.base)
        self.assertEqual(s["mon_lead_min"], 5)
        self.assertEqual(s["stop_lead_min"], 10)
        r = core.resolve_paths(self.base, s)
        self.assertEqual(r["log_dir"], os.path.join(self.base, "testcases"))
        core.save_settings(self.base, dict(r, vm_user="tester"))
        with open(os.path.join(self.base, core.SETTINGS_FILE), encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["record_csv"], "")              # 기본 경로는 비워 저장
        self.assertEqual(raw["vm_user"], "tester")
        core.save_settings(self.base, dict(r, log_dir="D:\\logs" if os.name == "nt" else "/tmp/x_logs"))
        self.assertTrue(core.load_settings(self.base)["log_dir"])

    def test_rows_roundtrip_and_backup(self):
        p = os.path.join(self.base, core.RUN_FILE)
        core.save_rows(p, core.RUN_FIELDS, core.RUN_HEADERS, RUN)
        self.assertEqual(core.load_rows(p, core.RUN_FIELDS, core.RUN_HEADERS), RUN)
        core.save_settings(self.base, self.settings)
        dest, copied = core.backup_settings(self.base, now=NOW)
        self.assertEqual(os.path.basename(dest), "20261109153012")
        self.assertEqual(sorted(copied), sorted([core.SETTINGS_FILE, core.RUN_FILE]))
        self.assertTrue(os.path.exists(os.path.join(dest, core.RUN_FILE)))


if __name__ == "__main__":
    unittest.main()
