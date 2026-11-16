# -*- coding: utf-8 -*-
"""
화면 점검 (수동 실행, 화면이 있는 PC 에서): python tests/gui_smoke.py [그림 저장 폴더]

임시 생성기 폴더에서 화면을 띄워 버튼과 선택 창을 코드로 눌러 본다. 대화 상자는 자동으로 '예' 로 답한다.
그림 저장 폴더를 주면 macOS 에서는 탭과 창마다 화면을 PNG 로 저장한다(창 내용을 같은 프로세스에서 그려 저장하므로
화면 기록 권한이 필요 없다. 글자가 잘리는지는 이 그림을 열어 확인한다). 자동 시험(unittest discover)에는 포함하지 않는다.

순서: 시각 지정 방식(v1.4 와 같은 화면)의 흐름을 먼저 보고, 순서 실행 방식(탭 3 설정 프로파일, 탭 4 의 설정과 최대 대기,
탭 5 의 블록 A, P, S, D 와 실행 계획)을 본 뒤, 데이터 폴더와 다시 실행, v1 데이터 가져오기를 본다.
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
os.environ["TCGEN_V1_CONFIG_DIR"] = os.path.join(TMP, "v1cfg")      # v1 의 설정 폴더와 데이터 폴더도 임시 폴더로 (실제 폴더를 읽지 않게)
os.environ["TCGEN_V1_DATA_DIR"] = os.path.join(TMP, "v1data")
core.app_dir = lambda: PROG
import tcgen_app as appmod  # noqa: E402

ASK_DIR = []                                # 폴더 선택 창 대답 (차례대로)
appmod.filedialog.askdirectory = lambda **k: ASK_DIR.pop(0) if ASK_DIR else ""
ASK_SAVE = []                               # 저장 위치 창 대답 (차례대로)
appmod.filedialog.asksaveasfilename = lambda **k: ASK_SAVE.pop(0) if ASK_SAVE else ""
ASK_OPEN = []                               # 파일 열기 창 대답 (차례대로)
appmod.filedialog.askopenfilename = lambda **k: ASK_OPEN.pop(0) if ASK_OPEN else ""

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


def snap_window(title_part, path):
    """macOS: 제목에 title_part 가 있는 창의 내용을 PNG 로 저장한다(NSView cacheDisplayInRect. 화면 기록 권한 불필요)."""
    import ctypes
    import ctypes.util
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    class CGRect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]

    def send(obj, name, *args, **kw):
        restype = kw.get("restype", ctypes.c_void_p)
        argtypes = kw.get("argtypes")
        argtypes = argtypes if argtypes is not None else [ctypes.c_void_p] * len(args)
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        return proto(("objc_msgSend", objc))(obj, objc.sel_registerName(name.encode()), *args)

    def cls(name):
        return objc.objc_getClass(name.encode())

    wins = send(send(cls("NSApplication"), "sharedApplication"), "windows")
    for i in range(send(wins, "count", restype=ctypes.c_ulong, argtypes=[])):
        w = send(wins, "objectAtIndex:", i, argtypes=[ctypes.c_ulong])
        t = send(w, "title", argtypes=[])
        ptr = send(t, "UTF8String", argtypes=[]) if t else None
        title = ctypes.string_at(ptr).decode("utf-8") if ptr else ""
        if title_part not in title or not send(w, "isVisible", restype=ctypes.c_bool, argtypes=[]):
            continue
        view = send(w, "contentView", argtypes=[])
        b = send(view, "bounds", restype=CGRect, argtypes=[])
        bitmap = send(view, "bitmapImageRepForCachingDisplayInRect:", b, argtypes=[CGRect])
        send(view, "cacheDisplayInRect:toBitmapImageRep:", b, bitmap, restype=None, argtypes=[CGRect, ctypes.c_void_p])
        data = send(bitmap, "representationUsingType:properties:", 4, send(cls("NSDictionary"), "dictionary", argtypes=[]),
                    argtypes=[ctypes.c_ulong, ctypes.c_void_p])
        text = send(cls("NSString"), "stringWithUTF8String:", path.encode("utf-8"), argtypes=[ctypes.c_char_p])
        return bool(send(data, "writeToFile:atomically:", text, True, restype=ctypes.c_bool,
                         argtypes=[ctypes.c_void_p, ctypes.c_bool]))
    return False


def shot_window(root, title_part, name):
    """그림 저장 폴더를 줬을 때만: 제목에 title_part 가 있는 창을 저장한다."""
    root.update()
    root.after(300)
    root.update()
    if not (SHOTS and sys.platform == "darwin"):
        return
    out = os.path.join(SHOTS, name)
    try:
        done = snap_window(title_part, out)
    except Exception as e:          # 그림 저장은 점검의 보조 기능이다. 안 되면 화면 캡처 명령으로 저장해 본다
        print("snap failed:", e)
        done = False
    if not done:
        x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
        subprocess.run(["screencapture", "-x", "-R%d,%d,%d,%d" % (x, y - 28, w, h + 28), out], check=False)
    print("shot", out)


def shot(root, app, idx, name):
    app.nb.select(idx)
    shot_window(root, appmod.TITLE, name)


def main():
    root = tk.Tk()
    app = appmod.App(root)
    root.geometry("%sx%s+20+40" % (os.environ.get("TCGEN_W", "1500"), os.environ.get("TCGEN_H", "900")))
    root.update()
    check(appmod.TITLE == "테스트 실행 명령 생성기 v%s" % core.VERSION and root.title() == appmod.TITLE, "창 제목에 판 번호")
    check([app.nb.tab(i, "text") for i in range(5)] == ["1. 실행 명령", "2. 모니터링 명령", "3. 설정 프로파일", "4. 테스트 케이스",
                                                        "5. 생성 결과"], "탭 5개와 순서")
    check(app.mode() == "seq" and app.v_mode.get() == "순서 실행", "진행 방식의 기본값은 순서 실행")
    check(app.base == os.path.normpath(BASE) and not os.path.exists(os.environ["TCGEN_V1_DATA_DIR"]), "v1 데이터가 없으면 빈 화면으로 시작")

    # ---- 시각 지정 방식: v1.4 와 같은 화면과 동작 (아래 흐름은 v1.4 의 점검을 그대로 옮긴 것)
    app.v_mode.set("시각 지정")
    app.mode_changed()
    check(core.load_settings(BASE)["run_mode"] == "time", "진행 방식을 바꾸면 settings.json 에 저장")
    vis = list(app.t_case.tree.cget("displaycolumns"))
    check("prof" not in vis and "max_wait" not in vis and "seq_start" not in vis
          and all(k in vis for k in ("interval", "start", "mon_start", "mon_stop", "end_time")),
          "시각 지정: 탭 4 에서 설정, 최대 대기, 시작 시각(순서 실행) 열을 숨기고 시각 열을 보임 %s" % vis)
    check(app.blk_vm.frame.cget("text").startswith("B. VM 등록 블록") and app.blk_esx.frame.cget("text").startswith("C. ESX 블록"),
          "시각 지정: 탭 5 의 블록은 A, B, C, D")
    check(app.f_time.winfo_manager() == "pack" and app.f_seq.winfo_manager() == "" and app.row_seq.winfo_manager() == ""
          and app.lf_plan.winfo_manager() == "" and app.esx_pick.winfo_manager() == "pack",
          "시각 지정: 순서 실행용 설정 칸과 실행 계획 칸은 보이지 않음")

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
    shot(root, app, 3, "tcgen_time_tab4.png")
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
    shot(root, app, 4, "tcgen_time_tab5.png")

    # 적용 체크: 적용 칸을 누르면 체크, 다시 누르면 해제. 체크한 케이스는 생성에서 빠지고 test_cases.json 에 저장
    app.nb.select(3)
    root.update()
    tree = app.t_case.tree
    tree.see("c0")
    root.update()
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

    # 리포트용 CSV 저장: 케이스 고르기, 저장 위치 (비교 기준 칸은 없다. 표 순서대로 저장한다)
    app.nb.select(3)
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
    check(not [w for w in walk(top) if isinstance(w, ttk.Combobox)]
          and not [w for w in walk(top) if isinstance(w, ttk.Label) and "비교 기준 (첫 줄)" in str(w.cget("text"))],
          "저장 창에 비교 기준을 고르는 칸이 없음")
    shot_window(root, "리포트용 CSV 저장", "tcgen_time_report_dialog.png")
    check(any(isinstance(w, ttk.Label) and "고른 케이스 2개" in str(w.cget("text")) or
              (isinstance(w, ttk.Label) and str(w.cget("textvariable")) and "고른 케이스 2개" in top.getvar(str(w.cget("textvariable"))))
              for w in walk(top)), "고른 케이스 수 표시")
    REP = os.path.join(TMP, "reports", "cases_for_report.csv")
    ASK_SAVE.append(REP)
    press(top, "저장")
    check(os.path.exists(REP) and not top.winfo_exists(), "저장하고 창 닫힘")
    with open(REP, encoding="utf-8-sig", newline="") as f:
        rrows = list(csv.reader(f))
    check(rrows[0] == core.REPORT_HEADER, "리포트 생성기 케이스 정보 파일의 열 %s" % rrows[0])
    check([r[1] for r in rrows[1:]] == ["TC-base-a", "TC-base-a-r3"], "고른 케이스를 탭 4 의 표 순서대로 저장")
    ra = dict(zip(core.REPORT_HEADER, rrows[1]))
    check(ra["설명"] == "기준, 기본 설정 (THP never)" and ra["테스트 그룹"] == "tuning" and ra["호스트 이름"] == "esx01"
          and ra["잡 시작 시각"] == "2030-01-15 00:00:00" and ra["잡 종료 시각"] == "2030-01-15 11:40:00", "케이스 칸 %s" % ra)
    check(ra["VM"] == "esx01ev01;esx01ev02;esx01ev03" and ra["종료 기준 VM"] == "esx01ev03", "VM, 종료 기준 VM 칸")
    check(rrows[2][5] == "", "잡 종료를 비운 케이스는 빈칸 (리포트 생성기가 자동으로 정함)")
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

    # ---- 순서 실행 방식 (v2)
    n_log = len(LOG)
    app.v_mode.set("순서 실행")
    app.mode_changed()
    root.update()
    check(core.load_settings(BASE)["run_mode"] == "seq" and app.result is None, "순서 실행으로 바꾸면 저장하고 앞 결과를 비움")
    vis = list(app.t_case.tree.cget("displaycolumns"))
    check(vis == ["name", "desc", "esx", "job", "mon", "prof", "max_wait", "seq_start", "group", "end_ref"],
          "순서 실행: 탭 4 에서 간격, 잡 시작, 모니터링 시작, 모니터링 중지, 잡 종료 (리포트) 열을 숨김 %s" % vis)
    check(app.blk_vm.frame.cget("text").startswith("P. 점검 블록") and app.blk_esx.frame.cget("text").startswith("S. 실행 블록"),
          "순서 실행: 탭 5 의 블록은 A, P, S, D")
    check(app.f_seq.winfo_manager() == "pack" and app.f_time.winfo_manager() == "" and app.row_seq.winfo_manager() == "pack"
          and app.lf_plan.winfo_manager() == "grid" and app.esx_pick.winfo_manager() == "",
          "순서 실행: 점프 VM 로그 폴더, ESX 접속 주소, VM 다시 켠 뒤 대기 칸과 실행 계획 칸이 보임")
    check("순서 실행" in app.case_hint.cget("text") and "P(점검)" in app.result_hint.cget("text"), "탭 4, 탭 5 의 안내 글이 진행 방식에 맞게 바뀜")
    app.show_help("4. 테스트 케이스", app.case_help)
    helps = [w.cget("text") for w in walk(last_toplevel(root)) if isinstance(w, ttk.Label)]
    check(any("표의 순서대로 진행한다" in x for x in helps) and any("최대 대기" in x for x in helps)
          and any("시작 시각: 비우면 '자동'" in x and "케이스 사이 간격" in x for x in helps), "탭 4 도움말이 순서 실행 내용으로 바뀜(시작 시각 설명 포함)")
    shot_window(root, "도움말", "tcgen_seq_help4.png")
    last_toplevel(root).destroy()

    # 설정 영역: 점프 VM 로그 폴더, ESX 접속 주소, VM 다시 켠 뒤 잡 시작까지 최소 분
    app.v_jlog.set("/data/tcgen_log")
    app.v_addr.set("esx01=10.0.0.11")
    app.v_settle.set("15")
    st = core.load_settings(BASE)
    check((st["jump_log_dir"], st["esx_addr"], st["seq_settle_min"]) == ("/data/tcgen_log", "esx01=10.0.0.11", 15),
          "순서 실행 설정도 바뀌자마자 settings.json 에 저장")
    app.v_settle.set("")
    check(core.load_settings(BASE)["seq_settle_min"] == 15, "분 칸을 비운 동안에는 저장하지 않음")
    app.v_settle.set("10")

    # 탭 1: 프로세스 이름
    app.nb.select(0)
    root.update()
    ci = col_of(app.t_run, "proc")
    app.t_run.start_edit(0, ci)
    check(app.t_run.editor is not None and app.t_run.editor[3] == "proc", "탭 1 프로세스 이름 칸 편집")
    app.t_run.editor[1].set("run_a.sh")
    app.t_run.finish_edit()
    app.commit_run(1, "proc", "run_b.sh -n 4")
    n_log = len(LOG)
    app.commit_run(1, "proc", "it's bad!")
    check(app.run_cmds[1]["proc"] == "run_b.sh -n 4" and any(x[0] == "error" and "작은따옴표" in x[1] for x in LOG[n_log:]),
          "프로세스 이름에 쓸 수 없는 글자는 거부")
    saved_run = core.load_rows(os.path.join(BASE, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)
    check([r["proc"] for r in saved_run] == ["run_a.sh", "run_b.sh -n 4"], "프로세스 이름도 run_commands.csv 에 자동 저장")
    shot(root, app, 0, "tcgen_seq_tab1.png")

    # 탭 3: 처음 실행하면 기본 프로파일 12개가 들어 있다. [기본 프로파일 추가] 는 표에 없는 이름만 다시 추가한다.
    app.nb.select(2)
    root.update()
    tp = app.t_prof
    defaults = [q["name"] for q in core.default_profiles()]
    check(len(defaults) == 12 and [q["name"] for q in app.profiles] == defaults, "처음 실행: 탭 3 에 기본 프로파일 12개")
    check([q["name"] for q in core.load_profiles(BASE)] == defaults, "기본 프로파일을 profiles.json 에 저장")
    check(cell_of(tp, "0", "name") == "lpage_1g_on" and cell_of(tp, "10", "target") == "ESX 호스트"
          and "sched.mem.pin = TRUE" in cell_of(tp, "0", "text"), "기본 프로파일의 이름, 대상, 설정 내용이 표에 보임")
    shot(root, app, 2, "tcgen_seq_tab3_defaults.png")
    n_log = len(LOG)
    press(root, "기본 프로파일 추가")
    check(len(app.profiles) == 12 and any(x[0] == "info" and "이미 표에 모두 있음" in x[1] for x in LOG[n_log:]),
          "기본 프로파일이 모두 있으면 [기본 프로파일 추가] 는 아무것도 추가하지 않음")
    app.profiles[3]["text"] = "monitor_control.halt_in_monitor = FALSE\n"        # 사용자가 고친 내용
    tp.refresh(6)
    app.del_prof_row()
    check(len(app.profiles) == 11 and "vpmc_on" not in [q["name"] for q in app.profiles], "기본 프로파일 한 행 삭제")
    n_log = len(LOG)
    press(root, "기본 프로파일 추가")
    check(len(app.profiles) == 12 and app.profiles[-1]["name"] == "vpmc_on"
          and app.profiles[3]["text"] == "monitor_control.halt_in_monitor = FALSE\n"
          and any(x[0] == "info" and "1개를 표 끝에 추가" in x[1] for x in LOG[n_log:]),
          "[기본 프로파일 추가] 는 없는 이름만 표 끝에 추가하고 고친 행은 그대로 둠")
    check([q["name"] for q in core.load_profiles(BASE)][-1] == "vpmc_on", "추가한 기본 프로파일 자동 저장")
    while app.profiles:                      # 아래 흐름은 빈 표에서 시작한다
        tp.refresh(0)
        app.del_prof_row()
    check(app.profiles == [] and core.load_profiles(BASE) == [], "기본 프로파일을 모두 삭제하면 빈 표로 저장")

    # 탭 3: 설정 프로파일 (ESX 호스트 1개, VM 2개)
    app.add_prof_row()
    check(tp.editor is not None and tp.editor[3] == "name", "탭 3 + 행 추가 후 이름 칸 편집 상태")
    tp.editor[1].set("numa0")
    tp.finish_edit()
    check(app.profiles[0] == {"name": "numa0", "target": "VM", "text": "", "desc": ""}, "새 프로파일의 대상 기본값은 VM")
    tp.start_edit(0, col_of(tp, "target"))
    check(isinstance(tp.editor[0], ttk.Combobox) and list(tp.editor[0].cget("values")) == ["ESX 호스트", "VM"], "대상 칸은 목록 선택")
    tp.editor[1].set("ESX 호스트")
    tp.finish_edit()
    check(app.profiles[0]["target"] == "ESX" and cell_of(tp, "0", "target") == "ESX 호스트", "대상을 ESX 호스트로")
    tp.start_edit(0, col_of(tp, "text"))
    top = app.prof_win
    check(top.winfo_exists() and "대상 ESX 호스트" in top.title(), "설정 내용 칸을 두 번 누르면 여러 줄 입력 창이 열림")
    app.prof_text.insert("1.0", "LocalityWeightActionAffinity = 0")
    n_log = len(LOG)
    app.prof_ok()
    check(top.winfo_exists() and any(x[0] == "error" and "/구역/옵션" in x[1] for x in LOG[n_log:]), "형식이 틀리면 알리고 창을 닫지 않음")
    app.prof_text.delete("1.0", "end")
    app.prof_text.insert("1.0", "# NUMA 설정\nNuma.LocalityWeightActionAffinity = 0\n\n")
    shot_window(root, "설정 내용", "tcgen_seq_profile_dialog.png")
    app.prof_ok()
    check(not top.winfo_exists() and app.profiles[0]["text"] == "# NUMA 설정\nNuma.LocalityWeightActionAffinity = 0\n",
          "설정 내용 저장 (끝의 빈 줄 정리)")
    check(cell_of(tp, "0", "text") == "Numa.LocalityWeightActionAffinity = 0", "표에는 설정 내용을 한 줄로 보임 (설명 줄 제외)")
    app.commit_prof(0, "desc", "NUMA  action affinity 끔")
    app.add_prof_row()
    tp.finish_edit(commit=False)
    app.commit_prof(1, "name", "pin")
    tp.start_edit(1, col_of(tp, "text"))
    app.prof_text.insert("1.0", 'sched.mem.pin = "TRUE"\nnuma.nodeAffinity =\n')        # vmx 줄을 그대로 붙여넣은 경우와 키 삭제
    app.prof_ok()
    check(core.parse_profile_text(app.profiles[1]["text"], "VM")[0] == [("sched.mem.pin", "TRUE"), ("numa.nodeAffinity", "")],
          "vmx 줄(키 = \"값\")은 따옴표를 벗겨 받고, 값을 비우면 그 키 삭제")
    app.add_prof_row()
    tp.finish_edit(commit=False)
    app.commit_prof(2, "name", "aff")
    tp.start_edit(2, col_of(tp, "text"))
    app.prof_text.insert("1.0", "sched.cpu.affinity = 0,1,2,3\nsched.mem.pin = FALSE")
    app.prof_ok()
    n_log = len(LOG)
    app.commit_prof(2, "name", "pin")
    app.commit_prof(2, "name", "한글 이름")
    check(app.profiles[2]["name"] == "aff" and len([x for x in LOG[n_log:] if x[0] == "error"]) == 2, "같은 이름, 한글 이름은 거부")
    tp.start_edit(1, col_of(tp, "text"))
    app.prof_text.insert("end", "\ndisplayName = other")
    n_log = len(LOG)
    app.prof_ok()
    check(app.prof_win.winfo_exists() and any(x[0] == "error" and "바꿀 수 없음" in x[1] for x in LOG[n_log:]),
          "VM 을 구별하는 키(displayName)는 넣을 수 없음")
    app.prof_win.destroy()
    saved_prof = core.load_profiles(BASE)
    check([(q["name"], q["target"]) for q in saved_prof] == [("numa0", "ESX"), ("pin", "VM"), ("aff", "VM")]
          and saved_prof[0]["desc"] == "NUMA action affinity 끔", "탭 3 은 [저장] 없이 profiles.json 에 자동 저장")
    shot(root, app, 2, "tcgen_seq_tab3.png")

    # 탭 4: 설정 칸과 최대 대기 칸 (숨긴 열이 있어도 누른 칸이 맞게 열리는지 실제로 두 번 눌러 본다)
    app.nb.select(3)
    root.update()
    t = app.t_case
    tree = t.tree
    t.refresh("c1")
    root.update()

    def double(iid, key):
        """그 칸의 가운데를 두 번 누른 것과 같은 처리를 부른다(Tk 9 는 두 번 누르기 이벤트를 만들어 보낼 수 없다).
        표는 누른 좌표로 행과 화면의 열 순번을 찾으므로, 숨긴 열이 있을 때 열을 맞게 찾는지가 여기서 확인된다."""
        tree.see(iid)
        root.update()
        x, y, w, h = tree.bbox(iid, key)

        class Click(object):
            pass
        ev = Click()
        ev.x, ev.y = x + w // 2, y + h // 2
        t._double(ev)
        root.update()

    double("c1", "max_wait")
    check(t.editor is not None and t.editor[2:] == ("c1", "max_wait"), "숨긴 열이 있어도 두 번 누른 칸(최대 대기)이 편집 상태가 됨")
    t.editor[1].set("12")
    t.finish_edit()
    check(app.cases[1]["max_wait"] == "12:00" and cell_of(t, "c1", "max_wait") == "12:00", "최대 대기 형식 정리 (12 -> 12:00)")
    n_log = len(LOG)
    app.commit_case("c1", "max_wait", "soon")
    check(app.cases[1]["max_wait"] == "12:00" and any(x[0] == "error" and "최대 대기" in x[1] for x in LOG[n_log:]), "잘못된 최대 대기 거부")
    check(cell_of(t, "c2", "max_wait") == "제한 없음" and cell_of(t, "c1v0", "max_wait") == "", "최대 대기를 비우면 제한 없음, VM 행은 빈칸")
    double("c1", "prof")
    top = last_toplevel(root)
    texts = [w.cget("text") for w in walk(top) if isinstance(w, ttk.Checkbutton)]
    check("ESX 호스트 설정 선택" in top.title() and texts == ["numa0"], "케이스 행 설정 칸: 대상이 ESX 호스트인 프로파일만 %s" % texts)
    press(top, "numa0")
    press(top, "확인")
    double("c1v0", "prof")
    top = last_toplevel(root)
    texts = [w.cget("text") for w in walk(top) if isinstance(w, ttk.Checkbutton)]
    check("VM 설정 선택" in top.title() and texts == ["pin", "aff"], "VM 행 설정 칸: 대상이 VM 인 프로파일만 %s" % texts)
    press(top, "aff")
    press(top, "pin")
    shot_window(root, "VM 설정 선택", "tcgen_seq_profile_pick.png")
    press(top, "확인")
    c1 = app.cases[1]
    check(c1["esx_profiles"] == ["numa0"] and c1["vms"][0]["profiles"] == ["pin", "aff"], "고른 프로파일은 탭 3 의 순서로 저장")
    check(cell_of(t, "c1", "prof") == "numa0 (ESX)" and cell_of(t, "c1v0", "prof") == "pin, aff" and cell_of(t, "c1v1", "prof") == "(없음)",
          "설정 칸 표시")
    saved = core.load_cases(BASE)[1]
    check(saved["esx_profiles"] == ["numa0"] and saved["max_wait"] == "12:00" and saved["vms"][0]["profiles"] == ["pin", "aff"],
          "설정과 최대 대기도 test_cases.json 에 자동 저장")
    t.refresh("c1")
    times = [(c["start"], c["interval"]) for c in app.cases]          # + 케이스 추가는 아래 행의 잡 시작을 다시 계산한다(시각 지정용 값)
    app.add_case()
    check(app.cases[2]["name"] == "TC-base-b4-r2" and app.cases[2]["esx_profiles"] == ["numa0"]
          and app.cases[2]["vms"][0]["profiles"] == ["pin", "aff"] and app.cases[2]["max_wait"] == "12:00",
          "+ 케이스 추가: 설정과 최대 대기도 복사")
    app.move_row(-1)
    check([c["name"] for c in app.cases[:3]] == ["TC-base-a", "TC-base-b4-r2", "TC-base-b4"]
          and app.cases[2]["start"] == "2030-01-15 13:00:00", "순서 실행에서 위로: 케이스의 값이 모두 케이스를 따라감")
    app.move_row(1)
    app.del_row()
    check([c["name"] for c in app.cases] == ["TC-base-a", "TC-base-b4", "TC-base-a-r3"], "추가한 케이스 삭제")
    for c, (st0, iv0) in zip(app.cases, times):                       # 뒤의 시각 지정 점검을 위해 시각 값을 되돌린다
        c["start"], c["interval"] = st0, iv0
    app.mark_cases()
    t.refresh("c1")
    # 시작 시각 칸(비우면 자동)과 케이스 사이 간격
    check(cell_of(t, "c1", "seq_start") == "자동" and app.cases[1]["seq_start"] == "", "시작 시각 칸은 처음에 자동")
    app.commit_case("c1", "seq_start", "9:05")
    t.refresh("c1")
    check(app.cases[1]["seq_start"] == "09:05" and cell_of(t, "c1", "seq_start") == "09:05", "시작 시각에 시:분을 적음")
    n_log = len(LOG)
    app.commit_case("c1", "seq_start", "soon")
    check(app.cases[1]["seq_start"] == "09:05" and any(x[0] == "error" and "시작 시각" in x[1] for x in LOG[n_log:]), "형식이 맞지 않는 시작 시각은 거부")
    app.commit_case("c2", "seq_start", "2031/01/02 03:04")
    check(app.cases[2]["seq_start"] == "2031-01-02 03:04:00", "날짜까지 적은 시작 시각")
    app.commit_case("c2", "seq_start", "자동")
    check(app.cases[2]["seq_start"] == "", "'자동' 을 적으면 비움")
    check(core.load_cases(BASE)[1]["seq_start"] == "09:05", "시작 시각도 자동 저장")
    check(app.v_gap.get() == "30" and app.row_seq2.winfo_manager() == "pack", "설정 영역의 케이스 사이 간격 기본값은 30분")
    app.v_gap.set("45")
    check(core.load_settings(BASE)["seq_gap_min"] == 45, "케이스 사이 간격을 바꾸면 저장")
    app.v_gap.set("30")
    check(app.v_poll.get() == "30" and core.load_settings(BASE)["seq_poll_sec"] == 1800, "잡 종료 확인 간격의 기본값은 30분(1800초)")
    app.v_poll.set("60")
    check(core.load_settings(BASE)["seq_poll_sec"] == 3600, "잡 종료 확인 간격을 분으로 바꾸면 초로 저장")
    n_log = len(LOG)
    app.v_poll.set("0")
    check(app.current_settings() is None and any(x[0] == "error" and "잡 종료 확인 간격" in x[1] for x in LOG[n_log:])
          and core.load_settings(BASE)["seq_poll_sec"] == 3600, "잡 종료 확인 간격은 1분 이상")
    app.v_poll.set("30")
    check(core.load_settings(BASE)["seq_poll_sec"] == 1800, "30분으로 되돌림")
    t.refresh("c1")
    shot(root, app, 3, "tcgen_seq_tab4.png")

    # 생성: 블록 A, P, S, D 와 실행 계획
    app.generate()
    res = app.result
    check(res is not None and res["mode"] == "seq" and not res["errors"], "순서 실행으로 명령어 생성 성공")
    check(app.nb.index(app.nb.select()) == 4, "생성하면 탭 5 로 이동")
    check(app.blk_setup.get("1.0", "end-1c") == res["setup"] and "tcgen_apply.py" in res["setup"], "A 블록: tcrun 과 설정 적용 스크립트 포함")
    check(app.blk_vm.get("1.0", "end-1c") == res["check"] and app.blk_esx.get("1.0", "end-1c") == res["run"], "P 블록, S 블록 표시")
    check(app.blk_wipe.get("1.0", "end-1c") == res["vm_wipe"], "D 블록 표시")
    check(app.plan_text.get("1.0", "end-1c") == res["plan_view"] and "잡 종료 확인" in res["plan_view"], "실행 계획 글 표시")
    plan = res["plan"]
    check("CASE 001 esx01 TC-base-b4\n" in plan and "CASE 002 esx01 TC-base-a-r3\n" in plan and "TC-base-a\n" not in plan,
          "plan 에 적용 체크하지 않은 케이스 2개가 표의 순서로")
    check("AH 001 /Numa/LocalityWeightActionAffinity=0\n" in plan and "AV 001 esx01ev01 sched.mem.pin=FALSE\n" in plan
          and "AV 001 esx01ev01 numa.nodeAffinity=\n" in plan and "AV 001 esx01ev01 sched.cpu.affinity=0,1,2,3\n" in plan,
          "설정이 plan 에 들어감 (같은 키는 뒤 프로파일의 값)")
    check("LOGDIR /data/tcgen_log\n" in plan and "ADDR esx01 10.0.0.11\n" in plan and "SET SETTLE_MIN 10\n" in plan
          and "VM 001 esx01ev01 tester@esx01ev01\n" in plan and "PROC 001 esx01ev01 run_a\\.sh\n" in plan,
          "로그 폴더, ESX 접속 주소, VM 다시 켠 뒤 대기, 접속 계정, 프로세스 이름이 plan 에 들어감")
    check("MAXWAIT 001 720\n" in plan and "MAXWAIT 002 0\n" in plan, "최대 대기가 plan 에 들어감")
    import re as _re
    check("SET POLL_SEC 1800\n" in plan and "SET FIRST_CHECK_SEC 60\n" in plan and "그 뒤 30분마다" in res["plan_view"],
          "잡 종료 확인 간격(30분)과 첫 확인(60초)이 plan 과 실행 계획에 들어감")
    check("SET GAP_MIN 30\n" in plan and _re.search(r"(?m)^START 001 \d+ \d{4}-\d\d-\d\d 09:05$", plan) and "START 002 " not in plan,
          "케이스 사이 간격과 지정한 시작 시각이 plan 에 들어감. 시각을 비운 케이스에는 START 줄이 없음")
    check("시작 시각(지정): 잡을" in res["plan_view"] and "09:05 에 시작한다" in res["plan_view"]
          and "앞 케이스의 잡이 끝난 시각에서 30분 이상 지난 뒤" in res["plan_view"], "실행 계획에 시작 시각과 간격 설명")
    app.commit_case("c1", "seq_start", "")
    text5 = app.res_text.get("1.0", "end")
    check("순서 실행. 실행 ID %s" % res["run_id"] in text5 and "VM 을 자동으로 종료하고 다시 켠다" in text5, "결과 글에 실행 ID 와 VM 자동 종료 안내")
    app.copy_block("setup")
    check(root.clipboard_get() == res["setup"], "A 블록 복사")
    app.copy_block("vm")
    check(root.clipboard_get() == res["check"], "P 블록 복사")
    n_log = len(LOG)
    app.copy_block("esx")
    check(root.clipboard_get() == res["run"] and any(x[0] == "ask" and "진행이 바로 시작된다" in x[1] and "TC-base-b4" in x[1]
                                                     for x in LOG[n_log:]), "S 블록은 확인 창에서 예를 누른 뒤 복사")
    mb.askyesno = lambda *a, **k: (LOG.append(("ask",) + a[1:2]), False)[1]
    app.copy_block("vm")
    app.copy_block("esx")
    app.copy_block("wipe")
    mb.askyesno = lambda *a, **k: (LOG.append(("ask",) + a[1:2]), True)[1]
    check(root.clipboard_get() == res["check"], "확인 창에서 아니오를 누르면 S 블록, D 블록을 복사하지 않음")
    with open(rec, encoding="utf-8-sig", newline="") as f:
        rrows = list(csv.reader(f))
    last = dict(zip(rrows[0], rrows[-2]))
    check(rrows[0][-3:] == ["진행 방식", "설정", "실행 ID"] and last["진행 방식"] == "순서 실행" and last["실행 ID"] == res["run_id"]
          and last["설정"] == "esx01:numa0, esx01ev01:pin+aff" and last["잡 시작(KST)"] == "",
          "테스트 기록 CSV 에 진행 방식, 설정, 실행 ID 열 추가 (시각 칸은 비움)")
    shot(root, app, 4, "tcgen_seq_tab5.png")
    app.show_help("5. 생성 결과", app.RESULT_HELP_SEQ)
    shot_window(root, "도움말", "tcgen_seq_help5.png")
    last_toplevel(root).destroy()
    n_log = len(LOG)
    app.export_report()
    check(any(x[0] == "info" and "perfreport_cases.csv" in x[1] and "/data/tcgen_log/tcrun-" in x[1] for x in LOG[n_log:])
          and getattr(last_toplevel(root), "winfo_exists", lambda: False)() in (False, 0),
          "순서 실행의 [리포트용 CSV 저장] 은 점프 VM 로그 폴더의 파일을 안내만 함")
    app.run_cmds[0]["proc"] = ""
    n_log = len(LOG)
    app.generate()
    check(app.result is None and any(x[0] == "error" and "프로세스 이름이 비어 있음" in x[1] for x in LOG[n_log:])
          and app.blk_esx.get("1.0", "end-1c") == "" and app.plan_text.get("1.0", "end-1c") == "",
          "잡의 프로세스 이름이 없으면 오류, 블록과 실행 계획을 비움")
    app.run_cmds[0]["proc"] = "run_a.sh"
    app.cases[1]["vms"][0]["job"] = "(없음)"
    app.cases[1]["vms"][1]["job"] = "(없음)"
    app.cases[1]["vms"][2]["job"] = "(없음)"
    n_log = len(LOG)
    app.generate()
    check(app.result is None and any(x[0] == "error" and "모니터링만 있고 잡이 없음" in x[1] for x in LOG[n_log:]),
          "모니터링만 있고 잡이 없는 케이스는 오류")
    for k, jn in enumerate(("job_a", "job_a", "job_b4")):
        app.cases[1]["vms"][k]["job"] = jn
    app.mark_cases()

    # 대상을 바꾸면 고른 케이스에서 선택을 해제한다
    n_log = len(LOG)
    app.commit_prof(0, "target", "VM")
    check(app.profiles[0]["target"] == "VM" and app.cases[1]["esx_profiles"] == [], "ESX 호스트 프로파일의 대상을 VM 으로 바꾸면 케이스 행에서 해제")
    app.commit_prof(0, "target", "ESX 호스트")
    app.cases[1]["esx_profiles"] = ["numa0"]
    app.mark_cases()
    n_log = len(LOG)
    app.commit_prof(2, "target", "ESX 호스트")
    check(app.profiles[2]["target"] == "ESX" and app.cases[1]["vms"][0]["profiles"] == ["pin"]
          and any(x[0] == "info" and "선택을 해제함" in x[1] for x in LOG[n_log:]), "대상이 바뀐 프로파일은 케이스에서 선택 해제")
    app.commit_prof(2, "target", "VM")
    app.commit_prof(1, "name", "pin1g")
    check(app.cases[1]["vms"][0]["profiles"] == ["pin1g"], "프로파일 이름을 바꾸면 그 이름을 쓰던 케이스도 바뀜")
    app.commit_prof(1, "name", "pin")
    app.t_prof.refresh(2)
    app.del_prof_row()
    check([q["name"] for q in app.profiles] == ["numa0", "pin"], "프로파일 삭제")

    # 다시 시각 지정으로: v1.4 와 같은 화면으로 돌아가고, 고른 설정은 경고만 하고 쓰지 않는다
    app.v_mode.set("시각 지정")
    app.mode_changed()
    vis = list(app.t_case.tree.cget("displaycolumns"))
    check("prof" not in vis and "start" in vis and app.blk_vm.frame.cget("text").startswith("B. "), "시각 지정으로 돌아가면 열과 블록 제목도 돌아감")
    n_log = len(LOG)
    app.generate()
    res = app.result
    check(res is not None and res["mode"] == "time" and any("순서 실행에서만 적용" in w for w in res["warnings"])
          and any(x[0] == "ask" and "순서 실행에서만 적용" in x[1] for x in LOG[n_log:]) and res["vm"] == app.blk_vm.get("1.0", "end-1c"),
          "시각 지정: 설정 프로파일은 경고만 하고 블록 A, B, C, D 를 만든다")
    check(res["setup"] == core.setup_block() and "tcrun" not in res["setup"], "시각 지정의 A 블록은 v1.4 와 같다")

    # 오류가 있으면 만들지 않음
    app.cases[1]["vms"][2]["job"] = "nojob"
    app.generate()
    check(app.result is None and app.blk_vm.get("1.0", "end-1c") == "", "오류가 있으면 블록 비움")
    app.cases[1]["vms"][2]["job"] = "job_b4"

    # 백업(위치 선택), 백업 불러오기, 폴더 열기
    BK = os.path.join(TMP, "my_backups")
    ASK_DIR.append(BK)
    app.backup()
    bks = sorted(d for d in os.listdir(BK) if d.startswith("tcgen2_backup_")) if os.path.isdir(BK) else []
    check(len(bks) == 1 and all(os.path.exists(os.path.join(BK, bks[0], f)) for f in core.DATA_FILES),
          "설정 백업: 고른 폴더에 tcgen2_backup_ 폴더와 데이터 파일 5개(설정 프로파일 포함)")
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
    check(app2.mode() == "time" and [q["name"] for q in app2.profiles] == ["numa0", "pin"]
          and [r["proc"] for r in app2.run_cmds] == ["run_a.sh", "run_b.sh -n 4"] and app2.v_jlog.get() == "/data/tcgen_log",
          "다시 실행 시 진행 방식, 설정 프로파일, 프로세스 이름, 점프 VM 로그 폴더 불러옴")
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
    check(app4.cases[0]["vms"] == [{"name": "esx09ev01", "job": "job_a", "monitors": ["guest_perf"], "end_ref": False, "profiles": []},
                                   {"name": "esx09ev02", "job": "(없음)", "monitors": ["guest_perf"], "end_ref": False, "profiles": []}],
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

    # v2 를 처음 실행: v1 데이터 폴더(tcgen_data)에 데이터가 있으면 파일 4개를 복사해 쓴다. v1 폴더는 그대로 둔다
    v1 = os.environ["TCGEN_V1_DATA_DIR"]
    os.makedirs(os.path.join(v1, "testcases"))
    with open(os.path.join(v1, core.RUN_FILE), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["App명", "명령어 명", "잡 조건 설명", "경로/실행 명령"])          # v1.4 형식: 프로세스 이름 열이 없다
        w.writerow(["V1", "v1_job", "", "/x/v1.sh"])
    with open(os.path.join(v1, core.SETTINGS_FILE), "w", encoding="utf-8") as f:
        json.dump({"vm_user": "v1user", "mon_lead_min": 7, "stop_lead_min": 12, "record_csv": os.path.join(v1, "testcases", "r.csv")}, f)
    with open(os.path.join(v1, "testcases", "r.csv"), "w") as f:
        f.write("keep\n")
    before = dict((n, open(os.path.join(v1, n), "rb").read()) for n in (core.RUN_FILE, core.SETTINGS_FILE))
    core.save_config({})
    first = os.path.join(TMP, "data_first_v2")
    os.environ["TCGEN_DEFAULT_DATA_DIR"] = first
    core.app_dir = lambda: PROG
    n_log = len(LOG)
    root5 = tk.Tk()
    app5 = appmod.App(root5)
    check(app5.base == os.path.normpath(first) and [r["name"] for r in app5.run_cmds] == ["v1_job"] and app5.v_user.get() == "v1user",
          "v2 처음 실행: v1 의 명령 목록과 설정을 복사해 씀")
    check(any(x[0] == "info" and "v1 의 데이터를 복사해 왔다" in x[1] for x in LOG[n_log:]), "복사했다는 안내 표시")
    check(app5.mode() == "seq" and app5.run_cmds[0]["proc"] == "", "v1 데이터에는 진행 방식과 프로세스 이름이 없어 기본값")
    app5.commit_run(0, "proc", "v1.sh")
    app5.v_user.set("changed-in-v2")
    check(dict((n, open(os.path.join(v1, n), "rb").read()) for n in before) == before and sorted(os.listdir(v1)) ==
          sorted([core.RUN_FILE, core.SETTINGS_FILE, "testcases"]), "v2 에서 고쳐도 v1 폴더의 파일은 그대로")
    check(core.resolve_paths(first, core.load_settings(first))["record_csv"] == os.path.join(first, "testcases", "r.csv"),
          "v1 폴더 안을 가리키던 기록 CSV 경로는 v2 폴더로 바꿈")
    root5.destroy()

    # 테스트 케이스 파일 (새 데이터 폴더에서): 불러오기, 다른 이름으로 저장, 지정한 파일로 자동 저장, 하루 두 번 자동 백업
    core.save_config({})
    cf_dir = os.path.join(TMP, "data_casefile")
    ext = os.path.join(TMP, "my_case_files")
    os.makedirs(ext)
    os.environ["TCGEN_DEFAULT_DATA_DIR"] = cf_dir
    os.environ["TCGEN_V1_DATA_DIR"] = os.path.join(TMP, "no_v1_data")
    os.environ["TCGEN_V1_CONFIG_DIR"] = os.path.join(TMP, "no_v1_cfg")
    root6 = tk.Tk()
    app6 = appmod.App(root6)
    root6.geometry("%sx%s+20+40" % (os.environ.get("TCGEN_W", "1500"), os.environ.get("TCGEN_H", "900")))
    root6.update()
    default_file = os.path.join(cf_dir, core.CASES_FILE)
    check(app6.v_cfile.get().startswith(default_file) and "지정하지 않음" in app6.v_cfile.get()
          and "테스트 케이스 파일: 기본" in app6.v_status.get(), "파일을 정하지 않으면 데이터 폴더의 기본 파일을 쓴다고 보임")
    check(not [w for w in walk(root6) if isinstance(w, ttk.Button) and w.cget("text") in ("+ 새 종류", "종류 삭제")],
          "테스트 종류를 만드는 버튼은 없음")
    app6.add_cmd_row(app6.t_run, "run")
    app6.t_run.finish_edit(commit=False)
    for key, val in (("app", "AppA"), ("name", "job_a"), ("cmd", "/home/tester/bench/run_a.sh"), ("proc", "run_a.sh")):
        app6.commit_run(0, key, val)
    app6.add_case()
    app6.t_case.finish_edit(commit=False)
    app6.commit_case("c0", "name", "SPR-base")
    app6.commit_case("c0", "esx", "spr01")
    c = app6.cases[0]
    c["vms"][0].update(job="job_a", profiles=["lpage_1g_on"])
    c.update(esx_profiles=["ht_wholecore_1"], applied=True, max_wait="12:00")
    app6.mark_cases()
    check([x["name"] for x in core.load_cases(cf_dir)] == ["SPR-base"], "파일을 정하지 않은 동안에는 데이터 폴더에 자동 저장")
    auto = os.path.join(cf_dir, "backup", "auto")
    check(os.path.isdir(auto) and len(os.listdir(auto)) == 1 and os.listdir(auto)[0] == core.auto_backup_slot(dt.datetime.now()),
          "생성기를 켜면 그 시간대(오전 또는 오후)의 자동 백업을 한 번 만듦 %s" % (os.listdir(auto) if os.path.isdir(auto) else None))
    app6.auto_backup_tick()
    check(len(os.listdir(auto)) == 1, "같은 시간대에는 자동 백업을 다시 만들지 않음")
    # [다른 이름으로 저장]: 지금 표를 고른 파일에 저장하고 그 파일에 자동 저장
    spr = os.path.join(ext, "spr_cases.csv")
    ASK_SAVE.append(spr)
    press(root6, "다른 이름으로 저장")
    root6.update()
    check(os.path.isfile(spr) and app6.v_cfile.get() == spr and core.case_file(app6.settings) == spr
          and "spr_cases.csv" in app6.v_status.get(), "[다른 이름으로 저장] 뒤 그 파일을 쓰는 것으로 바뀜")
    with open(spr, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    check(rows[0] == core.CASE_CSV_HEADER and [r[0] for r in rows[1:]] == ["케이스", "VM", "VM"] and rows[1][2] == "SPR-base"
          and rows[2][8] == "lpage_1g_on" and rows[1][8] == "ht_wholecore_1" and rows[1][1] == "예", "저장한 CSV 에 케이스, VM, 설정, 적용 체크가 들어 있음")
    app6.commit_case("c0", "desc", "저장 뒤에 고친 설명")
    check(core.load_cases(cf_dir, spr)[0]["desc"] == "저장 뒤에 고친 설명" and core.load_cases(cf_dir)[0]["desc"] == "",
          "그 뒤에 고친 내용은 지정한 파일에 자동 저장되고 기본 파일은 그대로")
    app6.v_record.set(os.path.join(ext, "spr_record.csv"))
    # 다른 파일로 저장해 EMR 케이스를 만든다
    emr = os.path.join(ext, "emr_cases.csv")
    ASK_SAVE.append(emr)
    press(root6, "다른 이름으로 저장")
    root6.update()
    check(app6.v_cfile.get() == emr and app6.v_record.get() == os.path.join(ext, "spr_record.csv"),
          "새 파일로 저장하면 기록 CSV 는 지금 값을 그대로 씀")
    app6.commit_case("c0", "name", "EMR-base")
    app6.commit_case("c0", "esx", "emr01")
    app6.cases[0]["vms"][0]["profiles"] = ["vpmc_on"]
    app6.cases[0]["applied"] = False
    app6.mark_cases()
    app6.v_record.set(os.path.join(ext, "emr_record.csv"))
    app6.commit_run(0, "desc", "EMR 작업 중에 고친 설명")
    check(core.load_cases(cf_dir, spr)[0]["name"] == "SPR-base", "이전 파일은 마지막 상태로 남음")
    # [불러오기]: 잘못된 파일은 거부, SPR 파일을 불러오면 케이스와 지정한 설정, 기록 CSV 가 그대로
    rec_like = os.path.join(ext, "not_cases.csv")
    with open(rec_like, "w", encoding="utf-8-sig") as f:
        f.write("테스트 케이스 이름,생성 시각\nx,y\n")
    n_log = len(LOG)
    ASK_OPEN.append(rec_like)
    press(root6, "불러오기")
    root6.update()
    check(app6.v_cfile.get() == emr and any(x[0] == "error" and "테스트 기록 CSV" in x[1] for x in LOG[n_log:]),
          "테스트 케이스 파일이 아닌 CSV 는 불러오지 않고 알림")
    ASK_OPEN.append(spr)
    press(root6, "불러오기")
    root6.update()
    c = app6.cases[0] if app6.cases else {}
    check(app6.v_cfile.get() == spr and [x["name"] for x in app6.cases] == ["SPR-base"], "[불러오기] 로 SPR 파일의 케이스가 보임")
    check(c["applied"] and c["esx_profiles"] == ["ht_wholecore_1"] and c["vms"][0]["profiles"] == ["lpage_1g_on"]
          and c["vms"][0]["job"] == "job_a" and c["max_wait"] == "12:00" and c["desc"] == "저장 뒤에 고친 설명",
          "불러오면 케이스에 지정한 설정, 잡, 적용 체크, 최대 대기가 그대로")
    check(cell_of(app6.t_case, "c0v0", "prof") == "lpage_1g_on" and cell_of(app6.t_case, "c0", "prof").startswith("ht_wholecore_1"),
          "탭 4 의 설정 칸에도 그대로 보임")
    check(app6.v_record.get() == os.path.join(ext, "spr_record.csv"), "그 파일에서 쓰던 테스트 기록 CSV 로 함께 바뀜")
    check(app6.run_cmds[0]["desc"] == "EMR 작업 중에 고친 설명" and len(app6.profiles) == 12, "실행 명령과 설정 프로파일은 파일을 바꿔도 그대로(공통)")
    check(core.load_cases(cf_dir, emr)[0]["name"] == "EMR-base", "EMR 파일에는 EMR 케이스가 저장돼 있음")
    shot(root6, app6, 3, "tcgen_casefile_tab4.png")
    # 생성: 지금 파일의 기록 CSV 에 기록. 파일을 바꾸면 탭 5 의 이전 결과를 지움
    app6.cases[0]["applied"] = False
    app6.mark_cases()
    app6.generate()
    root6.update()
    check(app6.result is not None and app6.result["errors"] == [] and os.path.isfile(os.path.join(ext, "spr_record.csv"))
          and not os.path.exists(os.path.join(ext, "emr_record.csv")), "생성 기록은 지금 파일의 테스트 기록 CSV 에 추가")
    ASK_OPEN.append(emr)
    press(root6, "불러오기")
    root6.update()
    check(app6.result is None and app6.blk_setup.get("1.0", "end").strip() == "" and "emr_cases.csv" in app6.res_text.get("1.0", "end"),
          "파일을 바꾸면 탭 5 의 이전 생성 결과를 지움")
    check(app6.v_record.get() == os.path.join(ext, "emr_record.csv") and [x["name"] for x in app6.cases] == ["EMR-base"],
          "EMR 파일에서 쓰던 기록 CSV 와 케이스가 그대로")
    # 데이터 폴더의 기본 파일을 고르면 '지정하지 않음' 상태로 돌아간다
    ASK_OPEN.append(default_file)
    press(root6, "불러오기")
    root6.update()
    check(core.case_file(app6.settings) == "" and "지정하지 않음" in app6.v_cfile.get() and app6.cases[0]["desc"] == "",
          "데이터 폴더의 기본 파일을 고르면 지정하지 않은 상태로 돌아감")
    ASK_OPEN.append(emr)
    press(root6, "불러오기")
    root6.update()
    root6.destroy()
    root7 = tk.Tk()
    app7 = appmod.App(root7)
    root7.update()
    check(app7.v_cfile.get() == emr and [x["name"] for x in app7.cases] == ["EMR-base"] and app7.v_record.get() == os.path.join(ext, "emr_record.csv"),
          "다시 실행하면 마지막에 쓰던 테스트 케이스 파일과 기록 CSV 를 불러옴")
    root7.destroy()
    # 지정한 파일이 없어졌으면 기본 파일로 바꾸고 알린다
    os.rename(emr, emr + ".moved")
    n_log = len(LOG)
    root8 = tk.Tk()
    app8 = appmod.App(root8)
    root8.update()
    check(core.case_file(app8.settings) == "" and [x["name"] for x in app8.cases] == ["SPR-base"]
          and any(x[0] == "warning" and "기본 파일로 바꿈" in x[1] for x in LOG[n_log:]) and os.path.isfile(emr + ".moved"),
          "지정한 파일을 찾지 못하면 기본 파일로 바꾸고 알림")
    root8.destroy()
    print("GUI smoke OK  tmp=%s" % TMP)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
