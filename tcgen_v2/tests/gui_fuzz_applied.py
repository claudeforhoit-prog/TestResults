# -*- coding: utf-8 -*-
"""
적용 체크한 케이스의 값이 바뀌지 않는지 보는 무작위 점검 (수동 실행, 화면이 있는 PC 에서).

    python tests/gui_fuzz_applied.py [시드] [횟수]        기본: 시드 1, 300번

실제 화면 코드로 탭 4 의 조작(케이스 추가, 삭제, 위로, 아래로, 시작 시각 입력(날짜 시각, +간격, 지우기), 적용 체크, 잡 종료 입력,
다시 실행, 그리고 화면에서 고를 수 없는 시각 지정 화면의 조작)을 무작위 순서로 하고, 조작마다 적용 체크한 케이스의 시작 시각과
'잡 시작 (환산)' 칸, 시각 지정의 값(간격, 잡 시작, 모니터링 중지, 잡 종료)이 체크한 때와 같은지, 저장된 파일이 화면의 표와 같은지
확인한다. 다르면 그 조작과 직전 조작들을 보이고 멈춘다.
v2.7 에서는 열 번 안팎의 조작에서 값이 바뀌었다(사용자 보고 2026-11-17 "적용완료된거 순서를 바꾸거나 하면 잡 시작 시간 다 꼬이면서").
자동 시험(unittest discover)에는 포함하지 않는다.
"""
import os, random, sys, tempfile
SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1
STEPS = int(sys.argv[2]) if len(sys.argv) > 2 else 300
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
import tcgen_core as core
TMP = tempfile.mkdtemp(prefix="tcgen_fz_")
BASE = os.path.join(TMP, "data"); PROG = os.path.join(TMP, "prog"); os.makedirs(PROG)
os.environ["TCGEN_CONFIG_DIR"] = os.path.join(TMP, "cfg")
os.environ["TCGEN_DEFAULT_DATA_DIR"] = BASE
os.environ["TCGEN_V1_CONFIG_DIR"] = os.path.join(TMP, "v1cfg")
os.environ["TCGEN_V1_DATA_DIR"] = os.path.join(TMP, "v1data")
core.app_dir = lambda: PROG
import tcgen_app as appmod
mb = appmod.messagebox
for n in ("showinfo", "showerror", "showwarning"):
    setattr(mb, n, lambda *a, **k: None)
mb.askyesno = lambda *a, **k: True
appmod.open_folder = lambda p: None

rnd = random.Random(SEED)
root = tk.Tk(); app = appmod.App(root); root.update()
T, S = appmod.MODE_LABELS["time"], appmod.MODE_LABELS["seq"]
app.v_mode.set(T); app.mode_changed(); root.update()
app.run_cmds.append({"app": "sim", "name": "job_a", "desc": "", "cmd": "/home/t/run_a.sh", "proc": "run_a.sh"}); app.autosave("run")
app.mon_cmds.append({"app": "mon", "name": "gmon", "desc": "", "where": "VM", "start": "/home/t/mon.sh start", "stop": "/home/t/mon.sh stop"})
app.autosave("mon")
KEYS = ("interval", "start", "stop_mode", "stop_time", "end_time", "seq_start")
snap = {}          # 케이스 이름 -> (적용 체크한 때의 값, 고정한 자동 중지 시각 또는 None)
log = []


def rec(c):
    return tuple(c.get(k, "") for k in KEYS)


def pick():
    return rnd.randrange(len(app.cases)) if app.cases else None


def rtime():
    return "2026-11-%02d %02d:%02d:00" % (rnd.randint(18, 28), rnd.randint(0, 23), rnd.choice((0, 10, 30)))


def check(op):
    app.t_case.refresh()
    root.update()
    names = [c["name"] for c in app.cases]
    assert len(names) == len(set(names)), names
    for c in app.cases:
        n = c["name"]
        if not c.get("applied"):
            assert not c.get("applied_stop"), (op, n, "applied_stop on a case that is not applied")
            continue
        assert n in snap, (op, n)
        want, stop = snap[n]
        assert rec(c) == want, "after %s: applied case %s changed\n  was %s\n  now %s\n  ops %s" % (op, n, want, rec(c), log[-8:])
        if stop is None and c.get("applied_stop"):
            snap[n] = (want, c["applied_stop"])
        elif stop is not None:
            assert c.get("applied_stop") == stop, "after %s: fixed stop of %s changed %s -> %s" % (op, n, stop, c.get("applied_stop"))
    if app.mode() == "seq":
        # 적용 체크해 날짜 시각으로 고정된 케이스: '잡 시작 (환산)' 칸은 그 시각이고, 다른 행을 고쳐도 그대로다
        for k, c in enumerate(app.cases):
            ss = (c.get("seq_start") or "").strip()
            if c.get("applied") and ss[:2] == "20":
                assert app.seq_at_text(k) == ss[:16], "after %s: computed start of applied case %s is %s, fixed %s\n  ops %s" % (
                    op, c["name"], app.seq_at_text(k), ss, log[-8:])
    if app.mode() == "time":
        for c, s in zip(app.cases, app.sched):
            if c.get("applied") and (c.get("stop_mode") or "auto") == "auto" and c.get("applied_stop"):
                assert s["mon_stop"] == core.parse_dt(c["applied_stop"]), (op, c["name"])
    saved = core.load_cases(BASE)
    assert [(c["name"], c["applied"], rec(c), c.get("applied_stop", "")) for c in saved] == \
        [(c["name"], bool(c.get("applied")), rec(c), c.get("applied_stop", "")) for c in app.cases], "saved file differs after %s" % op


for step in range(STEPS):
    op = rnd.choice(["add", "add", "add", "del", "up", "down", "up", "down", "start", "start", "interval", "tick", "tick", "end", "stop", "mode",
                     "restart", "sstart", "sstart", "sstart", "sstart"])
    i = pick()
    if i is None and op != "add":
        op = "add"
    sel = "c%d" % i if i is not None else None
    name = app.cases[i]["name"] if i is not None else "-"
    log.append((step, op, name, app.mode()))
    if op == "add":
        if sel:
            app.t_case.tree.selection_set(sel)
        app.add_case(); app.t_case.finish_edit(commit=False)
        j = 0 if i is None else i + 1
        app.commit_case("c%d" % j, "name", "k%03d" % step)
        app.commit_case("c%d" % j, "esx", "esx01")
        app.commit_case("c%dv0" % j, "job", "job_a")
        app.cases[j]["vms"][0]["monitors"] = ["gmon"]
        if rnd.random() < 0.7 and app.mode() == "time":
            app.commit_case("c%d" % j, rnd.choice(("start", "interval")), rtime() if rnd.random() < 0.5 else "%d:00" % rnd.randint(1, 12))
    elif op == "del":
        was = app.cases[i]["name"]
        app.t_case.tree.selection_set(sel); app.del_row()
        snap.pop(was, None)
    elif op in ("up", "down"):
        app.t_case.tree.selection_set(sel); app.move_row(-1 if op == "up" else 1)
    elif op == "start" and app.mode() == "time":
        app.commit_case(sel, "start", rtime())
    elif op == "sstart" and app.mode() == "seq":
        # 시작 시각 칸: 날짜 시각, +간격, 지우기. 적용 체크한 케이스는 고쳐지지 않아야 한다(확인은 check 가 한다)
        app.commit_case(sel, "seq_start", rnd.choice((rtime(), rtime(), "+%d:00" % rnd.randint(1, 12), "+0:30", "")))
    elif op == "interval" and app.mode() == "time":
        app.commit_case(sel, "interval", rnd.choice(("", "8:00", "6:30", "12:00", "1:00")))
    elif op == "end":
        e = rtime()
        before = app.cases[i].get("end_time", "")
        app.commit_case(sel, "end_time", e)
        if app.cases[i].get("applied") and app.cases[i].get("end_time", "") != before:      # 잡 종료는 적용 케이스도 직접 고칠 수 있다
            w, st = snap[app.cases[i]["name"]]
            snap[app.cases[i]["name"]] = (rec(app.cases[i]), st)
    elif op == "stop" and app.mode() == "time":
        c = app.cases[i]
        if not c.get("applied"):               # 화면에서는 적용 케이스의 모니터링 중지 창이 열리지 않는다
            m = rnd.choice(("auto", "manual", "none"))
            if m == "manual":
                c["stop_time"] = rtime()
            c["stop_mode"] = m
            app.mark_cases()
        else:
            app.edit_stop(sel)
    elif op == "tick":
        c = app.cases[i]
        app.toggle_applied(sel)
        if c.get("applied"):
            snap[c["name"]] = (rec(c), None)
        else:
            snap.pop(c["name"], None)
    elif op == "mode":
        app.v_mode.set(S if app.mode() == "time" else T); app.mode_changed()
    elif op == "restart":
        app.on_close(); root = tk.Tk(); app = appmod.App(root); root.update()
    root.update()
    check("%d %s %s" % (step, op, name))
print("seed %d: %d steps ok, cases %d, applied %d, mode %s" % (SEED, STEPS, len(app.cases), sum(1 for c in app.cases if c.get("applied")), app.mode()))
root.destroy()
