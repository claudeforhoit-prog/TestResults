# -*- coding: utf-8 -*-
"""
화면 점검 (수동 실행, 화면이 있는 PC 에서): python tests/gui_smoke.py [그림 저장 폴더]

임시 생성기 폴더에서 화면을 띄워 버튼과 선택 창을 코드로 눌러 본다. 대화 상자는 자동으로 '예' 로 답한다.
그림 저장 폴더를 주면 macOS 에서는 탭마다 화면을 저장한다. 자동 시험(unittest discover)에는 포함하지 않는다.
"""
import csv
import datetime as dt
import json
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

TMP = tempfile.mkdtemp(prefix="tcgen_gui_")
BASE = os.path.join(TMP, "data")            # 데이터 폴더 (기본 위치 대신 임시 폴더)
PROG = os.path.join(TMP, "prog")            # 생성기 폴더
os.makedirs(PROG)
os.environ["TCGEN_CONFIG_DIR"] = os.path.join(TMP, "cfg")
os.environ["TCGEN_DEFAULT_DATA_DIR"] = BASE
core.app_dir = lambda: PROG
import tcgen_app as appmod  # noqa: E402

ASK_DIR = []                                # 폴더 선택 창 대답 (차례대로)
appmod.filedialog.askdirectory = lambda **k: ASK_DIR.pop(0) if ASK_DIR else ""
ASK_SAVE = []                               # 저장 위치 창 대답 (차례대로)
appmod.filedialog.asksaveasfilename = lambda **k: ASK_SAVE.pop(0) if ASK_SAVE else ""

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


def cell_of(table, iid, key):
    """표 칸에 보이는 글자 (열 이름으로 찾음)."""
    return table.tree.item(iid, "values")[[c["key"] for c in table.columns].index(key)]


def col_of(table, key):
    return [c["key"] for c in table.columns].index(key)


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
    app.t_mon.tree.selection_set("1")          # 마지막 행을 골라 그 아래(맨 끝)에 추가
    app.add_cmd_row(app.t_mon, "mon")
    app.t_mon.finish_edit(commit=False)
    k = app.t_mon.selected_index()
    check(k == 2, "선택한 행 아래에 새 행 추가")
    for key, val in (("app", "mon"), ("name", "guest_gpu"), ("desc", "GPU 지표 수집"),
                     ("start", "/home/tester/mon/gpu_mon.sh >> /home/tester/log/gpu-<vm이름>-<실행시각>.log"),
                     ("stop", "/home/tester/mon/gpu_mon.sh --stop")):
        app.commit_mon(k, key, val)
    saved_mon = core.load_rows(os.path.join(BASE, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS)
    check([m["name"] for m in saved_mon] == ["guest_perf", "host_pmu", "guest_gpu"] and saved_mon[1]["where"] == "ESX",
          "탭 2 는 [저장] 없이 monitor_commands.csv 에 자동 저장")

    # 탭 3: 케이스 행 아래 VM 행
    t = app.t_case
    tree = t.tree
    app.add_case()
    check(t.editor is not None and t.editor[2] == "c0", "첫 케이스 추가 후 케이스 이름 칸 편집 상태")
    t.editor[1].set("TC-base-a")
    t.finish_edit()
    c0 = app.cases[0]
    check([v["name"] for v in c0["vms"]] == ["", ""], "새 케이스는 VM 2대 (ESX 호스트 입력 전 이름 빈칸)")
    app.commit_case("c0", "esx", "esx01")
    check([v["name"] for v in c0["vms"]] == ["esx01ev01", "esx01ev02"], "ESX 호스트 입력 시 VM 이름 자동 입력 (ev01, ev02)")
    app.commit_case("c0", "esx", "esx02")
    check([v["name"] for v in c0["vms"]] == ["esx02ev01", "esx02ev02"], "ESX 호스트를 바꾸면 자동 이름도 따라감")
    app.commit_case("c0v0", "name", "vm-special")
    app.commit_case("c0", "esx", "esx01")
    check([v["name"] for v in c0["vms"]] == ["vm-special", "esx01ev02"], "직접 고친 VM 이름은 유지")
    app.commit_case("c0v0", "name", "esx01ev01")
    ci_job = [c["key"] for c in t.columns].index("job")
    t.start_edit("c0v0", ci_job)
    check(isinstance(t.editor[0], ttk.Combobox), "VM 행 잡 칸은 목록 선택")
    t.finish_edit(commit=False)
    t.start_edit("c0", ci_job)
    check(t.editor is None, "케이스 행의 잡 칸은 고칠 수 없음")
    app.commit_case("c0v0", "job", "job_a")
    app.commit_case("c0v1", "job", "job_a")
    app.commit_case("c0", "interval", "12")
    app.commit_case("c0", "start", "2030-01-15 00:00")
    check(c0["start"] == "2030-01-15 00:00:00" and c0["interval"] == "12:00", "시각, 간격 형식 정리")
    t.refresh("c0")
    app.add_vm()
    check([v["name"] for v in c0["vms"]] == ["esx01ev01", "esx01ev02", "esx01ev03"] and t.selected() == "c0v2",
          "+ VM 추가: 선택한 케이스에 다음 번호 ev03")
    app.commit_case("c0v2", "job", "job_b4")
    app.edit_monitors("c0v0")
    top = last_toplevel(root)
    texts = [w.cget("text") for w in walk(top) if isinstance(w, ttk.Checkbutton)]
    check(texts == ["mon / guest_perf", "mon / guest_gpu"], "VM 모니터링 선택 창: 실행 위치 VM 명령만 %s" % texts)
    press(top, "mon / guest_perf")
    press(top, "확인")
    app.edit_monitors("c0v2")
    top = last_toplevel(root)
    press(top, "mon / guest_perf")
    press(top, "mon / guest_gpu")
    press(top, "확인")
    check([v["monitors"] for v in c0["vms"]] == [["guest_perf"], [], ["guest_perf", "guest_gpu"]], "VM 마다 모니터링 다르게 선택")
    app.edit_monitors("c0")
    top = last_toplevel(root)
    texts = [w.cget("text") for w in walk(top) if isinstance(w, ttk.Checkbutton)]
    check(texts == ["mon / host_pmu"], "ESX 모니터링 선택 창: 실행 위치 ESX 명령만 %s" % texts)
    press(top, "mon / host_pmu")
    press(top, "확인")
    check(c0["esx_monitors"] == ["host_pmu"], "ESX 모니터링은 케이스 행에서 선택")
    t.refresh("c0")
    check(cell_of(t, "c0", "mon") == "host_pmu (ESX)", "케이스 행 모니터링 칸에 ESX 모니터링 표시")
    v2 = [cell_of(t, "c0v2", k) for k in ("name", "job", "mon")]
    check(v2[0].strip() == "└ esx01ev03" and v2[1:] == ["job_b4", "guest_perf, guest_gpu"], "VM 행 표시 %s" % (v2,))
    check("case" in tree.item("c0", "tags") and "vm" in tree.item("c0v1", "tags"), "케이스 행과 VM 행 구분")

    # 리포트용 칸: 설명, 테스트 그룹, 잡 종료 (리포트), 종료 기준 VM
    t.start_edit("c0", col_of(t, "desc"))
    check(t.editor is not None and t.editor[3] == "desc", "케이스 행 설명 칸 편집")
    t.editor[1].set("기준, 기본 설정  (THP\tnever)")
    t.finish_edit()
    check(c0["desc"] == "기준, 기본 설정 (THP never)" and cell_of(t, "c0", "desc") == c0["desc"], "설명 입력과 표시 (탭, 겹친 빈칸 정리)")
    t.start_edit("c0v0", col_of(t, "desc"))
    check(t.editor is None, "VM 행 설명 칸은 고칠 수 없음")
    app.commit_case("c0", "group", "tuning")
    check(cell_of(t, "c0", "end_time") == "자동", "잡 종료를 비우면 자동 표시")
    n_log = len(LOG)
    app.commit_case("c0", "end_time", "2030-01-14 23:00")
    check(c0["end_time"] == "" and any(x[0] == "error" and "잡 시작" in x[1] for x in LOG[n_log:]), "잡 시작보다 빠른 잡 종료 거부")
    app.commit_case("c0", "end_time", "2030-01-15 11:40:30")
    check(c0["end_time"] == "2030-01-15 11:40:30" and cell_of(t, "c0", "end_time") == "2030-01-15 11:40:30", "잡 종료 입력 (초까지)")
    app.commit_case("c0", "end_time", "자동")
    check(c0["end_time"] == "", "자동 입력 시 비움")
    app.commit_case("c0", "end_time", "2030/01/15 11:40")
    t.start_edit("c0v2", col_of(t, "end_ref"))
    check(c0["vms"][2]["end_ref"] is True and cell_of(t, "c0v2", "end_ref") == "기준", "VM 행 종료 기준 VM 칸을 두 번 누르면 지정")
    t.start_edit("c0v2", col_of(t, "end_ref"))
    check(c0["vms"][2]["end_ref"] is False and cell_of(t, "c0v2", "end_ref") == "", "다시 누르면 해제")
    t.start_edit("c0v2", col_of(t, "end_ref"))
    t.start_edit("c0", col_of(t, "end_ref"))
    check(t.editor is None and cell_of(t, "c0", "end_ref") == "", "케이스 행 종료 기준 VM 칸은 고칠 수 없음")
    saved = core.load_cases(BASE)[0]
    check(saved["desc"] == c0["desc"] and saved["group"] == "tuning" and saved["end_time"] == "2030-01-15 11:40:00"
          and saved["vms"][2]["end_ref"] is True, "리포트용 칸도 test_cases.json 에 자동 저장")
    t.refresh("c0")
    app.add_case()
    app.add_case()
    check(all(c["desc"] == c0["desc"] and c["group"] == "tuning" and c["vms"][2]["end_ref"] for c in app.cases[1:]),
          "+ 케이스 추가: 설명, 테스트 그룹, 종료 기준 VM 복사")
    check([c["end_time"] for c in app.cases] == ["2030-01-15 11:40:00", "", ""], "+ 케이스 추가: 잡 종료는 복사하지 않음 (자동)")
    names = [c["name"] for c in app.cases]
    check(names == ["TC-base-a", "TC-base-a-r2", "TC-base-a-r3"], "+ 케이스 추가: 선택한 케이스 복사, 이름 중복 피함 %s" % names)
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-15 12:00:00", "2030-01-16 00:00:00"],
          "잡 시작 = 윗줄 + 간격")
    check(all([v["job"] for v in c["vms"]] == ["job_a", "job_a", "job_b4"] for c in app.cases), "복사한 케이스에 VM 구성도 복사")
    app.commit_case("c1", "name", "TC-base-b4")
    app.commit_case("c1", "start", "2030-01-15 13:00:00")
    check(app.cases[1]["interval"] == "13:00" and app.cases[2]["start"] == "2030-01-16 01:00:00", "잡 시작을 고치면 간격과 아래 행이 따라감")
    app.commit_case("c2", "start", "bad time")
    check(app.cases[2]["start"] == "2030-01-16 01:00:00", "잘못된 시각 입력 거부")
    t.refresh("c1v1")
    app.add_vm()
    check([v["name"] for v in app.cases[1]["vms"]][-1] == "esx01ev04" and t.selected() == "c1v3", "VM 행을 골라도 그 케이스에 추가 (ev04)")
    app.del_row()
    check(len(app.cases[1]["vms"]) == 3 and len(app.cases) == 3, "VM 행 삭제는 그 VM 만")
    t.refresh("c0v2")
    app.move_row(-1)
    check([v["name"] for v in c0["vms"]] == ["esx01ev01", "esx01ev03", "esx01ev02"] and t.selected() == "c0v1", "VM 행 위로")
    app.move_row(1)
    check([v["name"] for v in c0["vms"]] == ["esx01ev01", "esx01ev02", "esx01ev03"], "VM 행 아래로")
    t.refresh("c2")
    app.edit_stop("c2")
    top = last_toplevel(root)
    press(top, "중지 없음 (중지 줄을 만들지 않음)")
    press(top, "확인")
    check(app.cases[2]["stop_mode"] == "none", "마지막 케이스 모니터링 중지: 중지 없음")
    t.refresh("c1")
    vals = [cell_of(t, "c0", "mon_start"), cell_of(t, "c0", "mon_stop")]
    check(vals == ["01-14 23:55", "01-15 12:50 (자동)"], "모니터링 시작 5분 전, 중지 다음 케이스 10분 전 %s" % (vals,))
    check(cell_of(t, "c2", "mon_stop") == "중지 없음", "중지 없음 표시")

    # 위로, 아래로 (케이스 행)
    t.refresh("c2")
    app.move_row(-1)
    check([c["name"] for c in app.cases] == ["TC-base-a", "TC-base-a-r3", "TC-base-b4"], "아래 케이스를 위로")
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-15 13:00:00", "2030-01-16 01:00:00"], "시각은 행에 남음")
    app.move_row(1)
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
    check(sorted(set(e["host"] for e in res["entries"])) == ["esx01", "esx01ev01", "esx01ev02", "esx01ev03"],
          "VM 3대와 ESX 모두 등록 대상")
    check(core.hd_unescape(res["vm"]).count("<< TCEOF") == 3, "B 블록에 VM 3대")
    app.copy_block("vm")
    check(root.clipboard_get() == res["vm"], "B 블록 복사")
    app.copy_block("esx")
    check(root.clipboard_get() == res["esx"]["esx01"], "C 블록 복사")
    check(app.blk_wipe.get("1.0", "end-1c") == res["vm_wipe"], "D 블록 (VM crontab 전체 삭제) 표시")
    b_hosts = [ln.split()[2].split("@")[-1] for ln in core.hd_unescape(res["vm"]).splitlines() if ln.startswith("sh ~/.tcgen/vmssh ")]
    d_hosts = [ln.split()[2].split("@")[-1] for ln in core.hd_unescape(res["vm_wipe"]).splitlines() if ln.startswith("sh ~/.tcgen/vmssh ")]
    check(d_hosts == b_hosts == res["wipe_hosts"] and sorted(d_hosts) == ["esx01ev01", "esx01ev02", "esx01ev03"],
          "D 블록은 B 블록과 같은 VM 3대, 같은 순서 %s" % d_hosts)
    n_log = len(LOG)
    app.copy_block("wipe")
    check(root.clipboard_get() == res["vm_wipe"] and any(x[0] == "ask" and "모두 지워진다" in x[1] for x in LOG[n_log:]),
          "D 블록은 확인 창에서 예를 누른 뒤 복사")
    app.copy_block("vm")
    mb.askyesno = lambda *a, **k: (LOG.append(("ask",) + a[1:2]), False)[1]
    app.copy_block("wipe")
    mb.askyesno = lambda *a, **k: (LOG.append(("ask",) + a[1:2]), True)[1]
    check(root.clipboard_get() == res["vm"], "확인 창에서 아니오를 누르면 D 블록을 복사하지 않음")
    rec = core.resolve_paths(BASE, core.load_settings(BASE))["record_csv"]
    check(os.path.exists(rec) and os.path.exists(res["log"]), "테스트 기록 CSV, 로그 저장")
    with open(rec, encoding="utf-8-sig") as f:
        rec_text = f.read()
    check("esx01ev01=job_a, esx01ev02=job_a, esx01ev03=job_b4" in rec_text, "테스트 기록 CSV 의 VM 구성 열")
    shot(root, app, 3, "tcgen_tab4.png")

    # 적용 체크: 적용 칸을 누르면 체크, 다시 누르면 해제. 체크한 케이스는 생성에서 빠지고 test_cases.json 에 저장
    app.nb.select(2)
    root.update()
    tree = app.t_case.tree
    x, y, w, h = tree.bbox("c0", "#0")

    def click_check():
        tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
        root.update()

    click_check()
    check(app.cases[0]["applied"] is True, "적용 칸 클릭으로 체크")
    check("applied" in tree.item("c0", "tags") and "applied" in tree.item("c0v2", "tags"), "체크한 케이스와 그 VM 행은 회색 표시")
    click_check()
    check(app.cases[0]["applied"] is False, "다시 누르면 체크 해제")
    click_check()
    app.generate()
    res2 = app.result
    made = set(e["case"] for e in res2["entries"]) if res2 else set()
    check(res2 is not None and res2["skipped"] == 1 and "TC-base-a" not in made and made, "체크한 케이스는 생성에서 빠짐 %s" % sorted(made))
    check("적용 체크로 뺀 케이스 1개" in app.res_text.get("1.0", "end"), "생성 결과에 뺀 케이스 수 표시")
    check(res2["wipe_conflicts"] == ["TC-base-a"] and "주의: 적용 체크한 케이스 TC-base-a" in app.res_text.get("1.0", "end"),
          "적용 체크한 케이스(실행 전)가 같은 VM 을 쓰면 D 블록 주의 표시")
    n_log = len(LOG)
    app.copy_block("wipe")
    check(any(x[0] == "ask" and "TC-base-a 의 줄도 지워진다" in x[1] for x in LOG[n_log:]), "D 블록 확인 창에 그 케이스 이름")
    check([c["applied"] for c in core.load_cases(BASE)] == [True, False, False], "체크 상태가 test_cases.json 에 저장됨")

    # 리포트용 CSV 저장: 케이스 고르기, 비교 기준(첫 줄), 저장 위치
    app.nb.select(2)
    root.update()
    app.export_report()
    top = last_toplevel(root)
    rt = app.report_table
    check([r["name"] for r in rt.rows] == ["TC-base-a", "TC-base-b4", "TC-base-a-r3"] and not any(r["sel"] for r in rt.rows),
          "저장 창: 케이스 전부 표시, 처음에는 고른 케이스 없음")
    check([r["applied"] for r in rt.rows] == ["예", "", ""], "저장 창에 적용 체크 표시")
    n_log = len(LOG)
    press(top, "저장")
    check(any(x[0] == "info" and "하나 이상" in x[1] for x in LOG[n_log:]) and top.winfo_exists(), "고르지 않고 저장하면 안내만")
    root.update()
    bx, by, bw, bh = rt.tree.bbox("2", "#0")
    rt.tree.event_generate("<Button-1>", x=bx + bw // 2, y=by + bh // 2)
    root.update()
    rt.check["toggle"]("0")
    check([r["sel"] for r in rt.rows] == [True, False, True], "저장 칸을 눌러 케이스 고르기")
    check("pick" in rt.tree.item("0", "tags") and "pick" not in rt.tree.item("1", "tags"), "고른 행 바탕색")
    cb = [w for w in walk(top) if isinstance(w, ttk.Combobox)][0]
    check(list(cb.cget("values")) == ["TC-base-a", "TC-base-a-r3"] and cb.get() == "TC-base-a",
          "비교 기준 후보는 고른 케이스, 기본은 표 순서 첫 케이스")
    cb.set("TC-base-a-r3")
    cb.event_generate("<<ComboboxSelected>>")
    rt.check["toggle"]("1")
    check(cb.get() == "TC-base-a-r3", "직접 고른 비교 기준은 다른 케이스를 골라도 유지")
    rt.check["toggle"]("1")
    REP = os.path.join(TMP, "reports", "cases_for_report.csv")
    ASK_SAVE.append(REP)
    press(top, "저장")
    check(os.path.exists(REP) and not top.winfo_exists(), "저장하고 창 닫힘")
    with open(REP, encoding="utf-8-sig", newline="") as f:
        rrows = list(csv.reader(f))
    check(rrows[0] == core.REPORT_HEADER, "리포트 생성기 케이스 정보 파일의 열 %s" % rrows[0])
    check([r[1] for r in rrows[1:]] == ["TC-base-a-r3", "TC-base-a"], "비교 기준 케이스를 첫 줄에")
    ra = dict(zip(core.REPORT_HEADER, rrows[2]))
    check(ra["설명"] == "기준, 기본 설정 (THP never)" and ra["테스트 그룹"] == "tuning" and ra["호스트 이름"] == "esx01"
          and ra["잡 시작 시각"] == "2030-01-15 00:00:00" and ra["잡 종료 시각"] == "2030-01-15 11:40:00", "케이스 칸 %s" % ra)
    check(ra["VM"] == "esx01ev01;esx01ev02;esx01ev03" and ra["종료 기준 VM"] == "esx01ev03", "VM, 종료 기준 VM 칸")
    check(rrows[1][5] == "", "잡 종료를 비운 케이스는 빈칸 (리포트 생성기가 자동으로 정함)")
    check(core.load_config().get("report_dir") == os.path.dirname(REP), "저장 위치를 기억함")
    check("리포트용 CSV 저장" in app.v_status.get(), "상태 줄에 저장 결과")
    ASK_SAVE.append(REP)
    app.export_report()
    top = last_toplevel(root)
    press(top, "모두 선택")
    press(top, "저장")
    with open(REP, encoding="utf-8-sig", newline="") as f:
        check(len(list(csv.reader(f))) == 4, "모두 선택: 케이스 3개 저장")
    check(any(d.endswith("_before_overwrite_cases_for_report.csv") for d in os.listdir(os.path.join(BASE, "backup"))),
          "같은 이름 파일은 덮어쓰기 전에 backup 에 복사")
    ASK_SAVE.append("")
    app.export_report()
    top = last_toplevel(root)
    press(top, "모두 선택")
    press(top, "저장")
    check(top.winfo_exists(), "저장 위치 창에서 취소하면 저장 창이 남음")
    press(top, "취소")
    check(not top.winfo_exists(), "취소로 닫힘")

    # 오류가 있으면 만들지 않음
    app.cases[1]["vms"][2]["job"] = "nojob"
    app.generate()
    check(app.result is None and app.blk_vm.get("1.0", "end-1c") == "", "오류가 있으면 블록 비움")
    app.cases[1]["vms"][2]["job"] = "job_b4"

    # 백업(위치 선택), 백업 불러오기, 폴더 열기
    BK = os.path.join(TMP, "my_backups")
    ASK_DIR.append(BK)
    app.backup()
    bks = sorted(d for d in os.listdir(BK) if d.startswith("tcgen_backup_")) if os.path.isdir(BK) else []
    check(len(bks) == 1 and all(os.path.exists(os.path.join(BK, bks[0], f)) for f in core.DATA_FILES),
          "설정 백업: 고른 폴더에 tcgen_backup_ 폴더와 데이터 파일 4개")
    check(os.path.exists(os.path.join(BK, bks[0], "testcases", "test_record.csv")), "백업에 테스트 기록 포함")
    check(core.load_config().get("backup_dir") == os.path.normpath(BK), "백업 위치를 기억함")
    before = app.run_cmds[0]["desc"]
    app.commit_run(0, "desc", "changed after backup")
    ASK_DIR.append(os.path.join(BK, bks[0]))
    app.restore_backup()
    check(app.run_cmds[0]["desc"] == before and app.t_run.tree.item("0", "values")[2] == before,
          "백업 불러오기: 데이터와 화면이 백업 내용으로 바뀜")
    check(any(d.endswith("_before_restore") for d in os.listdir(os.path.join(BASE, "backup"))), "불러오기 전 데이터를 따로 복사해 둠")
    app.open_data_dir()
    app.open_case_dir()
    opened = [x[1] for x in LOG if x[0] == "open"]
    check(BASE in opened and os.path.join(BASE, "testcases") in opened, "데이터 폴더 열기, 테스트 케이스 폴더 열기")

    # 데이터 폴더 변경: 지금 데이터를 새 폴더로 복사하고 위치를 config.json 에 기록
    NEW = os.path.join(TMP, "data_moved")
    ASK_DIR.append(NEW)
    app.change_data_dir()
    check(app.base == os.path.normpath(NEW) and core.has_data(NEW), "데이터 폴더 변경: 새 폴더로 복사하고 새 폴더를 씀")
    check(core.data_dir() == os.path.normpath(NEW), "데이터 폴더 위치가 config.json 에 기록됨")
    n_log = len(LOG)
    ASK_DIR.append(os.path.join(PROG, "sub"))
    app.change_data_dir()
    check(app.base == os.path.normpath(NEW) and any(x[0] == "error" and "생성기 폴더 안" in x[1] for x in LOG[n_log:]),
          "생성기 폴더 안은 데이터 폴더로 쓸 수 없음")

    # 삭제
    app.t_case.refresh("c1")
    app.del_row()
    check([c["start"] for c in app.cases] == ["2030-01-15 00:00:00", "2030-01-16 01:00:00"], "삭제해도 다른 케이스 시각 유지")
    check(app.cases[1]["interval"] == "25:00", "삭제 뒤 아래 행 간격 다시 계산")

    # 저장 버튼 없이 고친 직후 창을 닫아도(끝내기 확인 없이) 다시 열면 남아 있다
    app.commit_run(0, "desc", "1 job edited")
    app.commit_mon(0, "stop", "/home/tester/mon/stop_mon.sh --all")
    app.v_stop.set("12")
    root.destroy()
    root2 = tk.Tk()
    app2 = appmod.App(root2)
    check(app2.base == os.path.normpath(NEW), "다시 실행 시 바꾼 데이터 폴더를 씀")
    check([r["name"] for r in app2.run_cmds] == ["job_a", "job_b4"] and len(app2.mon_cmds) == 3, "다시 실행 시 명령 목록 불러옴")
    check(app2.v_user.get() == "tester" and app2.v_stop.get() == "12", "다시 실행 시 설정 불러옴")
    check(app2.run_cmds[0]["desc"] == "1 job edited" and app2.mon_cmds[0]["stop"] == "/home/tester/mon/stop_mon.sh --all",
          "닫기 직전에 고친 실행 명령, 모니터링 명령도 남아 있음")
    check([c["name"] for c in app2.cases] == ["TC-base-a", "TC-base-a-r3"] and [c["applied"] for c in app2.cases] == [True, False],
          "다시 실행 시 테스트 케이스 표와 적용 체크 불러옴")
    check("applied" in app2.t_case.tree.item("c0", "tags"), "다시 실행 시 체크한 행 회색 표시")
    check([len(c["vms"]) for c in app2.cases] == [3, 3] and app2.cases[0]["vms"][2]["monitors"] == ["guest_perf", "guest_gpu"],
          "다시 실행 시 VM 구성과 VM 별 모니터링 불러옴")
    root2.destroy()

    # 예전 형식 케이스 표(PVM, SVM 칸): 원본을 backup 에 복사하고 VM 행 형식으로 바꿔 보여 준다
    old_dir = os.path.join(TMP, "data_oldfmt")
    os.makedirs(old_dir)
    with open(os.path.join(old_dir, core.CASES_FILE), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "cases": [{"name": "OLD", "esx": "esx09", "pvm": "esx09ev01", "svm": "esx09ev02",
                                            "pvm_job": "job_a", "svm_job": "(없음)", "interval": "12:00",
                                            "start": "2030-02-01 00:00:00",
                                            "monitors": [{"name": "guest_perf", "targets": ["PVM", "SVM"]}]}]}, f)
    core.save_config({"data_dir": old_dir})
    n_log = len(LOG)
    root4 = tk.Tk()
    app4 = appmod.App(root4)
    check(app4.cases[0]["vms"] == [{"name": "esx09ev01", "job": "job_a", "monitors": ["guest_perf"], "end_ref": False},
                                   {"name": "esx09ev02", "job": "(없음)", "monitors": ["guest_perf"], "end_ref": False}],
          "예전 PVM, SVM 케이스를 VM 2대로 변환")
    check(any(x[0] == "info" and "VM 행 형식으로 바꿨다" in x[1] for x in LOG[n_log:]), "변환했다는 안내 표시")
    check(any(d.endswith("_before_vm_format") for d in os.listdir(os.path.join(old_dir, "backup"))), "변환 전 원본을 backup 에 복사")
    check(app4.t_case.tree.exists("c0v1"), "변환한 케이스가 VM 행으로 보임")
    root4.destroy()

    # 예전 판 데이터 옮기기: 생성기 폴더에 예전 파일이 있고 데이터 폴더가 비어 있으면 처음 실행 때 복사해 쓴다
    prog2 = os.path.join(TMP, "prog_old")
    os.makedirs(prog2)
    core.save_rows(os.path.join(prog2, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS,
                   [{"app": "X", "name": "old_job", "desc": "", "cmd": "/x/old.sh"}])
    core.save_config({})
    fresh = os.path.join(TMP, "data_fresh")
    os.environ["TCGEN_DEFAULT_DATA_DIR"] = fresh
    core.app_dir = lambda: prog2
    n_log = len(LOG)
    root3 = tk.Tk()
    app3 = appmod.App(root3)
    check(app3.base == os.path.normpath(fresh) and [r["name"] for r in app3.run_cmds] == ["old_job"],
          "예전 생성기 폴더의 데이터를 데이터 폴더로 복사해 씀")
    check(any(x[0] == "info" and "데이터 폴더로 복사" in x[1] for x in LOG[n_log:]), "복사했다는 안내 표시")
    check(not core.has_data(prog2), "생성기 폴더의 예전 파일은 옆 폴더로 옮김")
    root3.destroy()
    print("GUI smoke OK  tmp=%s" % TMP)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
