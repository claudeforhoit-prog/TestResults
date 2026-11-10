# -*- coding: utf-8 -*-
"""
화면 점검 (수동 실행, 화면이 있는 PC 에서): python tests/gui_smoke.py [그림 저장 폴더]

임시 생성기 폴더에서 화면을 띄워 버튼과 선택 창을 코드로 눌러 본다. 대화 상자는 자동으로 '예' 로 답한다.
그림 저장 폴더를 주면 macOS 에서는 탭마다 화면을 저장한다. 자동 시험(unittest discover)에는 포함하지 않는다.
"""
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import tcgen_core as core  # noqa: E402

BASE = tempfile.mkdtemp(prefix="tcgen_gui_")
core.app_dir = lambda: BASE
import tcgen_app as appmod  # noqa: E402

LOG = []
mb = appmod.messagebox
mb.showinfo = lambda *a, **k: LOG.append(("info",) + a[1:2])
mb.showerror = lambda *a, **k: LOG.append(("error",) + a[1:2])
mb.showwarning = lambda *a, **k: LOG.append(("warning",) + a[1:2])
mb.askyesno = lambda *a, **k: (LOG.append(("ask",) + a[1:2]), True)[1]
mb.askyesnocancel = lambda *a, **k: (LOG.append(("ask3",) + a[1:2]), True)[1]
mb.askokcancel = lambda *a, **k: (LOG.append(("okcancel",) + a[1:2]), True)[1]
appmod.open_folder = lambda p: LOG.append(("open", p))
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok  ", msg)


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


def last_toplevel(root):
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    return tops[-1] if tops else None


def press(top, text):
    for w in walk(top):
        if isinstance(w, (ttk.Button, ttk.Checkbutton, ttk.Radiobutton)) and w.cget("text") == text:
            w.invoke()
            return True
    raise AssertionError("button not found: " + text)


def shot(root, app, idx, name):
    app.nb.select(idx)
    root.update()
    root.after(400)
    root.update()
    if SHOTS and sys.platform == "darwin":
        x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
        out = os.path.join(SHOTS, name)
        subprocess.run(["screencapture", "-x", "-R%d,%d,%d,%d" % (x, y - 28, w, h + 28), out], check=False)
        print("shot", out)


def main():
    root = tk.Tk()
    app = appmod.App(root)
    root.geometry("1500x900+20+40")
    root.update()

    # 탭 1: 실행 명령 2개 (편집 칸을 실제로 띄워 입력)
    app.add_cmd_row(app.t_run, "run")
    check(app.t_run.editor is not None, "탭 1 + 행 추가 후 첫 칸 편집 상태")
    app.t_run.editor[1].set("AppA")
    app.t_run.finish_edit()
    for key, val in (("name", "job_a"), ("desc", "1 job"),
                     ("cmd", "/home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1")):
        ci = [c["key"] for c in app.t_run.columns].index(key)
        app.t_run.start_edit(0, ci)
        app.t_run.editor[1].set(val)
        app.t_run.finish_edit()
    app.add_cmd_row(app.t_run, "run")
    for key, val in (("app", "AppB"), ("name", "job_b4"), ("desc", "4 jobs 동시"), ("cmd", "/home/tester/bench/run_b.sh -n 4")):
        app.commit_run(1, key, val)
    check([r["name"] for r in app.run_cmds] == ["job_a", "job_b4"], "탭 1 명령 2개 입력")
    app.commit_run(1, "name", "job_a")
    check(any(x[0] == "error" for x in LOG) and app.run_cmds[1]["name"] == "job_b4", "같은 명령어 명 입력 거부")
    saved_run = core.load_rows(os.path.join(BASE, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)
    check([r["name"] for r in saved_run] == ["job_a", "job_b4"], "탭 1 은 [저장] 없이 run_commands.csv 에 자동 저장")

    # 탭 2: 모니터링 명령 (VM 1개, ESX 1개)
    app.add_cmd_row(app.t_mon, "mon")
    app.t_mon.finish_edit(commit=False)
    for key, val in (("app", "mon"), ("name", "guest_perf"), ("desc", "게스트 성능 수집"),
                     ("start", "/home/tester/mon/start_mon.sh >> /home/tester/log/mon-<vm이름>-<실행시각>.log"),
                     ("stop", "/home/tester/mon/stop_mon.sh")):
        app.commit_mon(0, key, val)
    app.add_cmd_row(app.t_mon, "mon")
    app.t_mon.finish_edit(commit=False)
    for key, val in (("app", "mon"), ("name", "host_pmu"), ("desc", "호스트 카운터"), ("where", "ESX"),
                     ("start", "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh start"),
                     ("stop", "/bin/sh /vmfs/volumes/ds1/mon/host_mon.sh stop")):
        app.commit_mon(1, key, val)
    ci = [c["key"] for c in app.t_mon.columns].index("where")
    app.t_mon.start_edit(0, ci)
    check(isinstance(app.t_mon.editor[0], ttk.Combobox), "실행 위치 칸은 목록 선택")
    app.t_mon.finish_edit(commit=False)
    saved_mon = core.load_rows(os.path.join(BASE, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS)
    check([m["name"] for m in saved_mon] == ["guest_perf", "host_pmu"] and saved_mon[1]["where"] == "ESX",
          "탭 2 는 [저장] 없이 monitor_commands.csv 에 자동 저장")

    # 탭 3: 케이스
    app.add_case()
    check(app.t_case.editor is not None, "첫 케이스 추가 후 이름 칸 편집 상태")
    app.t_case.editor[1].set("TC-base-a")
    app.t_case.finish_edit()
    app.commit_case(0, "esx", "esx01")
    check(app.cases[0]["pvm"] == "esx01ev01" and app.cases[0]["svm"] == "esx01ev02", "ESX 호스트 입력 시 PVM, SVM 자동 입력")
    app.commit_case(0, "esx", "esx02")
    check(app.cases[0]["pvm"] == "esx02ev01", "ESX 호스트를 바꾸면 자동 입력 값도 따라감")
    app.commit_case(0, "pvm", "vm-special")
    app.commit_case(0, "esx", "esx01")
    check(app.cases[0]["pvm"] == "vm-special" and app.cases[0]["svm"] == "esx01ev02", "직접 고친 PVM 은 유지")
    app.commit_case(0, "pvm", "esx01ev01")
    app.commit_case(0, "pvm_job", "job_a")
    app.commit_case(0, "interval", "12")
    app.commit_case(0, "start", "2030-01-15 00:00")
    check(app.cases[0]["start"] == "2030-01-15 00:00:00" and app.cases[0]["interval"] == "12:00", "시각, 간격 형식 정리")
    app.t_case.refresh(0)
    app.edit_monitors(0)
    top = last_toplevel(root)
    cbs = [w for w in walk(top) if isinstance(w, ttk.Checkbutton)]
    check([w.cget("text") for w in cbs] == ["PVM", "SVM", "ESX"], "모니터링 선택 창: VM 은 PVM, SVM / ESX 는 ESX")
    for w in cbs:
        w.invoke()
    press(top, "확인")
    check(app.cases[0]["monitors"] == [{"name": "guest_perf", "targets": ["PVM", "SVM"]},
                                       {"name": "host_pmu", "targets": ["ESX"]}], "모니터링 3개 대상 선택")
    app.t_case.refresh(0)
    app.add_case()
    app.add_case()
    names = [c["name"] for c in app.cases]
    check(names == ["TC-base-a", "TC-base-a-r2", "TC-base-a-r3"], "+ 케이스 추가: 윗줄 복사, 이름 중복 피함 %s" % names)
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-15 12:00:00", "2030-01-16 00:00:00"],
          "잡 시작 = 윗줄 + 간격")
    app.commit_case(1, "name", "TC-base-b4")
    app.commit_case(1, "svm_job", "job_b4")
    app.commit_case(1, "start", "2030-01-15 13:00:00")
    check(app.cases[1]["interval"] == "13:00" and app.cases[2]["start"] == "2030-01-16 01:00:00", "잡 시작을 고치면 간격과 아래 행이 따라감")
    app.commit_case(2, "start", "bad time")
    check(app.cases[2]["start"] == "2030-01-16 01:00:00", "잘못된 시각 입력 거부")
    app.t_case.refresh(2)
    app.edit_stop(2)
    top = last_toplevel(root)
    press(top, "중지 없음 (중지 줄을 만들지 않음)")
    press(top, "확인")
    check(app.cases[2]["stop_mode"] == "none", "마지막 케이스 모니터링 중지: 중지 없음")
    app.t_case.refresh(1)
    vals = app.t_case.tree.item("0", "values")
    check(vals[9] == "01-14 23:55" and vals[10] == "01-15 12:50 (자동)", "모니터링 시작 5분 전, 중지 다음 케이스 10분 전 %s" % (vals[9:],))
    check(app.t_case.tree.item("2", "values")[10] == "중지 없음", "중지 없음 표시")

    # 위로, 아래로, 삭제
    app.t_case.refresh(2)
    app.move_case(-1)
    check([c["name"] for c in app.cases] == ["TC-base-a", "TC-base-a-r3", "TC-base-b4"], "아래 케이스를 위로")
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-15 13:00:00", "2030-01-16 01:00:00"], "시각은 행에 남음")
    app.move_case(1)
    check([c["name"] for c in app.cases] == ["TC-base-a", "TC-base-b4", "TC-base-a-r3"], "다시 아래로")

    # 설정
    n_log = len(LOG)
    app.v_user.set("tester")
    check(core.load_settings(BASE)["vm_user"] == "tester", "설정은 바뀌자마자 settings.json 에 자동 저장")
    app.v_mon.set("")
    check(core.load_settings(BASE)["mon_lead_min"] == 5, "분 칸을 비운 동안에는 저장하지 않음")
    app.v_mon.set("5")
    check(not any(x[0] == "error" for x in LOG[n_log:]), "분 칸 입력 중에는 오류 창이 뜨지 않음")

    # 생성
    shot(root, app, 0, "tcgen_tab1.png")
    shot(root, app, 1, "tcgen_tab2.png")
    shot(root, app, 2, "tcgen_tab3.png")
    app.generate()
    res = app.result
    check(res is not None and not res["errors"], "명령어 생성 성공")
    check(app.blk_vm.get("1.0", "end-1c") == res["vm"], "B 블록 표시")
    check(app.v_esx_host.get() == "esx01" and app.blk_esx.get("1.0", "end-1c") == res["esx"]["esx01"], "C 블록 esx01 표시")
    check("tester@esx01ev01" in res["vm"], "VM 접속 계정 반영")
    app.copy_block("vm")
    check(root.clipboard_get() == res["vm"], "B 블록 복사")
    app.copy_block("esx")
    check(root.clipboard_get() == res["esx"]["esx01"], "C 블록 복사")
    rec = core.resolve_paths(BASE, core.load_settings(BASE))["record_csv"]
    check(os.path.exists(rec) and os.path.exists(res["log"]), "테스트 기록 CSV, 로그 저장")
    shot(root, app, 3, "tcgen_tab4.png")

    # 적용 체크: 적용 칸을 누르면 체크, 다시 누르면 해제. 체크한 케이스는 생성에서 빠지고 test_cases.json 에 저장
    app.nb.select(2)
    root.update()
    tree = app.t_case.tree
    x, y, w, h = tree.bbox("0", "#0")

    def click_check():
        tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
        root.update()

    click_check()
    check(app.cases[0]["applied"] is True, "적용 칸 클릭으로 체크")
    check("applied" in tree.item("0", "tags"), "체크한 행은 회색 표시")
    click_check()
    check(app.cases[0]["applied"] is False, "다시 누르면 체크 해제")
    click_check()
    app.generate()
    res2 = app.result
    made = set(e["case"] for e in res2["entries"]) if res2 else set()
    check(res2 is not None and res2["skipped"] == 1 and "TC-base-a" not in made and made, "체크한 케이스는 생성에서 빠짐 %s" % sorted(made))
    check("적용 체크로 뺀 케이스 1개" in app.res_text.get("1.0", "end"), "생성 결과에 뺀 케이스 수 표시")
    check([c["applied"] for c in core.load_cases(BASE)] == [True, False, False], "체크 상태가 test_cases.json 에 저장됨")

    # 오류가 있으면 만들지 않음
    app.commit_case(1, "pvm_job", "(없음)")
    app.cases[1]["svm_job"] = "nojob"
    app.generate()
    check(app.result is None and app.blk_vm.get("1.0", "end-1c") == "", "오류가 있으면 블록 비움")
    app.cases[1]["svm_job"] = "job_b4"

    # 백업, 폴더 열기
    app.backup()
    bdir = os.path.join(BASE, "backup")
    check(os.path.isdir(bdir) and len(os.listdir(os.path.join(bdir, os.listdir(bdir)[0]))) == 4,
          "설정 백업 4개 파일 (테스트 케이스 표 포함)")
    app.open_settings_dir()
    app.open_case_dir()
    opened = [x[1] for x in LOG if x[0] == "open"]
    check(BASE in opened and os.path.join(BASE, "testcases") in opened, "설정 폴더 열기, 테스트 케이스 폴더 열기")

    # 삭제
    app.t_case.refresh(1)
    app.del_case()
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-16 01:00:00"], "삭제해도 다른 케이스 시각 유지")
    check(app.cases[1]["interval"] == "25:00", "삭제 뒤 아래 행 간격 다시 계산")

    # 저장 버튼 없이 고친 직후 창을 닫아도(끝내기 확인 없이) 다시 열면 남아 있다
    app.commit_run(0, "desc", "1 job edited")
    app.commit_mon(0, "stop", "/home/tester/mon/stop_mon.sh --all")
    app.v_stop.set("12")
    root.destroy()
    root2 = tk.Tk()
    app2 = appmod.App(root2)
    check([r["name"] for r in app2.run_cmds] == ["job_a", "job_b4"] and len(app2.mon_cmds) == 2, "다시 실행 시 명령 목록 불러옴")
    check(app2.v_user.get() == "tester" and app2.v_stop.get() == "12", "다시 실행 시 설정 불러옴")
    check(app2.run_cmds[0]["desc"] == "1 job edited" and app2.mon_cmds[0]["stop"] == "/home/tester/mon/stop_mon.sh --all",
          "닫기 직전에 고친 실행 명령, 모니터링 명령도 남아 있음")
    check([c["name"] for c in app2.cases] == ["TC-base-a", "TC-base-a-r3"] and [c["applied"] for c in app2.cases] == [True, False],
          "다시 실행 시 테스트 케이스 표와 적용 체크 불러옴")
    check("applied" in app2.t_case.tree.item("0", "tags"), "다시 실행 시 체크한 행 회색 표시")
    root2.destroy()
    print("GUI smoke OK  base=%s" % BASE)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
