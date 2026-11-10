# -*- coding: utf-8 -*-
"""
tcgen_app.py - 테스트 실행 명령 생성기 화면 (tkinter, Python 표준 라이브러리만 사용)

실행: python tcgen_app.py  또는 빌드한 tcgen.exe
설정과 명령 목록은 생성기 폴더(이 파일 또는 tcgen.exe 가 있는 폴더)에 저장한다.
"""
import copy
import datetime as dt
import os
import re
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import tcgen_core as core

TITLE = "테스트 실행 명령 생성기 v%s" % core.VERSION
NONE_JOB = core.NONE_JOB


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: B606 (Windows 탐색기로 연다)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def unique_name(base, used):
    if not base or base not in used:
        return base
    m = re.match(r"^(.*)-r(\d+)$", base)
    stem, n = (m.group(1), int(m.group(2))) if m else (base, 1)
    while True:
        n += 1
        cand = "%s-r%d" % (stem, n)
        if cand not in used:
            return cand


def make_check_images(root, scale):
    """체크 칸 그림(켜짐, 꺼짐). 글꼴에 없는 기호를 쓰지 않으려고 점으로 그린다."""
    n = max(12, int(round(14 * scale)))
    off = tk.PhotoImage(master=root, width=n, height=n)
    on = tk.PhotoImage(master=root, width=n, height=n)
    off.put("#6b7480", to=(0, 0, n, n))
    off.put("#ffffff", to=(1, 1, n - 1, n - 1))
    on.put("#2f6fd1", to=(0, 0, n, n))
    t = max(2, n // 6)
    pts = [(0.18, 0.50), (0.42, 0.74), (0.82, 0.26)]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        steps = n * 3
        for k in range(steps + 1):
            x = int(round((x0 + (x1 - x0) * k / steps) * (n - 1))) - t // 2
            y = int(round((y0 + (y1 - y0) * k / steps) * (n - 1))) - t // 2
            on.put("#ffffff", to=(max(0, x), max(0, y), min(n, x + t), min(n, y + t)))
    return on, off


def next_hour():
    t = dt.datetime.now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    return core.fmt_dt(t)


# ---------------------------------------------------------------------------- 표
class EditableTable(ttk.Frame):
    """Treeview 표. 칸을 두 번 누르거나 F2, Enter 로 그 칸을 고친다. Tab 으로 다음 칸."""

    def __init__(self, master, app, columns, height=8, check=None):
        ttk.Frame.__init__(self, master)
        self.app = app
        self.columns = columns
        self.check = check           # 맨 앞 체크 열: dict(key, title, width, on, off, toggle) 또는 None
        self.rows = []
        self.display = None          # (i, key) -> 표시 문자열 또는 None
        self.on_commit = None        # (i, key, text) -> None
        self.before_refresh = None
        self.editor = None
        keys = [c["key"] for c in columns]
        self.tree = ttk.Treeview(self, columns=keys, show=("tree headings" if check else "headings"), height=height,
                                 selectmode="browse")
        if check:
            self.tree.heading("#0", text=check["title"], anchor="center")
            self.tree.column("#0", width=app.px(check["width"]), minwidth=app.px(check["width"]), stretch=False,
                             anchor="center")
        self.tree.tag_configure("applied", foreground="#8a8f98")
        for c in columns:
            self.tree.heading(c["key"], text=c["title"], anchor="w")
            self.tree.column(c["key"], width=app.px(c["width"]), minwidth=app.px(40),
                             stretch=bool(c.get("stretch")), anchor="w")
        ys = ttk.Scrollbar(self, orient="vertical", command=lambda *a: (self.finish_edit(), self.tree.yview(*a)))
        xs = ttk.Scrollbar(self, orient="horizontal", command=lambda *a: (self.finish_edit(), self.tree.xview(*a)))
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Button-1>", lambda e: self.finish_edit(), add="+")
        self.tree.bind("<Return>", self._key_edit)
        self.tree.bind("<F2>", self._key_edit)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.tree.bind(seq, lambda e: self.finish_edit(), add="+")
        if check:
            self.tree.bind("<Button-1>", self._click_check, add="+")
            self.tree.bind("<space>", self._space_check)

    # 체크 열
    def _click_check(self, ev):
        try:
            region = self.tree.identify_region(ev.x, ev.y)
        except tk.TclError:
            return None
        iid = self.tree.identify_row(ev.y)
        if region != "tree" or not iid:
            return None
        self.tree.selection_set(iid)
        self.check["toggle"](int(iid))
        return "break"

    def _space_check(self, ev):
        i = self.selected()
        if i is not None:
            self.check["toggle"](i)
        return "break"

    # 표시
    def cell_text(self, i, key):
        if self.display:
            v = self.display(i, key)
            if v is not None:
                return v
        return self.rows[i].get(key, "")

    def refresh(self, select=None):
        if select is None:
            select = self.selected()
        if self.before_refresh:
            self.before_refresh()
        self.tree.delete(*self.tree.get_children())
        for i in range(len(self.rows)):
            kw = {}
            if self.check:
                on = bool(self.rows[i].get(self.check["key"]))
                kw = {"image": self.check["on"] if on else self.check["off"], "text": "", "tags": ("applied",) if on else ()}
            self.tree.insert("", "end", iid=str(i), values=[self.cell_text(i, c["key"]) for c in self.columns], **kw)
        if select is not None and self.rows:
            select = max(0, min(select, len(self.rows) - 1))
            self.tree.selection_set(str(select))
            self.tree.focus(str(select))
            self.tree.see(str(select))

    def selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # 편집
    def _double(self, ev):
        try:
            region = self.tree.identify_region(ev.x, ev.y)
        except tk.TclError:
            region = "cell"
        iid, col = self.tree.identify_row(ev.y), self.tree.identify_column(ev.x)
        if self.check and region == "tree":
            # 빠른 두 번째 클릭은 Tk 가 더블클릭으로 처리하므로 여기서도 체크를 바꾼다(누를 때마다 바뀜)
            if iid:
                self.check["toggle"](int(iid))
            return "break"
        if region != "cell" or not iid or not col:
            return None
        self.start_edit(int(iid), int(col.lstrip("#")) - 1)
        return "break"

    def _key_edit(self, ev):
        i = self.selected()
        if i is None:
            return None
        for ci, c in enumerate(self.columns):
            if c.get("kind", "entry") in ("entry", "combo"):
                self.start_edit(i, ci)
                break
        return "break"

    def start_edit(self, i, ci):
        self.finish_edit()
        if i >= len(self.rows):
            return
        c = self.columns[ci]
        kind = c.get("kind", "entry")
        if kind == "readonly":
            return
        if kind == "popup":
            c["popup"](i)
            return
        self.tree.selection_set(str(i))
        self.tree.see(str(i))
        self.tree.update_idletasks()
        bbox = self.tree.bbox(str(i), c["key"])
        if not bbox:
            return
        x, y, w, h = bbox
        var = tk.StringVar(value=self.rows[i].get(c["key"], ""))
        if kind == "combo":
            ed = ttk.Combobox(self.tree, textvariable=var, values=list(c["values"]()), state="readonly")
            ed.bind("<<ComboboxSelected>>", lambda e, w=ed: self.tree.after_idle(lambda: self._finish_if(w, True)))
        else:
            ed = ttk.Entry(self.tree, textvariable=var)
            ed.bind("<FocusOut>", lambda e, w=ed: self.tree.after_idle(lambda: self._finish_if(w, False)))
        tw = self.tree.winfo_width()
        width = max(w, self.app.px(c.get("edit_width", 0)))
        if x + width > tw:
            width = max(w, tw - x)
        ed.place(x=x, y=y, width=width, height=h)
        ed.focus_set()
        if kind == "entry":
            ed.icursor("end")
            ed.select_range(0, "end")
        ed.bind("<Return>", lambda e: self._finish_key(True))
        ed.bind("<KP_Enter>", lambda e: self._finish_key(True))
        ed.bind("<Escape>", lambda e: self._finish_key(False))
        ed.bind("<Tab>", lambda e: self._tab(i, ci, 1))
        for seq in ("<Shift-Tab>", "<ISO_Left_Tab>"):
            try:
                ed.bind(seq, lambda e: self._tab(i, ci, -1))
            except tk.TclError:
                pass
        self.editor = (ed, var, i, c["key"])
        if kind == "combo":
            ed.after_idle(lambda: self._post_combo(ed))

    def _finish_if(self, widget, refocus):
        if self.editor and self.editor[0] is widget:
            self.finish_edit(refocus=refocus)

    def _post_combo(self, ed):
        try:
            ed.event_generate("<Down>")
        except tk.TclError:
            pass

    def _finish_key(self, commit):
        self.finish_edit(commit=commit, refocus=True)
        return "break"

    def _tab(self, i, ci, step):
        self.finish_edit(refocus=True)
        n = ci + step
        while 0 <= n < len(self.columns):
            if self.columns[n].get("kind", "entry") in ("entry", "combo"):
                self.start_edit(i, n)
                break
            n += step
        return "break"

    def finish_edit(self, commit=True, refocus=False):
        if not self.editor:
            return
        ed, var, i, key = self.editor
        self.editor = None
        text = var.get()
        try:
            ed.destroy()
        except tk.TclError:
            pass
        if commit and i < len(self.rows) and text != self.rows[i].get(key, ""):
            if self.on_commit:
                self.on_commit(i, key, text)
            else:
                self.rows[i][key] = text
                self.refresh(i)
        if refocus:
            self.tree.focus_set()


def make_readonly(text):
    """Text 를 읽기 전용으로 쓴다(선택과 복사는 가능)."""
    def on_key(ev):
        if ev.keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Shift_L", "Shift_R",
                         "Control_L", "Control_R"):
            return None
        if ev.state & (0x4 | 0x8 | 0x10) and ev.keysym.lower() in ("c", "a", "insert"):
            return None
        return "break"

    def select_all(ev):
        text.tag_add("sel", "1.0", "end-1c")
        return "break"

    text.bind("<Key>", on_key)
    text.bind("<Control-a>", select_all)
    text.bind("<Control-A>", select_all)
    for ev in ("<<Paste>>", "<<Cut>>", "<<Clear>>"):
        text.bind(ev, lambda e: "break")


def set_text(text, value):
    text.delete("1.0", "end")
    text.insert("1.0", value)


# ---------------------------------------------------------------------------- 화면
class App(object):
    def __init__(self, root):
        self.root = root
        self.base = core.app_dir()
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.settings = core.load_settings(self.base)
        paths = core.resolve_paths(self.base, self.settings)
        self.run_cmds = core.load_rows(os.path.join(self.base, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)
        self.mon_cmds = core.load_rows(os.path.join(self.base, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS)
        self.load_msg = ""
        try:
            self.cases = core.load_cases(self.base)
        except (OSError, ValueError) as e:
            self.cases = []
            bad = os.path.join(self.base, core.CASES_FILE)
            keep = bad + ".bad-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                os.replace(bad, keep)
            except OSError:
                keep = bad
            self.load_msg = "%s 를 읽지 못해 빈 표로 시작함 (원래 파일: %s) %s" % (core.CASES_FILE, keep, e)
        self.sched = []
        self.dirty = {"settings": False, "run": False, "mon": False, "cases": False}   # True = 자동 저장 실패
        self.save_warned = {}
        self.last_saved = None
        self.result = None
        self.setup_style()
        self.v_record = tk.StringVar(value=paths["record_csv"])
        self.v_log = tk.StringVar(value=paths["log_dir"])
        self.v_user = tk.StringVar(value=self.settings.get("vm_user", ""))
        self.v_mon = tk.StringVar(value=str(self.settings["mon_lead_min"]))
        self.v_stop = tk.StringVar(value=str(self.settings["stop_lead_min"]))
        self.v_status = tk.StringVar()
        self.v_esx_host = tk.StringVar()
        self.build()
        for v in (self.v_record, self.v_log, self.v_user, self.v_mon, self.v_stop):
            v.trace_add("write", self._settings_changed) if hasattr(v, "trace_add") else v.trace("w", self._settings_changed)
        self.update_status()
        if self.load_msg:
            self.v_status.set(self.load_msg)
            messagebox.showwarning(TITLE, self.load_msg, parent=root)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------ 모양
    def px(self, n):
        return int(round(n * self.scale))

    def setup_style(self):
        fams = set(tkfont.families(self.root))
        ui = next((f for f in ("Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR")
                   if f in fams), None)
        self.mono = next((f for f in ("Consolas", "D2Coding", "Menlo", "DejaVu Sans Mono", "Courier New") if f in fams),
                         "Courier")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                f = tkfont.nametofont(name)
                if ui:
                    f.configure(family=ui)
            except tk.TclError:
                pass
        base = tkfont.nametofont("TkDefaultFont")
        self.bold = base.copy()
        self.bold.configure(weight="bold")
        self.small = base.copy()
        self.small.configure(size=max(8, abs(int(base.cget("size"))) - 1))
        self.mono_font = tkfont.Font(family=self.mono, size=max(9, abs(int(base.cget("size")))))
        style = ttk.Style(self.root)
        line = base.metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.45))
        try:   # 일부 Tk 8.6 버전에서 행 글자색(tag)이 적용되지 않는 문제 회피
            style.map("Treeview", foreground=[e for e in style.map("Treeview", query_opt="foreground")
                                              if e[:2] != ("!disabled", "!selected")])
        except tk.TclError:
            pass
        self.img_on, self.img_off = make_check_images(self.root, self.scale)
        style.configure("Hint.TLabel", foreground="#5b6470", font=self.small)
        style.configure("Ok.TLabel", foreground="#1e7b34", font=self.bold)
        style.configure("Bold.TLabel", font=self.bold)

    def hint(self, parent, text):
        lb = ttk.Label(parent, text=text, style="Hint.TLabel", justify="left", wraplength=self.px(280))
        lb.bind("<Configure>", lambda e: lb.configure(wraplength=max(200, e.width - 10)))
        return lb

    # ------------------------------------------------------------ 배치
    def build(self):
        r = self.root
        r.title(TITLE)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(self.px(1480), int(sw * 0.95)), min(self.px(900), int(sh * 0.9))
        r.geometry("%dx%d" % (w, h))
        r.minsize(min(self.px(1000), w), min(self.px(640), h))
        outer = ttk.Frame(r, padding=self.px(6))
        outer.pack(fill="both", expand=True)

        sf = ttk.LabelFrame(outer, text="설정 (바뀔 때마다 생성기 폴더의 settings.json 에 자동 저장)", padding=self.px(6))
        sf.pack(fill="x")
        row1 = ttk.Frame(sf)
        row1.pack(fill="x")
        ttk.Label(row1, text="테스트 기록 CSV").grid(row=0, column=0, sticky="w")
        ttk.Entry(row1, textvariable=self.v_record, width=30).grid(row=0, column=1, sticky="ew", padx=(4, 2))
        ttk.Button(row1, text="찾기", command=self.pick_record, width=6).grid(row=0, column=2, padx=(0, 14))
        ttk.Label(row1, text="로그 폴더").grid(row=0, column=3, sticky="w")
        ttk.Entry(row1, textvariable=self.v_log, width=24).grid(row=0, column=4, sticky="ew", padx=(4, 2))
        ttk.Button(row1, text="찾기", command=self.pick_log, width=6).grid(row=0, column=5)
        row1.columnconfigure(1, weight=3)
        row1.columnconfigure(4, weight=2)
        row2 = ttk.Frame(sf)
        row2.pack(fill="x", pady=(self.px(5), 0))
        ttk.Label(row2, text="VM 접속 계정").pack(side="left")
        ttk.Entry(row2, textvariable=self.v_user, width=14).pack(side="left", padx=(4, 2))
        ttk.Label(row2, text="(비우면 점프 VM 계정)", style="Hint.TLabel").pack(side="left", padx=(0, 14))
        ttk.Label(row2, text="모니터링 시작").pack(side="left")
        tk.Spinbox(row2, from_=0, to=240, width=4, textvariable=self.v_mon).pack(side="left", padx=4)
        ttk.Label(row2, text="분 전 (잡 시작 기준)").pack(side="left", padx=(0, 14))
        ttk.Label(row2, text="모니터링 중지").pack(side="left")
        tk.Spinbox(row2, from_=0, to=240, width=4, textvariable=self.v_stop).pack(side="left", padx=4)
        ttk.Label(row2, text="분 전 (다음 케이스 잡 시작 기준)").pack(side="left")
        row3 = ttk.Frame(sf)
        row3.pack(fill="x", pady=(self.px(6), 0))
        for text, cmd in (("설정 백업", self.backup), ("설정 폴더 열기", self.open_settings_dir),
                          ("테스트 케이스 폴더 열기", self.open_case_dir)):
            ttk.Button(row3, text=text, command=cmd).pack(side="left", padx=(0, 6))
        self.hint(row3, "설정 폴더 = 생성기 폴더 (settings.json, run_commands.csv, monitor_commands.csv). "
                        "테스트 케이스 폴더 = 로그 폴더 (testcaselogging-*.csv)").pack(side="left", fill="x", expand=True, padx=8)

        bar = ttk.Frame(outer)
        bar.pack(side="bottom", fill="x", pady=(self.px(4), 0))
        ttk.Label(bar, textvariable=self.v_status, style="Hint.TLabel").pack(side="left")
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(self.px(6), 0))
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.finish_all_edits())
        self.build_run_tab()
        self.build_mon_tab()
        self.build_case_tab()
        self.build_result_tab()

    def build_run_tab(self):
        f = ttk.Frame(self.nb, padding=self.px(6))
        self.nb.add(f, text=" 1. 실행 명령 ")
        cols = [dict(key="app", title="App명", width=110), dict(key="name", title="명령어 명", width=140),
                dict(key="desc", title="잡 조건 설명", width=200),
                dict(key="cmd", title="경로/실행 명령 (절대 경로, 변수 <vm이름> <실행시각>)", width=700, stretch=True,
                     edit_width=700)]
        self.t_run = EditableTable(f, self, cols, height=8)
        self.t_run.rows = self.run_cmds
        self.t_run.on_commit = self.commit_run
        h = self.hint(f, "칸을 두 번 누르면 고친다. 고칠 때마다 run_commands.csv 에 자동 저장되고 다음 실행 때 불러온다. "
                         "명령은 절대 경로로 입력한다. crontab 에 한 줄로 그대로 들어가고 cron 은 홈 폴더에서 실행한다. "
                         "<vm이름> = VM 게스트 hostname, <실행시각> = 실행 시각 14자리 (예: 20261109000000). "
                         "예: /home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1")
        h.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        b = ttk.Frame(f)
        b.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_run.pack(fill="both", expand=True)
        ttk.Button(b, text="+ 행 추가", command=lambda: self.add_cmd_row(self.t_run, "run")).pack(side="left")
        ttk.Button(b, text="삭제", command=lambda: self.del_cmd_row(self.t_run, "run")).pack(side="left", padx=6)
        self.t_run.refresh()

    def build_mon_tab(self):
        f = ttk.Frame(self.nb, padding=self.px(6))
        self.nb.add(f, text=" 2. 모니터링 명령 ")
        cols = [dict(key="app", title="App명", width=100), dict(key="name", title="명령어 명", width=130),
                dict(key="desc", title="설명", width=150),
                dict(key="where", title="실행 위치", width=80, kind="combo", values=lambda: ["VM", "ESX"]),
                dict(key="start", title="시작 명령", width=480, stretch=True, edit_width=600),
                dict(key="stop", title="중지 명령", width=320, edit_width=500)]
        self.t_mon = EditableTable(f, self, cols, height=8)
        self.t_mon.rows = self.mon_cmds
        self.t_mon.on_commit = self.commit_mon
        h = self.hint(f, "어떤 모니터링 명령이든 직접 등록한다. 고칠 때마다 monitor_commands.csv 에 자동 저장되고 다음 실행 때 불러온다. "
                         "실행 위치가 VM 이면 PVM, SVM 의 crontab 에, ESX 이면 ESX 호스트의 crontab 에 등록한다. "
                         "중지 명령을 비우면 중지 줄을 만들지 않는다. 변수는 탭 1 과 같다 (ESX 의 <vm이름> = ESX 호스트 이름).")
        h.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        b = ttk.Frame(f)
        b.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_mon.pack(fill="both", expand=True)
        ttk.Button(b, text="+ 행 추가", command=lambda: self.add_cmd_row(self.t_mon, "mon")).pack(side="left")
        ttk.Button(b, text="삭제", command=lambda: self.del_cmd_row(self.t_mon, "mon")).pack(side="left", padx=6)
        self.t_mon.refresh()

    def build_case_tab(self):
        f = ttk.Frame(self.nb, padding=self.px(6))
        self.nb.add(f, text=" 3. 테스트 케이스 ")
        job_values = lambda: [NONE_JOB] + [r["name"] for r in self.run_cmds if r.get("name")]
        cols = [dict(key="name", title="케이스 이름", width=230), dict(key="esx", title="ESX 호스트", width=100),
                dict(key="pvm", title="PVM", width=120), dict(key="svm", title="SVM", width=120),
                dict(key="pvm_job", title="PVM 잡", width=110, kind="combo", values=job_values),
                dict(key="svm_job", title="SVM 잡", width=110, kind="combo", values=job_values),
                dict(key="interval", title="간격", width=60),
                dict(key="start", title="잡 시작 (KST)", width=150),
                dict(key="monitors", title="모니터링", width=220, kind="popup", popup=self.edit_monitors, stretch=True),
                dict(key="mon_start", title="모니터링 시작", width=105, kind="readonly"),
                dict(key="mon_stop", title="모니터링 중지", width=150, kind="popup", popup=self.edit_stop)]
        self.t_case = EditableTable(f, self, cols, height=8,
                                    check=dict(key="applied", title="적용", width=48, on=self.img_on, off=self.img_off,
                                               toggle=self.toggle_applied))
        self.t_case.rows = self.cases
        self.t_case.on_commit = self.commit_case
        self.t_case.display = self.case_display
        self.t_case.before_refresh = self.calc_schedule
        self.case_hint = self.hint(f, "")
        self.case_hint.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        b = ttk.Frame(f)
        b.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_case.pack(fill="both", expand=True)
        ttk.Button(b, text="+ 케이스 추가", command=self.add_case).pack(side="left")
        ttk.Button(b, text="삭제", command=self.del_case).pack(side="left", padx=6)
        ttk.Button(b, text="위로", command=lambda: self.move_case(-1)).pack(side="left")
        ttk.Button(b, text="아래로", command=lambda: self.move_case(1)).pack(side="left", padx=6)
        gen = tk.Button(b, text="  명령어 생성  ", command=self.generate, font=self.bold, bg="#c0392b", fg="white",
                        activebackground="#a93226", activeforeground="white", relief="raised", bd=1)
        gen.pack(side="right")
        self.update_case_hint()
        self.t_case.refresh()

    def build_result_tab(self):
        f = ttk.Frame(self.nb, padding=self.px(6))
        self.nb.add(f, text=" 4. 생성 결과 ")
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.res_text = tk.Text(top, height=6, wrap="word", relief="solid", bd=1, font=self.small)
        self.res_text.pack(fill="x")
        make_readonly(self.res_text)
        self.res_text.tag_configure("ok", foreground="#1e7b34", font=self.bold)
        self.res_text.tag_configure("err", foreground="#b03a2e")
        self.res_text.tag_configure("warn", foreground="#9a6700")
        set_text(self.res_text, "탭 3 에서 [명령어 생성] 을 누르면 여기에 결과가 나온다.")
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=(self.px(6), 0))
        self.blk_setup = self._block_panel(pw, "A. 점프 VM 최초 1회 설정 (비밀번호 저장)",
                                           "점프 VM 에서 처음 한 번만. 이 블록만 붙여넣고, VM password: 가 나오면 VM 비밀번호를 입력한다 "
                                           "(화면에 보이지 않음). ~/.tcgen 에 본인만 읽을 수 있게 저장된다. 비밀번호를 바꿀 때는 "
                                           "sh ~/.tcgen/setpw 를 실행한다. ESX 비밀번호는 저장하지 않는다.",
                                           lambda: self.copy_block("setup"))
        self.blk_vm = self._block_panel(pw, "B. VM 등록 블록 (점프 VM 터미널에 붙여넣기)",
                                        "어느 폴더에서 붙여넣어도 결과는 같다. VM 마다 ssh 로 접속해 crontab 에 줄을 등록하고 등록된 줄을 출력한다. "
                                        "다시 붙여넣어도 줄이 늘지 않는다.", lambda: self.copy_block("vm"))
        self.blk_esx = self._block_panel(pw, "C. ESX 블록 (root SSH 창에 붙여넣기)",
                                         "호스트를 고른 뒤 복사해 그 ESX 의 root SSH 창에 붙여넣는다. crontab 시각은 UTC (KST - 9시간). "
                                         "ESX 를 재부팅하면 등록한 줄이 지워진다.", lambda: self.copy_block("esx"), esx=True)

    def _block_panel(self, pw, title, note, copy_cmd, esx=False):
        lf = ttk.LabelFrame(pw, text=title, padding=self.px(4))
        pw.add(lf, weight=1)
        hb = ttk.Frame(lf)
        hb.pack(fill="x")
        tk.Button(hb, text=" 복사 ", command=copy_cmd, font=self.bold, bg="#c0392b", fg="white",
                  activebackground="#a93226", activeforeground="white", bd=1).pack(side="right")
        if esx:
            ttk.Label(hb, text="ESX 호스트").pack(side="left")
            self.cb_esx = ttk.Combobox(hb, textvariable=self.v_esx_host, state="readonly", width=14)
            self.cb_esx.pack(side="left", padx=4)
            self.cb_esx.bind("<<ComboboxSelected>>", lambda e: self.show_esx_block())
        self.hint(lf, note).pack(fill="x", pady=(self.px(4), self.px(4)))
        body = ttk.Frame(lf)
        body.pack(fill="both", expand=True)
        t = tk.Text(body, wrap="none", font=self.mono_font, relief="solid", bd=1, width=40, height=10)
        ys = ttk.Scrollbar(body, orient="vertical", command=t.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        t.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        make_readonly(t)
        return t

    # ------------------------------------------------------------ 상태
    def _settings_changed(self, *a):
        self.autosave("settings")
        if hasattr(self, "t_case"):
            self.update_case_hint()
            self.t_case.refresh()

    SAVE_NAMES = (("설정", "settings"), ("탭 1 실행 명령", "run"), ("탭 2 모니터링 명령", "mon"), ("탭 3 테스트 케이스", "cases"))

    def update_status(self):
        failed = [name for name, key in self.SAVE_NAMES if self.dirty[key]]
        tail = ""
        if self.last_saved:
            tail = "   자동 저장 %s" % self.last_saved.strftime("%H:%M:%S")
        if failed:
            tail += "   저장 실패: " + ", ".join(failed)
        self.v_status.set("생성기 폴더 (설정 폴더): %s%s" % (self.base, tail))

    def autosave(self, kind):
        """설정, 탭 1, 탭 2, 탭 3 을 바뀔 때마다 생성기 폴더에 저장한다. 실패하면 상태 줄과 한 번의 알림으로 알린다."""
        try:
            if kind == "settings":
                st = self.current_settings(show_error=False)
                if st is None:          # 분 칸을 입력하는 중(숫자가 아님): 숫자가 되면 저장
                    return
                core.save_settings(self.base, core.resolve_paths(self.base, st))
                self.settings = st
            elif kind == "run":
                core.save_rows(os.path.join(self.base, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS, self.run_cmds)
            elif kind == "mon":
                core.save_rows(os.path.join(self.base, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS, self.mon_cmds)
            else:
                core.save_cases(self.base, self.cases)
            self.dirty[kind] = False
            self.last_saved = dt.datetime.now()
        except OSError as e:
            self.dirty[kind] = True
            if not self.save_warned.get(kind):
                self.save_warned[kind] = True
                name = dict((k, n) for n, k in self.SAVE_NAMES)[kind]
                messagebox.showerror(TITLE, "%s 을 자동 저장하지 못함\n%s\n%s\n\n생성기 폴더에 쓸 수 있는지 확인할 것"
                                     % (name, self.base, e), parent=self.root)
        self.update_status()

    def save_everything(self):
        self.finish_all_edits()
        for _, kind in self.SAVE_NAMES:
            self.autosave(kind)
        return not any(self.dirty.values())

    def leads(self):
        try:
            mon, stop = int(self.v_mon.get()), int(self.v_stop.get())
        except ValueError:
            return None
        return mon, stop

    def update_case_hint(self):
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        s = self.settings
        self.case_hint.configure(text=(
            "적용 칸을 누르면(또는 Space) 체크된다. 체크한 케이스는 이미 등록한 것으로 보고 [명령어 생성] 에서 뺀다. "
            "탭 3 의 표와 체크 상태는 바뀔 때마다 test_cases.json 에 저장되고 다음 실행 때 불러온다.\n"
            "칸을 두 번 누르면 고친다. ESX 호스트를 입력하면 PVM = 호스트+%s, SVM = 호스트+%s 가 자동 입력된다 (직접 수정 가능). "
            "잡 칸은 탭 1 의 명령어 명 중에서 고르고, (없음) 이면 모니터링만 등록한다.\n"
            "+ 케이스 추가: 선택한 줄을 복사해 아래에 넣는다. 간격은 그 줄 간격을 가져오고 잡 시작 = 윗줄 잡 시작 + 간격 (행마다 수정 가능). "
            "잡 시작이나 간격을 고치면 아래 행이 따라 움직인다. 위로, 아래로는 케이스 순서만 바꾸고 시각은 그 행에 남는다. "
            "시각 형식: 2026-11-09 10:00:00, 간격 형식: 12:00\n"
            "모니터링 시작 = 잡 시작 %d분 전 (자동). 모니터링 중지 = 자동 (다음 케이스 잡 시작 %d분 전, 마지막 케이스는 잡 시작 + 간격 - %d분) "
            "/ 직접 입력 / 중지 없음 중 선택. 모니터링 칸과 모니터링 중지 칸은 두 번 누르면 선택 창이 열린다."
            % (s.get("pvm_suffix") or "ev01", s.get("svm_suffix") or "ev02", ld[0], ld[1], ld[1])))

    def finish_all_edits(self):
        for t in (getattr(self, n, None) for n in ("t_run", "t_mon", "t_case")):
            if t is not None:
                t.finish_edit()

    def current_settings(self, show_error=True):
        ld = self.leads()
        if ld is None or ld[0] < 0 or ld[1] < 0:
            if show_error:
                messagebox.showerror(TITLE, "모니터링 시작, 중지 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        s = dict(self.settings)
        s.update(record_csv=self.v_record.get().strip(), log_dir=self.v_log.get().strip(), vm_user=self.v_user.get().strip(),
                 mon_lead_min=ld[0], stop_lead_min=ld[1])
        return s

    # ------------------------------------------------------------ 설정 버튼
    def pick_record(self):
        cur = self.v_record.get().strip() or core.default_paths(self.base)[0]
        opts = dict(parent=self.root, title="테스트 기록 CSV 선택 (없는 파일 이름을 적으면 새로 만든다)",
                    initialdir=os.path.dirname(cur) if os.path.isdir(os.path.dirname(cur)) else self.base,
                    initialfile=os.path.basename(cur), defaultextension=".csv",
                    filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        try:
            p = filedialog.asksaveasfilename(confirmoverwrite=False, **opts)
        except tk.TclError:
            p = filedialog.asksaveasfilename(**opts)
        if p:
            self.v_record.set(os.path.normpath(p))

    def pick_log(self):
        cur = self.v_log.get().strip()
        p = filedialog.askdirectory(parent=self.root, title="로그 폴더 선택", mustexist=False,
                                    initialdir=cur if os.path.isdir(cur) else self.base)
        if p:
            self.v_log.set(os.path.normpath(p))

    def backup(self):
        self.save_everything()
        try:
            dest, copied = core.backup_settings(self.base)
        except OSError as e:
            messagebox.showerror(TITLE, "백업하지 못함\n%s" % e, parent=self.root)
            return
        if not copied:
            try:
                os.rmdir(dest)
            except OSError:
                pass
            messagebox.showinfo(TITLE, "백업할 파일이 없음. 생성기 폴더에 저장된 파일이 없음", parent=self.root)
            return
        if messagebox.askyesno(TITLE, "백업함: %s\n\n%s\n\n복원은 이 파일들을 생성기 폴더에 덮어쓰고 생성기를 다시 실행한다.\n백업 폴더를 열까요?"
                               % (dest, "\n".join(copied)), parent=self.root):
            open_folder(dest)

    def open_settings_dir(self):
        open_folder(self.base)

    def open_case_dir(self):
        s = self.current_settings(show_error=False) or self.settings
        d = core.resolve_paths(self.base, s)["log_dir"]
        try:
            open_folder(d)
        except OSError as e:
            messagebox.showerror(TITLE, "폴더를 열지 못함\n%s\n%s" % (d, e), parent=self.root)

    # ------------------------------------------------------------ 탭 1, 2
    def add_cmd_row(self, table, kind):
        table.finish_edit()
        row = {k: "" for k in (core.RUN_FIELDS if kind == "run" else core.MON_FIELDS)}
        if kind == "mon":
            row["where"] = "VM"
        i = table.selected()
        i = len(table.rows) if i is None else i + 1
        table.rows.insert(i, row)
        self.autosave(kind)
        table.refresh(i)
        table.start_edit(i, 0)

    def del_cmd_row(self, table, kind):
        table.finish_edit()
        i = table.selected()
        if i is None:
            return
        name = table.rows[i].get("name", "")
        used = self.cases_using(kind, name) if name else []
        msg = "'%s' 행을 삭제할까요?" % (name or "%d행" % (i + 1))
        if used:
            msg += "\n\n탭 3 케이스 %d개가 이 명령을 쓰고 있음: %s" % (len(used), ", ".join(used[:5]))
        if not messagebox.askyesno(TITLE, msg, parent=self.root):
            return
        del table.rows[i]
        self.autosave(kind)
        table.refresh(i)
        self.t_case.refresh()

    def cases_using(self, kind, name):
        out = []
        for c in self.cases:
            if kind == "run" and name in (c.get("pvm_job"), c.get("svm_job")):
                out.append(c.get("name") or "?")
            if kind == "mon" and any(m["name"] == name for m in core.selected_monitors(c)):
                out.append(c.get("name") or "?")
        return out

    def _name_check(self, table, i, text):
        name = text.strip()
        if name and not core.NAME_RE.match(name):
            messagebox.showerror(TITLE, "명령어 명은 영문, 숫자, . _ - 만 사용: '%s'" % name, parent=self.root)
            return None
        if name and any(j != i and r.get("name", "").strip() == name for j, r in enumerate(table.rows)):
            messagebox.showerror(TITLE, "같은 명령어 명이 이미 있음: '%s'" % name, parent=self.root)
            return None
        return name

    def _cmd_warn(self, text, label):
        errs = core.check_command(text, label) if text.strip() else []
        if errs:
            messagebox.showwarning(TITLE, "\n".join(errs) + "\n\n이대로 두면 [명령어 생성] 에서 오류로 멈춘다.", parent=self.root)

    def commit_run(self, i, key, text):
        row = self.run_cmds[i]
        if key == "name":
            name = self._name_check(self.t_run, i, text)
            if name is None:
                return
            old = row.get("name", "")
            row["name"] = name
            if old and name != old:
                for c in self.cases:
                    for k in ("pvm_job", "svm_job"):
                        if c.get(k) == old:
                            c[k] = name or NONE_JOB
        elif key == "cmd":
            row["cmd"] = text.strip()
            self._cmd_warn(row["cmd"], "경로/실행 명령")
        else:
            row[key] = text.strip()
        self.autosave("run")
        if key == "name":
            self.autosave("cases")          # 이름을 바꾸면 탭 3 의 잡 칸도 따라 바뀜
        self.t_run.refresh(i)
        self.t_case.refresh()

    def commit_mon(self, i, key, text):
        row = self.mon_cmds[i]
        if key == "name":
            name = self._name_check(self.t_mon, i, text)
            if name is None:
                return
            old = row.get("name", "")
            row["name"] = name
            if old and name != old:
                for c in self.cases:
                    for m in c.get("monitors") or []:
                        if m["name"] == old:
                            m["name"] = name
        elif key == "where":
            old = (row.get("where") or "VM").upper()
            row["where"] = text
            if old != text.upper() and row.get("name"):
                used = self.cases_using("mon", row["name"])
                for c in self.cases:
                    c["monitors"] = [m for m in (c.get("monitors") or []) if m["name"] != row["name"]]
                if used:
                    messagebox.showinfo(TITLE, "실행 위치가 바뀌어 케이스 %d개에서 '%s' 선택을 해제함: %s"
                                        % (len(used), row["name"], ", ".join(used[:5])), parent=self.root)
        elif key in ("start", "stop"):
            row[key] = text.strip()
            self._cmd_warn(row[key], "시작 명령" if key == "start" else "중지 명령")
        else:
            row[key] = text.strip()
        self.autosave("mon")
        if key in ("name", "where"):
            self.autosave("cases")          # 탭 3 의 모니터링 선택도 따라 바뀜
        self.t_mon.refresh(i)
        self.t_case.refresh()

    # ------------------------------------------------------------ 탭 3
    def calc_schedule(self):
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        self.sched = core.case_schedule(self.cases, ld[0], ld[1])

    def case_display(self, i, key):
        c = self.cases[i]
        s = self.sched[i] if i < len(self.sched) else {}
        sel = core.selected_monitors(c)
        if key in ("pvm_job", "svm_job"):
            return c.get(key) or NONE_JOB
        if key == "monitors":
            return core.format_monitors(sel)
        if key == "mon_start":
            return core.fmt_short(s["mon_start"]) if (sel and s.get("mon_start")) else "-"
        if key == "mon_stop":
            if not sel:
                return "-"
            mode = c.get("stop_mode") or "auto"
            if mode == "none":
                return "중지 없음"
            if s.get("stop_error") or not s.get("mon_stop"):
                return "확인 필요"
            return core.fmt_short(s["mon_stop"]) + (" (직접)" if mode == "manual" else " (자동)")
        return None

    def mark_cases(self):
        """탭 3 이 바뀔 때마다 test_cases.json 에 저장한다(적용 체크 포함)."""
        self.autosave("cases")

    def toggle_applied(self, i):
        self.t_case.finish_edit()
        if i >= len(self.cases):
            return
        self.cases[i]["applied"] = not self.cases[i].get("applied")
        self.mark_cases()
        self.t_case.refresh(i)

    def commit_case(self, i, key, text):
        c = self.cases[i]
        t = text.strip()
        if key in ("name", "pvm", "svm", "esx"):
            if t and not core.NAME_RE.match(t):
                messagebox.showerror(TITLE, "영문, 숫자, . _ - 만 사용: '%s'" % t, parent=self.root)
                return
            if key == "esx":
                old = c.get("esx", "")
                for k, suf in (("pvm", self.settings.get("pvm_suffix") or "ev01"), ("svm", self.settings.get("svm_suffix") or "ev02")):
                    if not c.get(k) or c.get(k) == old + suf:
                        c[k] = (t + suf) if t else ""
            c[key] = t
        elif key == "interval":
            try:
                iv = core.parse_interval(t)
            except ValueError as e:
                messagebox.showerror(TITLE, str(e), parent=self.root)
                return
            c["interval"] = core.fmt_interval(iv) if iv else ""
            core.recalc_starts(self.cases, i)
        elif key == "start":
            try:
                d = core.parse_dt(t)
            except ValueError as e:
                messagebox.showerror(TITLE, str(e), parent=self.root)
                return
            c["start"] = core.fmt_dt(d)
            core.interval_from_starts(self.cases, i)
            core.recalc_starts(self.cases, i + 1)
        else:
            c[key] = t
        self.mark_cases()
        self.t_case.refresh(i)

    def add_case(self):
        self.t_case.finish_edit()
        i = self.t_case.selected()
        if not self.cases:
            row = {"applied": False, "name": "", "esx": "", "pvm": "", "svm": "", "pvm_job": NONE_JOB, "svm_job": NONE_JOB,
                   "interval": "", "start": next_hour(), "monitors": [], "stop_mode": "auto", "stop_time": ""}
            self.cases.append(row)
            new = 0
        else:
            src = self.cases[len(self.cases) - 1 if i is None else i]
            new = (len(self.cases) - 1 if i is None else i) + 1
            row = copy.deepcopy(src)
            row["name"] = unique_name(src.get("name", ""), set(c.get("name", "") for c in self.cases))
            row["applied"] = False          # 새로 만든 케이스는 아직 등록 전
            if row.get("stop_mode") == "manual":
                row["stop_mode"], row["stop_time"] = "auto", ""
            self.cases.insert(new, row)
            core.recalc_starts(self.cases, new)
        self.mark_cases()
        self.t_case.refresh(new)
        if not row["name"]:
            self.t_case.start_edit(new, 0)

    def del_case(self):
        self.t_case.finish_edit()
        i = self.t_case.selected()
        if i is None:
            return
        name = self.cases[i].get("name") or "%d행" % (i + 1)
        if not messagebox.askyesno(TITLE, "케이스 '%s' 를 삭제할까요?\n다른 케이스의 잡 시작 시각은 바뀌지 않는다." % name,
                                   parent=self.root):
            return
        del self.cases[i]
        if i < len(self.cases):
            core.interval_from_starts(self.cases, i)
        self.mark_cases()
        self.t_case.refresh(i)

    def move_case(self, step):
        """케이스 순서만 바꾼다. 잡 시작, 간격, 모니터링 중지 설정은 그 자리(행)에 남는다."""
        self.t_case.finish_edit()
        i = self.t_case.selected()
        if i is None or not (0 <= i + step < len(self.cases)):
            return
        j = i + step
        a, b = self.cases[i], self.cases[j]
        for k in ("start", "interval", "stop_mode", "stop_time"):
            a[k], b[k] = b.get(k, ""), a.get(k, "")
        self.cases[i], self.cases[j] = b, a
        self.mark_cases()
        self.t_case.refresh(j)

    def edit_monitors(self, i):
        self.t_case.finish_edit()
        c = self.cases[i]
        mons = [m for m in self.mon_cmds if m.get("name")]
        cur = {m["name"]: list(m["targets"]) for m in core.selected_monitors(c)}
        missing = [n for n in cur if n not in set(m["name"] for m in mons)]
        if not mons and not missing:
            messagebox.showinfo(TITLE, "탭 2 에 모니터링 명령을 먼저 등록할 것", parent=self.root)
            return
        top = tk.Toplevel(self.root)
        top.title("모니터링 선택 - %s" % (c.get("name") or "%d행" % (i + 1)))
        top.transient(self.root)
        fr = ttk.Frame(top, padding=self.px(10))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="이 케이스에서 실행할 모니터링과 대상을 고른다 (기본은 모두 선택 안 함).", style="Bold.TLabel").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, self.px(8)))
        vars_ = []
        r = 1
        for m in mons:
            where = (m.get("where") or "VM").upper()
            ttk.Label(fr, text="%s / %s" % (m.get("app") or "-", m["name"])).grid(row=r, column=0, sticky="w", padx=(0, 12))
            ttk.Label(fr, text=where, style="Hint.TLabel").grid(row=r, column=1, sticky="w", padx=(0, 12))
            targets = ["ESX"] if where == "ESX" else ["PVM", "SVM"]
            for k, tname in enumerate(targets):
                v = tk.BooleanVar(value=tname in cur.get(m["name"], []))
                ttk.Checkbutton(fr, text=tname, variable=v).grid(row=r, column=2 + k, sticky="w", padx=(0, 10))
                vars_.append((m["name"], tname, v))
            desc = m.get("desc") or ""
            if desc:
                ttk.Label(fr, text=desc, style="Hint.TLabel").grid(row=r, column=4, sticky="w")
            r += 1
        for n in missing:
            ttk.Label(fr, text="%s (탭 2 에 없음)" % n, style="Hint.TLabel").grid(row=r, column=0, sticky="w")
            for k, tname in enumerate(cur[n]):
                v = tk.BooleanVar(value=True)
                ttk.Checkbutton(fr, text=tname, variable=v).grid(row=r, column=2 + k, sticky="w")
                vars_.append((n, tname, v))
            r += 1
        bf = ttk.Frame(fr)
        bf.grid(row=r, column=0, columnspan=5, sticky="e", pady=(self.px(10), 0))

        def ok():
            sel = {}
            for name, tname, v in vars_:
                if v.get():
                    sel.setdefault(name, []).append(tname)
            order = [m["name"] for m in mons] + missing
            c["monitors"] = [{"name": n, "targets": sel[n]} for n in order if n in sel]
            top.destroy()
            self.mark_cases()
            self.t_case.refresh(i)

        ttk.Button(bf, text="모두 해제", command=lambda: [v.set(False) for _, _, v in vars_]).pack(side="left", padx=(0, 20))
        ttk.Button(bf, text="확인", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left")
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Return>", lambda e: ok())
        self._center(top)
        top.grab_set()

    def edit_stop(self, i):
        self.t_case.finish_edit()
        c = self.cases[i]
        if not core.selected_monitors(c):
            messagebox.showinfo(TITLE, "이 케이스는 모니터링을 고르지 않았음. 먼저 모니터링 칸에서 선택할 것", parent=self.root)
            return
        self.calc_schedule()
        s = self.sched[i]
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        top = tk.Toplevel(self.root)
        top.title("모니터링 중지 - %s" % (c.get("name") or "%d행" % (i + 1)))
        top.transient(self.root)
        fr = ttk.Frame(top, padding=self.px(10))
        fr.pack(fill="both", expand=True)
        mode = tk.StringVar(value=c.get("stop_mode") or "auto")
        auto_txt = "자동: 다음 케이스 잡 시작 %d분 전" % ld[1] if i + 1 < len(self.cases) else \
            "자동: 마지막 케이스이므로 잡 시작 + 간격 - %d분" % ld[1]
        if (c.get("stop_mode") or "auto") == "auto":
            auto_txt += "  (%s)" % (core.fmt_dt(s["mon_stop"]) if s.get("mon_stop") else (s.get("stop_error") or "확인 필요"))
        ttk.Radiobutton(fr, text=auto_txt, value="auto", variable=mode).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(fr, text="직접 입력", value="manual", variable=mode).grid(row=1, column=0, sticky="w")
        guess = c.get("stop_time") or (core.fmt_dt(s["mon_stop"]) if s.get("mon_stop") else "")
        v_time = tk.StringVar(value=guess)
        ent = ttk.Entry(fr, textvariable=v_time, width=22)
        ent.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(fr, text="예: 2026-11-09 21:50:00", style="Hint.TLabel").grid(row=2, column=1, sticky="w", padx=6)
        ttk.Radiobutton(fr, text="중지 없음 (중지 줄을 만들지 않음)", value="none", variable=mode).grid(row=3, column=0, columnspan=2,
                                                                                         sticky="w", pady=(4, 0))
        ent.bind("<FocusIn>", lambda e: mode.set("manual"))
        bf = ttk.Frame(fr)
        bf.grid(row=4, column=0, columnspan=2, sticky="e", pady=(self.px(10), 0))

        def ok():
            m = mode.get()
            if m == "manual":
                try:
                    d = core.parse_dt(v_time.get())
                except ValueError as e:
                    messagebox.showerror(TITLE, str(e), parent=top)
                    return
                c["stop_time"] = core.fmt_dt(d)
            c["stop_mode"] = m
            top.destroy()
            self.mark_cases()
            self.t_case.refresh(i)

        ttk.Button(bf, text="확인", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left")
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Return>", lambda e: ok())
        self._center(top)
        top.grab_set()

    def _center(self, top):
        top.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - top.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - top.winfo_reqheight()) // 3)
        top.geometry("+%d+%d" % (x, y))

    # ------------------------------------------------------------ 생성
    def generate(self):
        self.finish_all_edits()
        if not self.cases:
            messagebox.showinfo(TITLE, "탭 3 에 테스트 케이스를 먼저 추가할 것", parent=self.root)
            return
        s = self.current_settings()
        if s is None:
            return
        paths = core.resolve_paths(self.base, s)
        entries, errors, warnings, _ = core.build_entries(self.cases, self.run_cmds, self.mon_cmds, paths)
        if not errors and not entries:
            if all(c.get("applied") for c in self.cases):
                errors = ["모든 케이스가 적용 체크되어 있어 생성할 케이스가 없음. 새로 등록할 케이스는 적용 체크를 끌 것"]
            else:
                errors = ["등록할 줄이 없음. 적용 체크를 끈 케이스마다 잡이나 모니터링을 하나 이상 지정할 것"]
        if errors:
            self.show_messages("오류 %d건. 블록과 기록을 만들지 않았음. 고친 뒤 다시 생성할 것." % len(errors), errors, warnings)
            self.nb.select(3)
            messagebox.showerror(TITLE, "오류 %d건\n\n%s%s" % (len(errors), "\n".join(errors[:12]),
                                                              "\n..." if len(errors) > 12 else ""), parent=self.root)
            return
        if warnings and not messagebox.askyesno(
                TITLE, "경고 %d건\n\n%s%s\n\n계속 생성할까요? 생성하면 테스트 기록 CSV 에 행이 추가된다."
                % (len(warnings), "\n".join(warnings[:12]), "\n..." if len(warnings) > 12 else ""), parent=self.root):
            return
        try:
            res = core.generate(self.cases, self.run_cmds, self.mon_cmds, paths, write=True)
        except (OSError, ValueError) as e:
            messagebox.showerror(TITLE, "기록 파일을 저장하지 못해 블록을 만들지 않았음.\n%s\n\n"
                                        "테스트 기록 CSV 를 엑셀 등이 열고 있으면 닫고 다시 생성할 것." % e, parent=self.root)
            return
        if res["errors"]:
            self.show_messages("오류", res["errors"], res["warnings"])
            return
        self.result = res
        self.show_result(res)
        self.nb.select(3)

    def show_messages(self, head, errors, warnings):
        self.result = None
        t = self.res_text
        t.delete("1.0", "end")
        t.insert("end", head + "\n", "err" if errors else "ok")
        for e in errors:
            t.insert("end", "오류: " + e + "\n", "err")
        for w in warnings:
            t.insert("end", "경고: " + w + "\n", "warn")
        for blk in (self.blk_setup, self.blk_vm, self.blk_esx):
            set_text(blk, "")
        self.cb_esx.configure(values=[])
        self.v_esx_host.set("")

    def show_result(self, res):
        t = self.res_text
        t.delete("1.0", "end")
        vm_cnt, esx_cnt = {}, {}
        for e in res["entries"]:
            d = esx_cnt if e["esx"] else vm_cnt
            d[e["host"]] = d.get(e["host"], 0) + 1
        t.insert("end", "생성 %s.  테스트 기록 CSV 에 %d행 추가: %s\n" % (core.fmt_dt(res["gen_time"]), res["record_rows"],
                                                                 res["record"]), "ok")
        t.insert("end", "로그: %s\n" % res["log"], "ok")
        made = len(set(e["case"] for e in res["entries"]))
        t.insert("end", "생성한 케이스 %d개, 적용 체크로 뺀 케이스 %d개\n" % (made, res.get("skipped", 0)))
        t.insert("end", "VM %d대: %s\n" % (len(vm_cnt), ", ".join("%s %d줄" % kv for kv in vm_cnt.items()) or "없음"))
        t.insert("end", "ESX %d대: %s\n" % (len(esx_cnt), ", ".join("%s %d줄" % kv for kv in esx_cnt.items()) or "없음"))
        if res["record_missing"]:
            t.insert("end", "기존 테스트 기록 CSV 에 없는 열은 기록하지 않았음 (로그에는 전부 있음): %s\n"
                     % ", ".join(res["record_missing"]), "warn")
        for w in res["warnings"]:
            t.insert("end", "경고: " + w + "\n", "warn")
        set_text(self.blk_setup, res["setup"])
        set_text(self.blk_vm, res["vm"] or "(VM 에 등록할 줄 없음)\n")
        hosts = list(res["esx"].keys())
        self.cb_esx.configure(values=hosts)
        self.v_esx_host.set(hosts[0] if hosts else "")
        self.show_esx_block()

    def show_esx_block(self):
        res = self.result
        h = self.v_esx_host.get()
        if not res or not res["esx"]:
            set_text(self.blk_esx, "(ESX 에 등록할 줄 없음)\n" if res else "")
            return
        set_text(self.blk_esx, res["esx"].get(h, ""))

    def copy_block(self, which):
        res = self.result
        if not res:
            messagebox.showinfo(TITLE, "탭 3 에서 [명령어 생성] 을 먼저 누를 것", parent=self.root)
            return
        if which == "setup":
            text, label = res["setup"], "A. 점프 VM 최초 1회 설정"
        elif which == "vm":
            text, label = res["vm"], "B. VM 등록 블록"
        else:
            h = self.v_esx_host.get()
            text, label = res["esx"].get(h, ""), "C. ESX 블록 %s" % h
        if not text:
            messagebox.showinfo(TITLE, "복사할 내용이 없음", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.v_status.set("복사함: %s (%d줄). 붙여넣을 창에 붙여넣기 (점프 VM: 마우스 오른쪽 또는 Shift+Insert)"
                          % (label, text.count("\n")))

    # ------------------------------------------------------------ 끝내기
    def on_close(self):
        if not self.save_everything():
            failed = [name for name, key in self.SAVE_NAMES if self.dirty[key]]
            if not messagebox.askokcancel(TITLE, "저장하지 못한 항목이 있음: %s\n끝내면 이 변경은 사라진다. 끝낼까요?"
                                          % ", ".join(failed), parent=self.root):
                return
        self.root.destroy()


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
