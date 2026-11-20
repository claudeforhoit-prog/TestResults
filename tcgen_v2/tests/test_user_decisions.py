# -*- coding: utf-8 -*-
"""사용자 결정 시험. DEVELOPER.md 0절의 결정마다 그 결정이 지켜지는지 본다.

결정된 기능이 빠지거나 동작이 바뀌면 여기서 실패한다(2026-11-20 사용자 지적 "왜 이렇게 마음대로 기능을 뺴는 현상이 생기는거야?"
에 따른 재발 방지). 실패하면 코드를 고치기 전에 사용자에게 묻는다. 사용자가 결정을 바꿨으면 DEVELOPER.md 0절과 이 시험을 함께 고친다.
시험 이름의 dNN 은 0절의 번호다."""
import datetime as dt
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import tcgen_core as core          # noqa: E402
import test_seq as S               # noqa: E402  (같은 입력 도우미: 명령 목록, 케이스, 설정, 생성)

GP = lambda esx="esx01": [S.vm(esx + "ev01", "job_a", ["guest_perf"])]       # 중지 명령이 있는 VM 모니터링


def lines(res, kind):
    return [x for x in res["plan"].split("\n") if x.startswith(kind + " ")]


def ep(*a):
    return int(time.mktime(dt.datetime(*a).timetuple()))


class MonitoringDecisions(unittest.TestCase):
    """2026-11-09 의 모니터링 결정(업무기록 W-20261109-06, -08, -09)."""

    def test_d01_monitoring_starts_five_minutes_before_the_job(self):
        """"모니터링은 잡보다 5분 먼저 시작 해줘" """
        self.assertEqual(core.DEFAULT_SETTINGS["mon_lead_min"], 5)
        res = S.gen([S.case("a1", seq_start="18:00", vms=GP())])
        self.assertIn("SET MON_LEAD_MIN 5", res["plan"])
        self.assertIn("   모니터링 (잡 시작 5분 전에 시작):", res["plan_view"])

    def test_d02_auto_stop_is_ten_minutes_before_the_next_test(self):
        """"다음테스트 시작하기 10분전에 설정한 모든 모니터링 종료하도록 만들어줘": 자동 중지는 다음 케이스 잡 시작 10분 전이고
        그 케이스의 VM 모니터링과 ESX 모니터링에 같이 쓴다. 다음 케이스가 설정 변경 케이스여도 같다."""
        self.assertEqual(core.DEFAULT_SETTINGS["stop_lead_min"], 10)
        cases = [S.case("a1", seq_start="18:00", vms=GP(), esx_monitors=["host_mon"]),
                 S.case("a2", seq_start="+8", esx_profiles=["numa0"])]
        res = S.gen(cases)
        self.assertEqual(res["errors"], [])
        self.assertIn("MSTOP 001 %d 2026-11-17 01:50" % ep(2026, 11, 17, 1, 50), lines(res, "MSTOP"))
        self.assertTrue(any(x.startswith("MX 001 ") for x in res["plan"].split("\n")), "VM 모니터링 중지 줄")
        self.assertTrue(any(x.startswith("EMX 001 ") for x in res["plan"].split("\n")), "ESX 모니터링 중지 줄")
        self.assertIn("SEG 002 1", res["plan"], "다음 케이스는 설정 변경 케이스")

    def test_d03_each_case_chooses_auto_manual_or_none(self):
        """"중지 없음 옵션도있어야됨", "모니터링 중지시점은 중지 하지 않는 옵션도있어야됨" 와 2026-11-09 의 화면 설계(자동, 시각 직접
        입력, 중지 없음)."""
        nxt = S.case("a2", seq_start="+8")
        none = S.gen([S.case("a1", seq_start="18:00", vms=GP(), stop_mode="none"), nxt])
        self.assertEqual((none["errors"], lines(none, "MSTOP")), ([], []), "중지 없음: 중지 줄 없음")
        self.assertIn("   모니터링 중지: 중지 없음(모니터링 중지 칸에서 고름)", none["plan_view"])
        man = S.gen([S.case("a1", seq_start="18:00", vms=GP(), stop_mode="manual", stop_time="2026-11-16 21:30:00"), nxt])
        self.assertEqual(man["errors"], [])
        self.assertEqual(lines(man, "MSTOP"), ["MSTOP 001 %d 2026-11-16 21:30" % ep(2026, 11, 16, 21, 30)], "직접 입력: 적은 시각")
        self.assertIn("   모니터링 중지 (11-16 21:30 직접 입력):", man["plan_view"])
        bad = S.gen([S.case("a1", seq_start="18:00", vms=GP(), stop_mode="manual", stop_time="soon"), nxt])
        self.assertEqual(len(bad["errors"]), 1)
        self.assertIn("모니터링 중지 칸의 시각을 고칠 것", bad["errors"][0])
        early = S.gen([S.case("a1", seq_start="18:00", vms=GP(), stop_mode="manual", stop_time="2026-11-16 17:00:00"), nxt])
        self.assertIn("모니터링 중지 칸의 시각을 잡 시작 11-16 18:00 뒤로 고칠 것", early["errors"][0])

    def test_d03_tab4_has_the_stop_column(self):
        """탭 4 의 순서 실행 화면에 모니터링 중지 칸이 있다(v2.12 ~ v2.14 에서 빠졌던 칸). 화면은 gui_smoke.py 가 본다."""
        with open(os.path.join(os.path.dirname(HERE), "tcgen_app.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('dict(key="mon_stop", title="모니터링 중지"', src)
        seq_hide = src.split("SEQ_HIDE = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn('"mon_stop"', seq_hide, "순서 실행에서 숨기는 열에 모니터링 중지가 없어야 함")
        self.assertIn('text="모니터링 중지"', src.split("self.f_seq = ttk.Frame", 1)[1].split("self.row_seq", 1)[0],
                      "설정 영역(잡 시작 기준 줄)에 모니터링 중지 N분 전 칸")


class RunDecisions(unittest.TestCase):
    """2026-11-16 ~ 11-18 의 진행 결정."""

    def test_d12_job_starts_are_five_minutes_apart(self):
        """"모든 잡들은 분단위로 다른시각에 시작되어야하거든? 겹치면 안됨" (W-20261116-16)"""
        self.assertEqual(core.DEFAULT_SETTINGS["seq_start_spacing_min"], 5)
        res = S.gen([S.case("a1", seq_start="18:00"), S.case("b1", esx="esx02", seq_start="18:03")])
        self.assertTrue(res["errors"] and "5분 이상 떨어져 있지 않음" in res["errors"][0], res["errors"])

    def test_d22_start_time_is_used_as_written(self):
        """"시작시간이정의가되어있으면 잡 등록은 시작시간으로 해야해 임의로 바꾸면 안됨" (W-20261118-08 (3))"""
        res = S.gen([S.case("a1", seq_start="2026-11-16 18:07:00")])
        self.assertEqual(lines(res, "START"), ["START 001 %d 2026-11-16 18:07" % ep(2026, 11, 16, 18, 7)])

    def test_d23_plus_interval_counts_from_the_row_above(self):
        """"그냥 헤깔리니까 바로 윗줄로할래" (W-20261118-08 (4)): ESX 호스트가 달라도 바로 윗줄 기준"""
        res = S.gen([S.case("a1", seq_start="18:00"), S.case("b1", esx="esx02", seq_start="+1:30"), S.case("a2", seq_start="+2")])
        self.assertEqual([x.split(" ", 3)[3] for x in lines(res, "START")], ["2026-11-16 18:00", "2026-11-16 19:30", "2026-11-16 21:30"])

    def test_d25_settings_change_is_thirty_minutes_before(self):
        """"60분전에 설정 적용이 아니고 30분으로 바꿔줘" (W-20261118-08 (6))"""
        self.assertEqual(core.DEFAULT_SETTINGS["seq_prep_lead_min"], 30)

    def test_d26_config_snapshot_is_ten_minutes_before(self):
        """"config 스냅샷은 잡 시작전 10분에 해줘 모든 케이스에 대해" (W-20261118-08 (7))"""
        self.assertEqual(core.DEFAULT_SETTINGS["seq_snap_lead_min"], 10)

    def test_d34_s_block_lists_the_cases_for_the_check(self):
        """"생성기 점프 VM 에 복사 붙여넣기 하면 테스트케이스 이름, 호스트 혹은 VM 명, 시작 시간, 실행 명령어 모니터링 목록
        리스트업되고 확인한 다음 실행되도록하고싶어 확인작업필수" (2026-11-20): plan 의 LIST 줄. 묻고 답을 받는 것은 test_tcrun 의
        ConfirmTests 와 test_seq_blocks_shell 이 본다."""
        res = S.gen([S.case("a1", seq_start="18:00", vms=GP(), esx_monitors=["host_mon"]),
                     S.case("b1", esx="", seq_start="20:00", vms=[S.vm("bm01", "job_a")])])
        got = [x[5:] for x in lines(res, "LIST")]
        for want in ("case 1/2  a1   job start 2026-11-16 18:00", "  vm esx01ev01",
                     "    job: /home/tester/bench/run_a.sh >> /home/tester/log/run_a-${TC_HOST}-${TC_START}.log 2>&1",
                     "    monitoring guest_perf: /home/tester/mon/start_mon.sh >> /home/tester/log/mon-${TC_HOST}-${TC_START}.log",
                     "  ESX host esx01", "---- no ESX host: 1 case", "case 2/2  b1   job start 2026-11-16 20:00", "  vm bm01"):
            self.assertIn(want, got)
        with open(os.path.join(os.path.dirname(HERE), "jump", "tcrun.sh"), encoding="ascii") as f:
            sh = f.read()
        self.assertIn("confirm_start", sh.split("cmd_start() {", 1)[1].split("\n}\n", 1)[0], "tcrun start 가 묻는다")

    def test_d35_vm_names_are_green(self):
        """"출력물에서 VM 명은 그린색이나 노란색으로 표기되도록" (2026-11-20): tcrun 의 vm_color. 색은 test_tcrun 이 본다."""
        with open(os.path.join(os.path.dirname(HERE), "jump", "tcrun.sh"), encoding="ascii") as f:
            sh = f.read()
        self.assertIn("[1;32m", sh)



class StopDecisions(unittest.TestCase):
    """2026-11-20 의 stop 결정(업무기록 W-20261120-12)."""

    def test_d36_stop_does_the_work_of_cancel_and_removes_the_run(self):
        """"Stop으로 멈추고 cancle작업까지 둘다 허고 cancle 없애줘", "삭제하면 trunk status에 없어져야지": tcrun stop 이 진행을
        끝내고 줄을 지우고 모니터링을 중지한 뒤 실행 기록을 지운다. tcrun cancel 은 없다. 동작은 test_tcrun 의 StopTests 가 본다."""
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "jump", "tcrun.sh"), encoding="ascii") as f:
            sh = f.read()
        stop = sh.split("cmd_stop() {", 1)[1].split("\n}\n", 1)[0]
        for name in ("stop_chain", "forget_chain", "forget_run"):
            self.assertIn(name, stop, "tcrun stop 이 %s 를 부른다" % name)
        self.assertIn("close_case", sh.split("stop_chain() {", 1)[1].split("\n}\n", 1)[0], "줄 삭제와 모니터링 중지")
        self.assertNotIn("cmd_cancel", sh)
        self.assertIn('cancel) say "tcrun $TCRUN_VERSION has no cancel', sh)
        self.assertNotIn("cancel [esx]", sh)
        res = S.gen([S.case("a1", seq_start="18:00", vms=GP())])
        self.assertNotIn("cancel", res["plan_view"])
        self.assertIn("중지 sh ~/.tcgen/tcrun stop (진행을 끝내고 등록한 crontab 줄을 지우고 시작된 모니터링을 중지함)", res["plan_view"])
        with open(os.path.join(root, "tcgen_app.py"), encoding="utf-8") as f:
            self.assertNotIn("tcrun cancel", f.read())


if __name__ == "__main__":
    unittest.main()
