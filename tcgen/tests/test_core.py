# -*- coding: utf-8 -*-
"""tcgen_core 단위 시험. 실행: python -m unittest discover -s tests -v"""
import copy
import csv
import datetime as dt
import io
import json
import os
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
]


def case(name, start, interval="12:00", pvm_job="job_a", svm_job="(없음)", monitors=None, stop_mode="auto", stop_time=""):
    return {"name": name, "esx": "esx01", "pvm": "esx01ev01", "svm": "esx01ev02", "pvm_job": pvm_job, "svm_job": svm_job,
            "interval": interval, "start": start, "stop_mode": stop_mode, "stop_time": stop_time,
            "monitors": copy.deepcopy(monitors) if monitors is not None else
            [{"name": "guest_perf", "targets": ["PVM", "SVM"]}, {"name": "host_mon", "targets": ["ESX"]}]}


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
        for text in [res["vm"], res["setup"]] + list(res["esx"].values()):
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
        c["pvm"] = ""
        e = core.generate([c], RUN, MON, st, now=NOW)["errors"]
        self.assertTrue(any("PVM 이름" in x for x in e))
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
        self.assertEqual(got[0]["monitors"], cs[0]["monitors"])
        with open(os.path.join(self.base, core.CASES_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "cases": [{"name": "X", "applied": 1, "monitors": [{"name": "m", "targets": ["PVM", "BAD"]}],
                                                "stop_mode": "odd"}]}, f)
        got = core.load_cases(self.base)
        self.assertEqual(got[0]["applied"], True)
        self.assertEqual(got[0]["monitors"], [{"name": "m", "targets": ["PVM"]}])
        self.assertEqual(got[0]["stop_mode"], "auto")
        self.assertEqual(got[0]["pvm_job"], "(없음)")
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
        self.assertEqual(rows[1][6], "1 job")
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
        res = core.generate([case("A", "2026-11-20 00:00:00")], RUN, MON, self.settings, now=NOW)
        rows = self.read(p, enc="cp949")
        self.assertEqual(rows[0], ["케이스", "잡 시작(KST)", "비고"])
        self.assertEqual(rows[1], ["OLD", "2026-11-01 00:00:00", "기존 행"])
        self.assertEqual(rows[2], ["A", "2026-11-20 00:00:00", ""])
        self.assertIn("생성 시각", res["record_missing"])

    def test_encrypted_record(self):
        p = self.settings["record_csv"]
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"DRM\x00\x01\x02binary")
        with self.assertRaises(ValueError):
            core.generate([case("A", "2026-11-20 00:00:00")], RUN, MON, self.settings, now=NOW)


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
