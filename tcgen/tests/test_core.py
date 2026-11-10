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
