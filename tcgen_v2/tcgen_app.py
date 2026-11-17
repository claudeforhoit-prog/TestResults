# -*- coding: utf-8 -*-
"""
tcgen_app.py - 테스트 실행 명령 생성기 v2 화면 (tkinter, Python 표준 라이브러리만 사용)

실행: python tcgen_app.py  또는 빌드한 tcgen2.exe
설정, 명령 목록, 설정 프로파일, 테스트 케이스 표는 데이터 폴더(기본 <사용자 폴더>/tcgen2_data)에 저장한다.
진행 방식(설정 영역)이 순서 실행이면 탭 4 에서 시각 칸을 숨기고 탭 5 에 블록 A, P, S, D 와 실행 계획을 보인다.
시각 지정이면 v1.4 와 같은 화면이다(블록 A, B, C, D).
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
MODE_LABELS = {"seq": "순서 실행", "time": "시각 지정"}
SEQ_HIDE = ("interval", "start", "mon_start", "mon_stop", "end_time")      # 순서 실행에서 숨기는 탭 4 열
TIME_HIDE = ("power", "prof", "max_wait", "seq_start")                      # 시각 지정에서 숨기는 탭 4 열

COLORS = dict(bg="#f3f5f8", card="#ffffff", line="#d5dbe3", head="#e8edf4", ink="#1f2937", mut="#6b7280",
              blue="#2a6fd6", blue_d="#1f5bb5", green="#1f9d6b", green_d="#17845a", purple="#7a4fbf", purple_d="#6540a3",
              red="#c0392b", red_d="#a93226", case="#edf3fc", sel="#cfe0fb", stripe="#f8fafc", dark="#2f3a48",
              tab="#e3e8ef", tab_h="#d5dce6", field="#aeb7c2")


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


def poll_minutes(sec):
    """잡 종료 확인 간격(초)을 화면에 보일 분으로 바꾼다. 1분보다 짧은 값은 1 로 보인다."""
    try:
        return max(1, int(round(int(sec) / 60.0)))
    except (TypeError, ValueError):
        return 30


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
    """Treeview 표. 칸을 두 번 누르거나 F2, Enter 로 그 칸을 고친다. Tab 으로 다음 칸.
    rows 의 항목 하나가 표의 행 하나이면(iid = 순번) 그대로 쓴다. 탭 4 처럼 케이스 행과 VM 행이 섞이면
    row_ids, cell_value, cell_kind, row_check, row_tags 를 넘겨 행마다 다르게 다룬다."""

    def __init__(self, master, app, columns, height=8, check=None):
        ttk.Frame.__init__(self, master, style="Card.TFrame")
        self.app = app
        self.columns = columns
        self.check = check           # 맨 앞 체크 열: dict(title, width, on, off, toggle[, key]) 또는 None
        self.rows = []
        self.display = None          # (iid, key) -> 표시 문자열 또는 None
        self.on_commit = None        # (iid, key, text) -> None
        self.before_refresh = None
        self.row_ids = None          # () -> [iid]. 없으면 rows 순번
        self.cell_value = None       # (iid, key) -> 편집 칸에 넣을 값
        self.cell_kind = None        # (iid, column) -> "entry", "combo", "popup", "readonly" 또는 None(열 기본값)
        self.row_check = None        # (iid) -> None(체크 칸 없음), True, False
        self.row_tags = None         # (iid) -> 행 태그 목록
        self.visible = None          # 보이는 열의 key 목록 (None 이면 모든 열)
        self.editor = None
        keys = [c["key"] for c in columns]
        self.tree = ttk.Treeview(self, columns=keys, show=("tree headings" if check else "headings"), height=height,
                                 selectmode="browse")
        if check:
            self.tree.heading("#0", text=check["title"], anchor="center")
            self.tree.column("#0", width=app.px(check["width"]), minwidth=app.px(check["width"]), stretch=False,
                             anchor="center")
        self.tree.tag_configure("applied", foreground="#8a8f98")
        self.tree.tag_configure("odd", background=COLORS["stripe"])
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

    # 보이는 열
    def set_visible(self, keys):
        """보이는 열을 정한다. keys 에 없는 열은 숨긴다. None 이면 모든 열을 보인다."""
        self.finish_edit()
        self.visible = [c["key"] for c in self.columns if c["key"] in keys] if keys is not None else None
        self.tree.configure(displaycolumns=(self.visible if self.visible is not None else "#all"))

    def is_visible(self, ci):
        return self.visible is None or self.columns[ci]["key"] in self.visible

    # 행과 칸
    def ids(self):
        return list(self.row_ids()) if self.row_ids else [str(i) for i in range(len(self.rows))]

    def value(self, iid, key):
        if self.cell_value:
            return self.cell_value(iid, key)
        i = int(iid)
        return self.rows[i].get(key, "") if i < len(self.rows) else ""

    def kind(self, iid, ci):
        c = self.columns[ci]
        k = self.cell_kind(iid, c) if self.cell_kind else None
        return k or c.get("kind", "entry")

    def check_state(self, iid):
        if not self.check:
            return None
        if self.row_check:
            return self.row_check(iid)
        i = int(iid)
        return bool(self.rows[i].get(self.check.get("key", "applied"))) if i < len(self.rows) else None

    # 체크 열
    def _click_check(self, ev):
        try:
            region = self.tree.identify_region(ev.x, ev.y)
        except tk.TclError:
            return None
        iid = self.tree.identify_row(ev.y)
        if region != "tree" or not iid or self.check_state(iid) is None:
            return None
        self.tree.selection_set(iid)
        self.check["toggle"](iid)
        return "break"

    def _space_check(self, ev):
        iid = self.selected()
        if iid is not None and self.check_state(iid) is not None:
            self.check["toggle"](iid)
        return "break"

    # 표시
    def cell_text(self, iid, key):
        if self.display:
            v = self.display(iid, key)
            if v is not None:
                return v
        return self.value(iid, key)

    def refresh(self, select=None):
        if select is None:
            select = self.selected()
        elif not isinstance(select, str):
            select = str(select)
        if self.before_refresh:
            self.before_refresh()
        self.tree.delete(*self.tree.get_children())
        ids = self.ids()
        for iid in ids:
            kw = {}
            st = self.check_state(iid) if self.check else None
            if self.check:
                kw["text"] = ""
                if st is not None:
                    kw["image"] = self.check["on"] if st else self.check["off"]
            if self.row_tags:
                kw["tags"] = tuple(self.row_tags(iid))
            elif st:
                kw["tags"] = ("applied",)
            elif iid.isdigit() and int(iid) % 2 == 1:
                kw["tags"] = ("odd",)
            self.tree.insert("", "end", iid=iid, values=[self.cell_text(iid, c["key"]) for c in self.columns], **kw)
        if not ids or select is None:
            return
        if select not in ids:
            if not select.isdigit():
                return
            select = str(max(0, min(int(select), len(ids) - 1)))
            if select not in ids:
                return
        self.tree.selection_set(select)
        self.tree.focus(select)
        self.tree.see(select)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def selected_index(self):
        s = self.selected()
        return int(s) if (s is not None and s.isdigit()) else None

    # 편집
    def _double(self, ev):
        try:
            region = self.tree.identify_region(ev.x, ev.y)
        except tk.TclError:
            region = "cell"
        iid, col = self.tree.identify_row(ev.y), self.tree.identify_column(ev.x)
        if self.check and region == "tree":
            # 빠른 두 번째 클릭은 Tk 가 더블클릭으로 처리하므로 여기서도 체크를 바꾼다(누를 때마다 바뀜)
            if iid and self.check_state(iid) is not None:
                self.check["toggle"](iid)
            return "break"
        if region != "cell" or not iid or not col:
            return None
        n = int(col.lstrip("#")) - 1
        if self.visible is not None:          # 숨긴 열이 있으면 화면의 열 순번과 columns 순번이 다르다
            if not 0 <= n < len(self.visible):
                return None
            n = [c["key"] for c in self.columns].index(self.visible[n])
        self.start_edit(iid, n)
        return "break"

    def _key_edit(self, ev):
        iid = self.selected()
        if iid is None:
            return None
        for ci in range(len(self.columns)):
            if self.is_visible(ci) and self.kind(iid, ci) in ("entry", "combo"):
                self.start_edit(iid, ci)
                break
        return "break"

    def start_edit(self, iid, ci):
        self.finish_edit()
        iid = str(iid)
        if not self.tree.exists(iid):
            return
        c = self.columns[ci]
        kind = self.kind(iid, ci)
        if kind == "readonly":
            return
        if kind == "popup":
            c["popup"](iid)
            return
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.tree.update_idletasks()
        bbox = self.tree.bbox(iid, c["key"])
        if not bbox:
            return
        x, y, w, h = bbox
        var = tk.StringVar(value=self.value(iid, c["key"]))
        if kind == "combo":
            ed = ttk.Combobox(self.tree, textvariable=var, values=list(c["values"]()), state="readonly",
                              style="Cell.TCombobox")
            ed.bind("<<ComboboxSelected>>", lambda e, w=ed: self.tree.after_idle(lambda: self._finish_if(w, True)))
        else:
            ed = ttk.Entry(self.tree, textvariable=var, style="Cell.TEntry")
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
        ed.bind("<Tab>", lambda e: self._tab(iid, ci, 1))
        for seq in ("<Shift-Tab>", "<ISO_Left_Tab>"):
            try:
                ed.bind(seq, lambda e: self._tab(iid, ci, -1))
            except tk.TclError:
                pass
        self.editor = (ed, var, iid, c["key"])
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

    def _tab(self, iid, ci, step):
        self.finish_edit(refocus=True)
        n = ci + step
        while 0 <= n < len(self.columns):
            if self.tree.exists(iid) and self.is_visible(n) and self.kind(iid, n) in ("entry", "combo"):
                self.start_edit(iid, n)
                break
            n += step
        return "break"

    def finish_edit(self, commit=True, refocus=False):
        if not self.editor:
            return
        ed, var, iid, key = self.editor
        self.editor = None
        text = var.get()
        try:
            ed.destroy()
        except tk.TclError:
            pass
        if commit and text != str(self.value(iid, key)):
            if self.on_commit:
                self.on_commit(iid, key, text)
            elif iid.isdigit() and int(iid) < len(self.rows):
                self.rows[int(iid)][key] = text
                self.refresh(iid)
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
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.prog_dir = core.app_dir()          # 생성기 폴더 (실행 파일만, 다시 빌드하면 지워질 수 있음)
        self.start_msgs = []
        self.base = self.open_data_dir_at_start()
        self.loading = False
        self.load_msg = ""
        self.run_cmds, self.mon_cmds, self.cases, self.profiles = [], [], [], []
        self.read_data()
        if self.upgrade_msg:
            self.start_msgs.append(self.upgrade_msg)
        paths = core.resolve_paths(self.base, self.settings)
        self.sched = []
        self.dirty = {"settings": False, "run": False, "mon": False, "profiles": False, "cases": False}   # True = 자동 저장 실패
        self.save_warned = {}
        self.last_saved = None
        self.result = None
        self.setup_style()
        self.v_record = tk.StringVar(value=paths["record_csv"])
        self.v_log = tk.StringVar(value=paths["log_dir"])
        self.v_user = tk.StringVar(value=self.settings.get("vm_user", ""))
        self.v_mon = tk.StringVar(value=str(self.settings["mon_lead_min"]))
        self.v_stop = tk.StringVar(value=str(self.settings["stop_lead_min"]))
        self.v_mode = tk.StringVar(value=MODE_LABELS[self.settings["run_mode"]])
        self.v_jlog = tk.StringVar(value=self.settings.get("jump_log_dir", ""))
        self.v_addr = tk.StringVar(value=self.settings.get("esx_addr", ""))
        self.v_jdir = tk.StringVar(value=self.settings.get("jump_dir", ""))     # 순서 실행: 점프 VM 작업 폴더
        self.v_settle = tk.StringVar(value=str(self.settings["seq_settle_min"]))
        self.v_gap = tk.StringVar(value=str(self.settings["seq_gap_min"]))      # 순서 실행: 케이스 사이 간격(분)
        self.v_poll = tk.StringVar(value=str(poll_minutes(self.settings["seq_poll_sec"])))   # 순서 실행: 잡 종료 확인 간격(분)
        self.v_spacing = tk.StringVar(value=str(self.settings["seq_start_spacing_min"]))   # 순서 실행: 잡 시작 최소 간격(분)
        self.v_prep = tk.StringVar(value=str(self.settings["seq_prep_lead_min"]))          # 순서 실행: 시작 시각 전 준비(분)
        self.v_snapgap = tk.StringVar(value=str(self.settings["seq_snap_gap_min"]))        # 순서 실행: 스냅샷 뒤 잡 시작까지 최소(분)
        self.v_gcmd = tk.StringVar(value=self.settings.get("seq_guest_cmd", ""))           # 순서 실행: 스냅샷 때 VM 에서 실행할 명령
        self.v_cfile = tk.StringVar()           # 지금 쓰는 테스트 케이스 파일 (보여 주기만 한다)
        self.v_status = tk.StringVar()
        self.v_esx_host = tk.StringVar()
        self.build()
        self.apply_mode()
        for v in (self.v_record, self.v_log, self.v_user, self.v_mon, self.v_stop, self.v_jlog, self.v_addr, self.v_settle, self.v_gap,
                  self.v_poll, self.v_spacing, self.v_prep, self.v_jdir, self.v_snapgap, self.v_gcmd):
            v.trace_add("write", self._settings_changed) if hasattr(v, "trace_add") else v.trace("w", self._settings_changed)
        self.update_status()
        for m in self.start_msgs:
            messagebox.showinfo(TITLE, m, parent=root)
        if self.load_msg:
            self.v_status.set(self.load_msg)
            messagebox.showwarning(TITLE, self.load_msg, parent=root)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.auto_backup_tick()

    # ------------------------------------------------------------ 데이터 폴더
    def open_data_dir_at_start(self):
        """데이터 폴더를 정하고(config.json 또는 기본 위치), 예전 판이 생성기 폴더에 둔 데이터가 있으면 복사해 온다."""
        base = core.data_dir()
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as e:
            fallback = os.path.normpath(core.default_data_dir())
            self.start_msgs.append("데이터 폴더를 쓸 수 없어 기본 위치를 씀\n쓸 수 없는 폴더: %s\n%s\n기본 위치: %s"
                                   % (base, e, fallback))
            base = fallback
            os.makedirs(base, exist_ok=True)
        try:
            moved = core.migrate_from(self.prog_dir, base)
        except OSError as e:
            moved = []
            self.start_msgs.append("예전 생성기 폴더의 데이터를 데이터 폴더로 복사하지 못함\n%s\n%s" % (self.prog_dir, e))
        if moved:
            self.start_msgs.append("예전 판이 생성기 폴더에 저장한 데이터를 데이터 폴더로 복사함.\n\n%s\n\n생성기 폴더: %s\n데이터 폴더: %s\n\n"
                                   "이제 설정, 명령, 케이스는 데이터 폴더에 저장되어 생성기를 다시 빌드해도 남는다."
                                   % ("\n".join(moved), self.prog_dir, base))
        try:
            got, v1 = core.import_from_v1(base)      # v2 를 처음 실행: v1 의 데이터를 복사해 온다(v1 폴더는 읽기만 함)
        except OSError as e:
            got, v1 = [], core.v1_data_dir()
            self.start_msgs.append("v1 의 데이터를 복사해 오지 못함\n%s\n%s" % (v1, e))
        if got:
            self.start_msgs.append("v2 를 처음 실행해서 v1 의 데이터를 복사해 왔다.\n\n%s\n\nv1 데이터 폴더: %s\nv2 데이터 폴더: %s\n\n"
                                   "v1 폴더의 파일은 그대로 두었다. v2 에서 고친 내용은 v2 폴더에만 저장되므로 v1.4 를 다시 실행하면 "
                                   "v1 의 데이터가 그대로 보인다." % ("\n".join(got), v1, base))
        return base

    def read_data(self):
        """데이터 폴더에서 설정, 실행 명령, 모니터링 명령, 테스트 케이스 표를 읽는다. 목록 객체는 그대로 두고 내용만 바꾼다.
        테스트 케이스 표가 예전 형식(PVM, SVM 칸)이면 원본을 backup 에 복사한 뒤 VM 행 형식으로 바꾼다."""
        self.upgrade_msg = ""
        try:
            up = core.upgrade_cases_file(self.base)
        except (OSError, ValueError) as e:
            up = None
            self.upgrade_msg = "테스트 케이스 표를 새 형식으로 바꾸지 못함 (원래 파일은 그대로): %s" % e
        if up:
            self.upgrade_msg = ("테스트 케이스 표를 VM 행 형식으로 바꿨다. 예전 PVM, SVM 은 VM 2대로 옮겼다.\n\n"
                                "바꾸기 전 파일: %s" % os.path.join(up, core.CASES_FILE))
        self.settings = core.load_settings(self.base)
        self.run_cmds[:] = core.load_rows(os.path.join(self.base, core.RUN_FILE), core.RUN_FIELDS, core.RUN_HEADERS)
        self.mon_cmds[:] = core.load_rows(os.path.join(self.base, core.MON_FILE), core.MON_FIELDS, core.MON_HEADERS)
        self.load_msg = self.load_case_table()
        try:
            self.profiles[:] = core.load_profiles(self.base, seed=True)     # 파일이 없으면(처음 실행) 기본 프로파일로 채움
        except (OSError, ValueError) as e:
            self.profiles[:] = []
            bad = os.path.join(self.base, core.PROFILES_FILE)
            keep = bad + ".bad-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                os.replace(bad, keep)
            except OSError:
                keep = bad
            self.load_msg = (self.load_msg + "\n" if self.load_msg else "") + \
                "%s 를 읽지 못해 빈 표로 시작함 (원래 파일: %s) %s" % (core.PROFILES_FILE, keep, e)

    def load_case_table(self):
        """설정에 적힌 테스트 케이스 파일의 표를 self.cases 에 읽는다. 알릴 글(없으면 빈 문자열)을 반환한다.
        사용자가 정한 파일이 없어졌거나 읽을 수 없으면 그 파일은 그대로 두고 기본 파일(데이터 폴더의 test_cases.json)로 바꾼다.
        기본 파일을 읽지 못하면 그 파일을 옆에 두고 빈 표로 시작한다."""
        msg = ""
        cf = core.case_file(self.settings)
        if cf:
            try:
                if not os.path.isfile(cf):
                    raise ValueError("파일이 없음")
                self.cases[:] = core.load_cases(self.base, cf)
                return ""
            except (OSError, ValueError) as e:
                msg = ("지정한 테스트 케이스 파일을 읽지 못해 기본 파일로 바꿈.\n%s\n%s\n\n그 파일은 그대로 두었다. "
                       "다시 쓰려면 [불러오기] 에서 고른다." % (cf, e))
                self.settings = core.switch_case_file(self.base, self.settings, "")
                try:
                    core.save_settings(self.base, core.resolve_paths(self.base, self.settings))
                except OSError:
                    pass
        try:
            self.cases[:] = core.load_cases(self.base)
        except (OSError, ValueError) as e:
            self.cases[:] = []
            bad = core.cases_path(self.base)
            keep = bad + ".bad-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                os.replace(bad, keep)
            except OSError:
                keep = bad
            msg = (msg + "\n\n" if msg else "") + "%s 를 읽지 못해 빈 표로 시작함 (원래 파일: %s) %s" % (core.CASES_FILE, keep, e)
        return msg

    def reload_data(self):
        """데이터 폴더를 바꾸거나 백업을 불러온 뒤 화면 전체를 다시 채운다."""
        self.finish_all_edits()
        self.load_msg = ""
        self.loading = True
        try:
            self.read_data()
            paths = core.resolve_paths(self.base, self.settings)
            self.v_record.set(paths["record_csv"])
            self.v_log.set(paths["log_dir"])
            self.v_user.set(self.settings.get("vm_user", ""))
            self.v_mon.set(str(self.settings["mon_lead_min"]))
            self.v_stop.set(str(self.settings["stop_lead_min"]))
            self.v_mode.set(MODE_LABELS[self.settings["run_mode"]])
            self.v_jlog.set(self.settings.get("jump_log_dir", ""))
            self.v_addr.set(self.settings.get("esx_addr", ""))
            self.v_jdir.set(self.settings.get("jump_dir", ""))
            self.v_settle.set(str(self.settings["seq_settle_min"]))
            self.v_gap.set(str(self.settings["seq_gap_min"]))
            self.v_poll.set(str(poll_minutes(self.settings["seq_poll_sec"])))
            self.v_spacing.set(str(self.settings["seq_start_spacing_min"]))
            self.v_prep.set(str(self.settings["seq_prep_lead_min"]))
            self.v_snapgap.set(str(self.settings["seq_snap_gap_min"]))
            self.v_gcmd.set(self.settings.get("seq_guest_cmd", ""))
            self.refresh_case_file_box()
        finally:
            self.loading = False
        self.dirty = dict((k, False) for k in self.dirty)
        self.save_warned = {}
        self.apply_mode()
        self.t_run.refresh(0)
        self.t_mon.refresh(0)
        self.t_prof.refresh(0)
        self.t_case.refresh(0)
        self.show_messages("데이터를 다시 읽음. [명령어 생성] 을 누르면 결과가 나온다.", [], [])
        self.update_status()
        if self.upgrade_msg:
            messagebox.showinfo(TITLE, self.upgrade_msg, parent=self.root)
        if self.load_msg:
            messagebox.showwarning(TITLE, self.load_msg, parent=self.root)

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
        raw = int(base.cget("size")) or 9          # 양수 = 포인트, 음수 = 픽셀

        def sized(delta, weight="normal"):
            f = base.copy()
            n = abs(raw) + (delta if raw > 0 else int(round(delta * 4 / 3.0)))
            f.configure(size=(n if raw > 0 else -n), weight=weight)
            return f

        self.bold = sized(0, "bold")
        self.small = sized(-1) if abs(raw) > 8 else sized(0)
        self.tab_font = sized(1)
        self.tab_font_b = sized(1, "bold")
        self.title_font = sized(4, "bold")
        self.mono_font = tkfont.Font(family=self.mono, size=(max(9, raw) if raw > 0 else min(-12, raw)))
        C = COLORS
        px = self.px
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")       # Windows 기본 테마는 버튼, 탭 색을 바꿀 수 없어 색을 지정할 수 있는 테마를 쓴다
        except tk.TclError:
            pass
        self.root.configure(bg=C["bg"])
        style.configure(".", background=C["bg"], foreground=C["ink"], bordercolor=C["line"], focuscolor=C["blue"])
        style.configure("TFrame", background=C["card"])
        style.configure("Card.TFrame", background=C["card"])
        style.configure("Bg.TFrame", background=C["bg"])
        style.configure("TLabel", background=C["card"], foreground=C["ink"])
        style.configure("Hint.TLabel", background=C["card"], foreground=C["mut"], font=self.small)
        style.configure("Title.TLabel", background=C["card"], foreground=C["blue"], font=self.bold)
        style.configure("Bold.TLabel", background=C["card"], font=self.bold)
        style.configure("Ok.TLabel", background=C["card"], foreground="#1e7b34", font=self.bold)
        for w in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(w, fieldbackground="white", bordercolor=C["field"], lightcolor="white", darkcolor="white",
                            padding=(px(4), px(3)))
            style.map(w, bordercolor=[("focus", C["blue"])], lightcolor=[("focus", C["blue"])])
        style.map("TCombobox", fieldbackground=[("readonly", "white")], foreground=[("readonly", C["ink"])],
                  selectbackground=[("readonly", C["sel"])], selectforeground=[("readonly", C["ink"])])
        style.configure("TSpinbox", arrowsize=px(11))
        style.configure("Cell.TEntry", padding=(px(3), 0))          # 표 칸 안 편집기: 행 높이 안에 글자가 들어가게
        style.configure("Cell.TCombobox", padding=(px(3), 0))
        style.configure("TButton", background="white", foreground=C["ink"], bordercolor=C["field"], lightcolor="white",
                        darkcolor="white", padding=(px(10), px(4)), width=-8)
        style.map("TButton", background=[("pressed", "#e2e8f0"), ("active", "#eef2f7")],
                  bordercolor=[("active", "#8a96a5")], lightcolor=[("pressed", "#e2e8f0")],
                  darkcolor=[("pressed", "#e2e8f0")])
        for name, c, d in (("Green", C["green"], C["green_d"]), ("Purple", C["purple"], C["purple_d"]),
                           ("Red", C["red"], C["red_d"]), ("Blue", C["blue"], C["blue_d"])):
            style.configure(name + ".TButton", background=c, foreground="white", bordercolor=c, lightcolor=c, darkcolor=c,
                            font=self.bold, padding=(px(12), px(4)), focuscolor="white")
            style.map(name + ".TButton", background=[("pressed", d), ("active", d)], bordercolor=[("active", d)],
                      lightcolor=[("active", d)], darkcolor=[("active", d)], foreground=[("disabled", "#e5e7eb")])
        for w in ("TCheckbutton", "TRadiobutton"):
            style.configure(w, background=C["card"])
            style.map(w, background=[("active", C["card"])])
        style.configure("TNotebook", background=C["bg"], bordercolor=C["line"], lightcolor=C["card"],
                        darkcolor=C["card"], tabmargins=(px(2), px(4), px(2), 0))
        style.configure("TNotebook.Tab", background=C["tab"], foreground="#374151", padding=(px(20), px(7)),
                        font=self.tab_font, bordercolor="#c7cfd9", lightcolor=C["tab"], focuscolor="white")
        style.map("TNotebook.Tab", background=[("selected", C["blue"]), ("active", C["tab_h"])],
                  foreground=[("selected", "white")], lightcolor=[("selected", C["blue"])],
                  bordercolor=[("selected", C["blue"])], padding=[("selected", (px(20), px(7)))],
                  expand=[("selected", (1, 2, 1, 0))])
        try:
            style.map("TNotebook.Tab", font=[("selected", self.tab_font_b)])
        except tk.TclError:
            pass
        line = base.metrics("linespace")
        style.configure("Treeview", background="white", fieldbackground="white", foreground=C["ink"],
                        bordercolor=C["line"], lightcolor="white", rowheight=int(line * 1.6))
        style.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["ink"])])
        style.configure("Treeview.Heading", background=C["head"], foreground="#334155", font=self.bold, relief="flat",
                        bordercolor=C["line"], lightcolor=C["head"], padding=(px(4), px(5)))
        style.map("Treeview.Heading", background=[("active", "#dde4ee")])
        style.configure("TLabelframe", background=C["card"], bordercolor=C["line"], lightcolor=C["card"],
                        darkcolor=C["card"])
        style.configure("TLabelframe.Label", background=C["card"], foreground=C["blue"], font=self.bold)
        style.configure("TPanedwindow", background=C["card"])
        for w in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(w, background="#dfe4ea", troughcolor=C["card"], bordercolor=C["card"], lightcolor="#dfe4ea",
                            darkcolor="#dfe4ea", arrowcolor=C["mut"])
        self.img_on, self.img_off = make_check_images(self.root, self.scale)

    def card(self, parent, **pack):
        """흰 바탕에 옅은 테두리가 있는 영역."""
        outer = tk.Frame(parent, bg=COLORS["card"], highlightthickness=1, highlightbackground=COLORS["line"])
        outer.pack(**pack)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(self.px(12), self.px(8)))
        inner.pack(fill="both", expand=True)
        return inner

    def text_box(self, parent, **kw):
        opts = dict(relief="flat", bd=0, highlightthickness=1, highlightbackground=COLORS["line"],
                    highlightcolor=COLORS["blue"], bg="white", fg=COLORS["ink"], padx=self.px(6), pady=self.px(4))
        opts.update(kw)
        return tk.Text(parent, **opts)

    def hint(self, parent, text):
        lb = ttk.Label(parent, text=text, style="Hint.TLabel", justify="left", wraplength=self.px(280))
        lb.bind("<Configure>", lambda e: lb.configure(wraplength=max(200, e.width - 10)))
        return lb

    # ------------------------------------------------------------ 배치
    def build(self):
        r = self.root
        r.title(TITLE)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(self.px(1600), int(sw * 0.95)), min(self.px(900), int(sh * 0.9))
        r.geometry("%dx%d" % (w, h))
        r.minsize(min(self.px(1000), w), min(self.px(640), h))
        C, px = COLORS, self.px
        head = tk.Frame(r, bg=C["dark"])
        head.pack(fill="x")
        tk.Label(head, text="테스트 실행 명령 생성기", font=self.title_font, bg=C["dark"], fg="white").pack(
            side="left", padx=(px(14), px(8)), pady=px(6))
        tk.Label(head, text="v" + core.VERSION, font=self.bold, bg=C["dark"], fg="#c9d3df").pack(side="left")
        bar = tk.Frame(r, bg=C["head"])
        bar.pack(side="bottom", fill="x")
        tk.Label(bar, textvariable=self.v_status, font=self.small, bg=C["head"], fg="#374151", anchor="w").pack(
            side="left", fill="x", expand=True, padx=px(12), pady=px(4))
        outer = ttk.Frame(r, style="Bg.TFrame", padding=(px(10), px(8), px(10), px(8)))
        outer.pack(fill="both", expand=True)

        sf = self.card(outer, fill="x")
        row0 = ttk.Frame(sf)
        row0.pack(fill="x")
        ttk.Label(row0, text="설정", style="Title.TLabel").pack(side="left")
        ttk.Label(row0, text="바뀌면 바로 데이터 폴더에 저장된다. 데이터 폴더는 생성기를 다시 빌드해도 남는다.",
                  style="Hint.TLabel").pack(side="left", padx=px(10))
        row_cf = ttk.Frame(sf)                   # 테스트 케이스 파일: 탭 4 의 표를 저장하는 파일
        row_cf.pack(fill="x", pady=(px(6), 0))
        ttk.Label(row_cf, text="테스트 케이스 파일").grid(row=0, column=0, sticky="w")
        ttk.Entry(row_cf, textvariable=self.v_cfile, state="readonly").grid(row=0, column=1, sticky="ew", padx=(px(6), px(4)))
        ttk.Button(row_cf, text="불러오기", command=self.load_case_file).grid(row=0, column=2, padx=(0, px(4)))
        ttk.Button(row_cf, text="다른 이름으로 저장", command=self.save_case_file_as).grid(row=0, column=3, padx=(0, px(10)))
        ttk.Label(row_cf, text="탭 4 의 표를 이 파일에 자동 저장한다. 탭 1, 2, 3 은 어느 파일을 쓰든 같이 쓴다",
                  style="Hint.TLabel").grid(row=0, column=4, sticky="w")
        row_cf.columnconfigure(1, weight=1)
        self.refresh_case_file_box()
        row1 = ttk.Frame(sf)
        row1.pack(fill="x", pady=(px(6), 0))
        ttk.Label(row1, text="테스트 기록 CSV").grid(row=0, column=0, sticky="w")
        ttk.Entry(row1, textvariable=self.v_record, width=30).grid(row=0, column=1, sticky="ew", padx=(px(6), px(4)))
        ttk.Button(row1, text="찾기", command=self.pick_record, width=6).grid(row=0, column=2, padx=(0, px(16)))
        ttk.Label(row1, text="로그 폴더").grid(row=0, column=3, sticky="w")
        ttk.Entry(row1, textvariable=self.v_log, width=24).grid(row=0, column=4, sticky="ew", padx=(px(6), px(4)))
        ttk.Button(row1, text="찾기", command=self.pick_log, width=6).grid(row=0, column=5)
        row1.columnconfigure(1, weight=3)
        row1.columnconfigure(4, weight=2)
        row2 = ttk.Frame(sf)
        row2.pack(fill="x", pady=(px(6), 0))
        ttk.Label(row2, text="진행 방식").pack(side="left")
        self.cb_mode = ttk.Combobox(row2, textvariable=self.v_mode, state="readonly", width=9,
                                    values=[MODE_LABELS["seq"], MODE_LABELS["time"]])
        self.cb_mode.pack(side="left", padx=(px(6), px(18)))
        self.cb_mode.bind("<<ComboboxSelected>>", lambda e: self.mode_changed())
        ttk.Label(row2, text="VM 접속 계정").pack(side="left")
        ttk.Entry(row2, textvariable=self.v_user, width=14).pack(side="left", padx=(px(6), px(4)))
        ttk.Label(row2, text="비우면 점프 VM 계정", style="Hint.TLabel").pack(side="left", padx=(0, px(18)))
        ttk.Label(row2, text="모니터링 시작").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=240, width=4, textvariable=self.v_mon).pack(side="left", padx=px(6))
        self.f_time = ttk.Frame(row2)            # 시각 지정에서만 보이는 칸
        ttk.Label(self.f_time, text="분 전 (잡 시작 기준)").pack(side="left", padx=(0, px(18)))
        ttk.Label(self.f_time, text="모니터링 중지").pack(side="left")
        ttk.Spinbox(self.f_time, from_=0, to=240, width=4, textvariable=self.v_stop).pack(side="left", padx=px(6))
        ttk.Label(self.f_time, text="분 전 (다음 케이스 잡 시작 기준)").pack(side="left")
        self.f_seq = ttk.Frame(row2)             # 순서 실행에서만 보이는 칸
        ttk.Label(self.f_seq, text="분 뒤 잡 시작").pack(side="left", padx=(0, px(18)))
        ttk.Label(self.f_seq, text="변경 작업 뒤 잡 시작까지 최소").pack(side="left")
        ttk.Spinbox(self.f_seq, from_=0, to=240, width=4, textvariable=self.v_settle).pack(side="left", padx=px(6))
        ttk.Label(self.f_seq, text="분").pack(side="left")
        # 순서 실행에서만 보이는 줄: 잡 시작 최소 간격, 준비, 그리고 시작 시각이 '자동' 인 케이스에만 쓰는 두 값(케이스 사이 간격, 잡 종료
        # 확인 간격). 시작 시각을 적는 것이 기본이므로 자동에만 쓰는 값은 뒤에 두고 이름에 (자동) 을 붙인다
        self.row_seq2 = ttk.Frame(sf)
        for label, var, low, unit in (("잡 시작 최소 간격", self.v_spacing, 1, "분"), ("시작 시각 준비", self.v_prep, 0, "분 전"),
                                      ("스냅샷 뒤 잡 시작까지 최소", self.v_snapgap, 0, "분"),
                                      ("케이스 사이 간격(자동)", self.v_gap, 0, "분"), ("잡 종료 확인 간격(자동)", self.v_poll, 1, "분")):
            ttk.Label(self.row_seq2, text=label).pack(side="left")
            ttk.Spinbox(self.row_seq2, from_=low, to=1440, width=5, textvariable=var).pack(side="left", padx=px(6))
            ttk.Label(self.row_seq2, text=unit).pack(side="left", padx=(0, px(18)))
        ttk.Label(self.row_seq2, text="뜻은 탭 4 의 [도움말]", style="Hint.TLabel").pack(side="left")
        self.row_seq = ttk.Frame(sf)             # 순서 실행에서만 보이는 줄
        ttk.Label(self.row_seq, text="점프 VM 로그 폴더").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.row_seq, textvariable=self.v_jlog, width=22).grid(row=0, column=1, sticky="ew", padx=(px(6), px(4)))
        ttk.Label(self.row_seq, text="비우면 ~/tcgen_log (작업 폴더를 적었으면 <작업 폴더>/log)",
                  style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=(0, px(18)))
        ttk.Label(self.row_seq, text="ESX 접속 주소").grid(row=0, column=3, sticky="w")
        ttk.Entry(self.row_seq, textvariable=self.v_addr, width=28).grid(row=0, column=4, sticky="ew", padx=(px(6), px(4)))
        ttk.Label(self.row_seq, text="호스트이름=주소 를 ; 로 구분. 비우면 ESX 호스트 이름으로 접속",
                  style="Hint.TLabel").grid(row=0, column=5, sticky="w")
        ttk.Label(self.row_seq, text="점프 VM 작업 폴더").grid(row=1, column=0, sticky="w", pady=(px(6), 0))
        ttk.Entry(self.row_seq, textvariable=self.v_jdir, width=22).grid(row=1, column=1, sticky="ew", padx=(px(6), px(4)),
                                                                        pady=(px(6), 0))
        ttk.Label(self.row_seq, text="비우면 ~/.tcgen. 진행 스크립트, ssh 키, VM 비밀번호, 실행 기록을 둔다. 홈 폴더 용량이 작으면 "
                                     "큰 디스크의 폴더를 적고 A 블록을 다시 붙여넣는다(키와 비밀번호는 ~/.tcgen 에서 복사)",
                  style="Hint.TLabel").grid(row=1, column=2, columnspan=4, sticky="w", pady=(px(6), 0))
        # 구성 스냅샷 때 케이스의 켜져 있는 VM 마다 실행할 명령: 게스트 구성을 VCF Operations 에 기록하는 스크립트를 적는다
        ttk.Label(self.row_seq, text="스냅샷 때 VM 에서 실행할 명령").grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        ttk.Entry(self.row_seq, textvariable=self.v_gcmd, width=22).grid(row=2, column=1, columnspan=2, sticky="ew",
                                                                        padx=(px(6), px(4)), pady=(px(6), 0))
        ttk.Label(self.row_seq, text="비우면 실행하지 않음. 게스트 구성을 VCF Operations 에 기록하는 스크립트를 VM 안의 경로로 적는다. "
                                     "예: bash /home/tester/perf/guest_config_push.sh",
                  style="Hint.TLabel").grid(row=2, column=3, columnspan=3, sticky="w", pady=(px(6), 0))
        self.row_seq.columnconfigure(1, weight=2)
        self.row_seq.columnconfigure(4, weight=3)
        row3 = ttk.Frame(sf)
        self.row3 = row3
        row3.pack(fill="x", pady=(px(8), px(2)))
        for text, cmd in (("설정 백업", self.backup), ("백업 불러오기", self.restore_backup),
                          ("데이터 폴더 열기", self.open_data_dir), ("데이터 폴더 변경", self.change_data_dir),
                          ("테스트 케이스 폴더 열기", self.open_case_dir)):
            ttk.Button(row3, text=text, command=cmd).pack(side="left", padx=(0, px(6)))

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(px(10), 0))
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.finish_all_edits())
        self.build_run_tab()
        self.build_mon_tab()
        self.build_prof_tab()
        self.build_case_tab()
        self.build_result_tab()

    def tab_page(self, text):
        """탭 하나: 탭 바로 아래 파란 줄과 흰 바탕 내용 영역."""
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=text)
        tk.Frame(f, bg=COLORS["blue"], height=self.px(3)).pack(fill="x")
        body = ttk.Frame(f, padding=(self.px(10), self.px(8)))
        body.pack(fill="both", expand=True)
        return body

    def toolbar(self, parent):
        tb = ttk.Frame(parent)
        tb.pack(fill="x", pady=(0, self.px(8)))
        return tb

    def help_button(self, bar, title, lines):
        ttk.Button(bar, text="도움말", command=lambda: self.show_help(title, lines)).pack(side="right", padx=(self.px(6), 0))

    def show_help(self, title, lines):
        """[도움말] 창. lines 는 문장 목록 또는 문장 목록을 돌려주는 함수."""
        if callable(lines):
            lines = lines()
        old = getattr(self, "help_win", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        C, px = COLORS, self.px
        top = tk.Toplevel(self.root, bg=C["card"])
        self.help_win = top
        top.title("도움말 - " + title)
        top.transient(self.root)
        hd = tk.Frame(top, bg=C["dark"])
        hd.pack(fill="x")
        tk.Label(hd, text="도움말 - " + title, font=self.bold, bg=C["dark"], fg="white").pack(
            side="left", padx=px(12), pady=px(6))
        body = ttk.Frame(top, padding=(px(14), px(10), px(14), px(4)))
        body.pack(fill="both", expand=True)
        for i, line in enumerate(lines):
            ttk.Label(body, text="•").grid(row=i, column=0, sticky="nw", padx=(0, px(6)), pady=px(2))
            ttk.Label(body, text=line, wraplength=px(640), justify="left").grid(row=i, column=1, sticky="w", pady=px(2))
        bf = ttk.Frame(top, padding=(px(14), px(6), px(14), px(12)))
        bf.pack(fill="x")
        ttk.Button(bf, text="닫기", command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        self._center(top)
        top.focus_set()

    RUN_HELP = [
        "칸을 두 번 누르면(또는 F2, Enter) 그 칸을 고친다. 고치는 중에 Tab 을 누르면 다음 칸으로 간다.",
        "고칠 때마다 데이터 폴더의 run_commands.csv 에 자동 저장되고, 다음 실행 때 불러온다.",
        "명령어 명은 영문, 숫자, . _ - 만 쓴다. 탭 4 의 잡 칸에서 이 이름을 고른다. 이름을 바꾸면 그 이름을 쓰던 케이스도 바뀐다.",
        "프로세스 이름(순서 실행에서 필요): 잡이 실행되는 동안 VM 에 떠 있는 프로세스의 명령줄에 들어 있는 글자를 적는다. "
        "점프 VM 이 각 VM 에서 pgrep -f 로 이 글자를 찾아, 한 번 이상 보인 뒤 더 보이지 않으면 잡이 끝난 것으로 판정한다. "
        "잡의 처음부터 끝까지 떠 있는 프로세스(예: 실행 스크립트 이름)를 적는다. 작은따옴표, !, 역슬래시, 한글은 쓸 수 없다.",
        "프로세스 이름이 다른 프로세스(모니터링 명령, 편집기로 연 로그 파일 등)의 명령줄에도 들어 있으면 잡이 끝나지 않은 것으로 "
        "판정된다. 그 잡에서만 쓰는 글자를 적는다.",
        "명령은 절대 경로로 입력한다. crontab 에 한 줄로 그대로 들어가고, cron 은 홈 폴더에서 실행한다.",
        "변수: <vm이름> = VM 게스트 hostname, <실행시각> = 실행 시각 14자리 (예: 20261109000000)",
        "예: /home/tester/bench/run_a.sh >> /home/tester/log/run_a-<vm이름>-<실행시각>.log 2>&1",
    ]
    MON_HELP = [
        "어떤 모니터링 명령이든 직접 등록한다. 고칠 때마다 데이터 폴더의 monitor_commands.csv 에 자동 저장되고, 다음 실행 때 불러온다.",
        "실행 위치가 VM 이면 그 VM 의 crontab 에, ESX 이면 ESX 호스트의 crontab 에 등록한다.",
        "탭 4 에서 VM 행의 모니터링 칸은 실행 위치가 VM 인 명령을, 케이스 행의 모니터링 칸은 ESX 인 명령을 고른다.",
        "중지 명령을 비우면 중지 줄을 만들지 않는다.",
        "변수는 탭 1 과 같다. ESX 에서 실행하는 명령의 <vm이름> 은 ESX 호스트 이름이다.",
    ]
    PROF_HELP = [
        "설정 프로파일은 순서 실행에서 케이스를 시작하기 전에 적용할 설정 묶음이다. 탭 4 의 설정 칸에서 고른다. "
        "고칠 때마다 데이터 폴더의 profiles.json 에 자동 저장된다.",
        "대상이 ESX 호스트인 프로파일은 케이스 행의 설정 칸에서, 대상이 VM 인 프로파일은 VM 행의 설정 칸에서 고른다.",
        "설정 내용 칸을 두 번 누르면 여러 줄 입력 칸이 열린다. 한 줄에 '키 = 값' 하나를 적는다. # 으로 시작하는 줄은 설명이다.",
        "대상 VM: vmx 파일의 키를 적는다. vmx 줄(키 = \"값\")을 그대로 붙여넣어도 된다. 값을 비우면 그 키의 줄을 지운다. "
        "예: sched.mem.pin = TRUE, sched.cpu.affinity = 0,1,2,3. CPU affinity 같은 값은 계산하지 않으므로 키와 값을 직접 적는다.",
        "대상 ESX 호스트: 호스트 고급 설정을 /구역/옵션 = 값 또는 구역.옵션 = 값 으로 적는다. "
        "예: Numa.LocalityWeightActionAffinity = 0. 재부팅해야 적용되는 호스트 설정은 넣지 않는다(수동으로 적용).",
        "적용 방법: 케이스의 목표 값이 현재 값과 모두 같으면 아무것도 바꾸지 않는다. 하나라도 다르면 그 케이스의 VM 을 종료하고, "
        "호스트 옵션을 바꾸고, vmx 에서 같은 키의 줄을 모두 지운 뒤 한 줄을 추가하고, VM 을 표의 순서대로 하나씩 켠다. "
        "vmx 는 바꾸기 전에 같은 폴더에 <이름>.vmx.tcgen-<시각> 으로 복사해 둔다.",
        "프로파일을 여러 개 고르면 이 표의 순서대로 합친다. 같은 키가 겹치면 아래쪽 프로파일의 값을 쓴다.",
        "VM 을 구별하는 값이나 파일 위치를 적는 키(displayName, uuid.bios, 디스크 파일 이름 등)는 넣을 수 없다.",
        "기본 프로파일: 생성기를 처음 실행하면 이 표에 기본 프로파일 12개가 들어 있다. 1GB page 백킹 키(lpage_1g_on, lpage_1g_off), "
        "유휴 vCPU 처리 키(halt_in_monitor_on, halt_in_monitor_off), Latency Sensitivity(latency_high, latency_normal), "
        "vPMC(vpmc_on, vpmc_off), vmx stats 빌드(kstats_on, kstats_off), 호스트 옵션 Cpu.HTWholeCoreThreshold(ht_wholecore_1, "
        "ht_wholecore_800)이다. 이름이 _off 로 끝나는 프로파일은 그 키를 vmx 에서 지운다. 고치거나 삭제해도 된다.",
        "[기본 프로파일 추가] 는 표에 없는 이름의 기본 프로파일만 표 끝에 추가한다. 이름이 같은 행은 고친 내용 그대로 둔다. "
        "처음 값으로 복원하려면 그 행을 삭제한 뒤 버튼을 누른다.",
        "lpage_1g_on 은 VM 메모리 전체 예약이, latency_high 는 CPU 와 메모리 전체 예약이 있어야 한다. 없으면 VM 이 켜지지 않고, "
        "진행 스크립트가 설정을 이전 값으로 복원한 뒤 멈춘다.",
    ]
    RESULT_HELP_SEQ = [
        "A. 점프 VM 최초 1회 설정: 점프 VM 터미널에 처음 한 번 붙여넣는다(생성기를 새 판으로 바꾼 뒤, 작업 폴더를 바꾼 뒤에도 한 번). "
        "작업 폴더 {D} 에 접속 스크립트, 진행 스크립트(tcrun), 설정 적용 스크립트를 설치하고 sha256 으로 확인해 OK 또는 FAILED 를 "
        "출력한다. ESX 접속용 ssh 키가 없으면 만든다. 작업 폴더가 ~/.tcgen 이 아니면 ~/.tcgen 의 키와 VM 비밀번호를 복사한다.",
        "A 블록 끝에 나오는 echo '...' >> /etc/ssh/keys-root/authorized_keys 한 줄을 각 ESX 호스트의 root SSH 창에 한 번 붙여넣는다. "
        "그 뒤 점프 VM 에서 sh {D}/esxssh <ESX 호스트> hostname 이 비밀번호를 묻지 않고 호스트 이름을 출력하면 된 것이다. "
        "이 줄은 sh {D}/tcrun key 로 다시 볼 수 있다. ESX 비밀번호는 저장하지 않는다.",
        "VM password: 가 나오면 VM 비밀번호를 입력한다(화면에 보이지 않음). 이미 저장돼 있으면 묻지 않는다. "
        "바꿀 때는 sh {D}/setpw 를 실행한다.",
        "P. 점검 블록: 실행 전에 점프 VM 터미널에 붙여넣는다. 아무것도 바꾸지 않고 ESX 키 접속, python3 와 vim-cmd 와 esxcli, "
        "VM 찾기, 설정의 현재 값과 목표 값, VM 마다 지금 전원과 케이스가 원하는 전원(TCGEN-POWER), VM 접속과 pgrep, 시계 차이를 "
        "출력한다. 마지막 줄이 CHECK RESULT: OK 이면 실행 블록을 붙여넣는다.",
        "S. 실행 블록: 점프 VM 터미널에 붙여넣으면 ESX 호스트별 진행이 백그라운드로 시작된다. 터미널을 닫아도 계속된다. "
        "설정 값이 현재 값과 다른 케이스에서는 그 케이스의 VM 을 자동으로 종료하고 다시 켠다. VM 행의 전원 칸이 끔인 VM 은 끄고, "
        "켬인 VM 은 꺼져 있으면 켠다.",
        "실행을 더할 때: 앞 실행이 진행 중인 ESX 호스트에 새 S 블록을 붙여넣으면 그 실행은 대기(queued)했다가 앞 실행이 done 으로 "
        "끝나면 스스로 시작한다. 앞 실행이 멈췄거나(paused, stopped) 줄을 남긴 채 끝났으면 시작하지 않고 이유를 알린다. 그때는 "
        "sh {D}/tcrun cancel <ESX 호스트> 로 정리한 뒤 같은 S 블록을 다시 붙여넣는다.",
        "진행 확인: sh {D}/tcrun status (진행 중, 대기 중, 줄이 남은 실행을 ESX 호스트별로). 멈추기: sh {D}/tcrun stop (진행만 "
        "멈춘다. 시작된 잡, 모니터링, crontab 줄은 그대로 둔다). 취소: sh {D}/tcrun cancel [ESX 호스트] (진행 중이거나 대기 중인 "
        "실행을 바로 끝내고 등록한 crontab 줄을 지운다. 시작된 모니터링은 중지 명령을 실행한다. 실행 중인 잡은 끝내지 않고 목록만 "
        "보인다. 적용한 설정은 그대로 둔다. 끝난 실행이 남긴 줄과 모니터링도 정리한다).",
        "점프 VM 로그 폴더에 남는 파일: tcrun-<실행 ID>-<ESX 호스트>.log (단계와 시각), -result.csv (케이스별 결과), "
        "-perfreport_cases.csv (성능 리포트 생성기의 케이스 정보 파일. 잡 시작과 종료는 실제 시각), "
        "-config-<케이스 번호>-<케이스>.csv (구성 스냅샷).",
        "구성 스냅샷(config snapshot): 잡이 있는 케이스마다 잡 시작 전에 그 케이스 VM 의 VM 설정(ESX 호스트에서 읽은 vmx 값)과 "
        "Guest OS 설정(THP, hugepages 등)을 csv 로 남긴다. 케이스의 준비 때(시작 시각을 적은 케이스는 그 시각에서 '시작 시각 준비' 분 "
        "전) 설정 적용과 VM 전원 맞추기를 마친 뒤에 읽는다. 배치는 config_snapshot 의 VM 표와 같다(구분, 항목, VM 이름 열). 읽기만 "
        "하고, 켜져 있는 VM 마다 접속 한 번이다. 읽지 못해도 케이스는 진행한다.",
        "잡 시작 전의 대기: 잡은 구성 스냅샷이 끝나고 '스냅샷 뒤 잡 시작까지 최소' 분, 이번 실행의 마지막 변경 작업(설정 적용, VM 끄기, "
        "VM 켜기)이 끝나고 '변경 작업 뒤 잡 시작까지 최소' 분이 지난 뒤에 시작한다. 진행 로그의 settle: 줄과 snapshot gap: 줄에 "
        "적용한 내용이 남는다. 이 때문에 적은 시작 시각을 지킬 수 없으면 로그에 WARN the start time 줄이 남는다.",
        "스냅샷 때 VM 에서 실행할 명령을 적었으면(설정 영역) 구성 스냅샷 때 켜져 있는 VM 마다 그 명령을 실행한다. 결과는 스냅샷 csv 의 "
        "snapshot command 행과 진행 로그의 config snapshot: command on vm 줄에 남는다.",
        "진행이 중간에 멈추면 로그의 PAUSED 줄에서 이유를 확인하고 고친 뒤, 끝난 케이스를 탭 4 에서 적용 체크하고 다시 생성해 "
        "실행 블록을 붙여넣는다. 남은 줄을 모두 지우고 끝내려면 sh {D}/tcrun cancel 을 실행한다.",
        "D. VM crontab 전체 삭제: 필요할 때만 쓴다. VM 의 crontab 을 모두 지운다(tcgen 이 등록하지 않은 줄 포함. 지우기 전 내용은 "
        "각 VM 홈 폴더의 tcgen_crontab_backup-<시각>.txt 에 저장). 진행 중에 붙여넣으면 등록된 줄이 지워져 잡이 시작되지 않는다.",
        "[복사] 를 누르면 그 블록 전체가 클립보드에 들어간다. 점프 VM 터미널에서는 마우스 오른쪽 또는 Shift+Insert 로 붙여넣는다.",
        "생성할 때마다 테스트 기록 CSV 에 케이스 행이 추가되고, 로그 폴더에 생성 로그가 남는다. 시작 시각을 적은 케이스는 잡 시작, "
        "모니터링 시작 시각(잡을 확인하지 않는 케이스는 모니터링 중지 시각도)이 적힌다. 자동인 케이스는 시각 칸이 비어 있다.",
    ]
    RESULT_HELP = [
        "A. 점프 VM 최초 1회 설정: 점프 VM 에서 처음 한 번만 붙여넣는다. VM password: 가 나오면 VM 비밀번호를 입력한다(화면에 보이지 않음). "
        "비밀번호는 ~/.tcgen 에 본인만 읽을 수 있게 저장된다. 비밀번호를 바꿀 때는 sh ~/.tcgen/setpw 를 실행한다. "
        "ESX 비밀번호는 저장하지 않는다.",
        "B. VM 등록 블록: 점프 VM 터미널에 붙여넣는다. 어느 폴더에서 붙여넣어도 결과는 같다. VM 마다 ssh 로 접속해 crontab 에 줄을 "
        "등록하고 등록된 줄을 출력한다. 다시 붙여넣어도 줄이 늘지 않는다.",
        "C. ESX 블록: ESX 호스트를 고른 뒤 복사해 그 ESX 의 root SSH 창에 붙여넣는다. crontab 시각은 UTC (KST - 9시간)이다. "
        "ESX 를 재부팅하면 등록한 줄이 지워진다.",
        "D. VM crontab 전체 삭제: 필요할 때만 B 보다 먼저 점프 VM 터미널에 붙여넣는다. B 블록과 같은 VM 에 같은 계정으로 접속해 "
        "crontab 을 모두 지운다(crontab -r). tcgen 이 등록하지 않은 줄도 지워진다. 지우기 전 내용은 각 VM 홈 폴더의 "
        "tcgen_crontab_backup-<시각>.txt 에 저장되고, 그 VM 에서 crontab <그 파일> 을 실행하면 되돌릴 수 있다. ESX 의 crontab 은 지우지 않는다.",
        "적용 체크한 케이스가 같은 VM 을 쓰고 아직 실행할 시각이 남아 있으면, D 블록이 그 케이스의 줄도 지운다. 이때는 결과 글과 "
        "D 블록 [복사] 확인 창에 케이스 이름이 나온다. 그 케이스도 다시 등록하려면 적용 체크를 끄고 다시 생성한다.",
        "[복사] 를 누르면 그 블록 전체가 클립보드에 들어간다. 점프 VM 터미널에서는 마우스 오른쪽 또는 Shift+Insert 로 붙여넣는다.",
        "생성할 때마다 테스트 기록 CSV 에 케이스 행이 추가되고, 로그 폴더에 생성 로그가 남는다.",
    ]

    def case_help(self):
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        p = self.vm_prefix()
        n = int(self.settings.get("default_vms", 2))
        if self.mode() == "seq":
            st = self.current_settings(show_error=False) or self.settings
            return [
                "순서 실행에서는 점프 VM 이 케이스를 표의 순서대로 진행한다. ESX 호스트가 여러 대이면 "
                "호스트마다 따로 진행하고 서로 기다리지 않는다.",
                "케이스 행(옅은 파란 바탕, 굵은 글씨): 적용 체크, 케이스 이름, 설명, ESX 호스트, ESX 모니터링, 설정(대상이 ESX 호스트인 "
                "프로파일), 최대 대기, 시작 시각, 테스트 그룹",
                "시작 시각: 잡을 시작할 시각을 적는다. 표에 적은 값이 그 케이스의 잡 시작 기록으로 남는다. '2026-11-20 10:00:00' 처럼 "
                "날짜까지 적거나, '10:00' 처럼 시:분만 적는다(지금부터 처음 오는 그 시각으로 날짜를 붙여 저장한다). 새 표의 첫 케이스에는 "
                "다음 정시가 들어가 있고, 케이스를 복사하면 시각은 비워져 '입력 필요' 로 보인다. 잡이 있는 케이스의 시작 시각이 비어 "
                "있으면 [명령어 생성] 이 블록을 만들지 않고 알린다. 잡이 없는 케이스(설정과 VM 전원만 맞춤)는 시작 시각을 쓰지 않으므로 "
                "비워 둔다.",
                "시작 시각에 '자동' 이라고 적으면 프로세스 확인 방식이다: 앞 케이스의 잡이 끝난 것을 확인하고 %d분(설정 영역의 '케이스 "
                "사이 간격') 이상 지난 뒤에 잡을 시작한다. 확인하러 잡이 실행되는 동안 VM 에 접속하므로 측정에 부하가 될 수 있다. 칸에는 "
                "'자동 (잡 확인)' 으로 보이고, 생성할 때 경고로 알린다." % int(st.get("seq_gap_min", 30)),
                "시작 시각에 +간격: '+8:00', '+8', '+8.5' 처럼 + 로 시작하면 같은 ESX 호스트의 바로 앞 케이스 시작 시각에서 그만큼 뒤다"
                "(8시간, 8시간, 8시간 30분). 예: 첫 케이스에 10:00, 다음 케이스들에 +8:00 을 적으면 10:00, 18:00, 다음 날 02:00 에 시작한다. "
                "그 호스트의 첫 케이스(이번 생성에 넣는 케이스 가운데)에는 시각을 직접 적는다. +간격을 적은 케이스를 [+ 케이스 추가] 로 "
                "복사하면 같은 +간격이 들어간다.",
                "시각을 적은 케이스(시각 또는 +간격): 그 시각 %d분 전(설정 영역의 '시작 시각 준비')에 설정 적용과 VM 전원 맞추기를 시작한다. "
                "케이스 사이 간격은 적용하지 않는다. VM 을 켠 뒤의 대기가 남았으면 가능한 가장 이른 시각에 시작하고 로그에 WARN 을 "
                "남긴다." % int(st.get("seq_prep_lead_min", 60)),
                "잡 확인 안 함: 시작 시각을 적은 케이스는 정해진 시각에 진행하므로 잡 프로세스를 확인하지 않는다. 잡이 실행되는 동안 VM 에 "
                "접속하지 않는다(VM 접속은 준비 때 VM 마다 3~4번, 그 가운데 1번이 구성 스냅샷. 케이스가 끝난 뒤 1번). 그 케이스는 다음 케이스 준비 2분 전에 모니터링을 "
                "중지하고 끝낸다. 최대 대기를 적었으면 잡 시작 + 최대 대기가 더 이를 때 그 시각에 끝낸다. 마지막 케이스에 최대 대기가 없으면 "
                "모니터링을 중지하지 않고 실행을 끝낸다(같은 ESX 호스트의 다음 실행이 준비를 시작할 때나 tcrun cancel 로 중지).",
                "잡 확인 함: 시작 시각이 자동인 케이스와, 시각을 적었어도 바로 다음 케이스가 자동인 케이스는 잡 종료를 알아야 하므로 확인한다. "
                "어느 케이스를 확인하는지는 탭 5 의 실행 계획에 케이스마다 '잡 확인: 함 / 안 함' 으로 나온다.",
                "잡 시작 최소 간격: 모든 잡 시작은 서로 %d분 이상 떨어진다. ESX 호스트가 달라도, 이 점프 VM 에서 도는 다른 실행과도 같다. "
                "자동인 케이스의 잡 시작이 다른 잡 시작과 가까우면 그 잡 시작 %d분 뒤로 옮기고 로그에 spacing 줄을 남긴다. 시각을 적은 "
                "케이스끼리 이보다 가까우면 [명령어 생성] 이 오류를 낸다." % (int(st.get("seq_start_spacing_min", 5)),
                                                          int(st.get("seq_start_spacing_min", 5))),
                "VM 행(└): VM 이름, 전원(켬, 끔), 잡, 그 VM 의 모니터링, 설정(대상이 VM 인 프로파일), 종료 기준 VM",
                "한 케이스의 진행 순서: 설정 적용, VM 전원 맞추기, 모니터링 시작, %d분 뒤 잡 시작(잡이 있는 모든 VM 이 같은 분에 시작), "
                "잡 종료 확인, 모니터링 중지, crontab 줄 삭제. 그 뒤 다음 케이스로 넘어간다." % ld[0],
                "설정 적용: 목표 값이 현재 값과 모두 같으면 VM 을 끄지 않는다. 하나라도 다르면 그 케이스의 VM 을 종료하고 값을 바꾼다.",
                "VM 전원: ESX 호스트가 있는 케이스에서는 이름이 있는 VM 행이 모두 그 케이스의 VM 이다. 전원 칸이 끔인 VM 은 켜져 있으면 "
                "끈다(게스트 종료, %d분 안에 꺼지지 않으면 전원 끄기). 켬인 VM 은 표의 순서대로 하나씩 확인해 꺼져 있으면 켠다. VM 마다 켜기 "
                "작업이 끝나고, 1GB page 를 쓰는 VM(sched.mem.lpage.enable1GPage)은 1GB page backing 수가 메모리 GB 수와 같아지고, "
                "ssh 로그인이 되면 다음 VM 으로 넘어간다. backing 수가 늘지 않으면 진행을 멈춘다." % int(st.get("seq_shutdown_wait_min", 10)),
                "잡 시작 전의 시간 규칙: 이번 실행이 그 ESX 호스트에서 변경 작업(설정 적용, VM 끄기, VM 켜기)을 했으면 마지막 변경이 "
                "끝난 시각에서 %d분 이상 지난 뒤에 잡을 시작한다(설정 영역의 '변경 작업 뒤 잡 시작까지 최소'). 변경한 것이 없으면 이 "
                "대기는 없다. 구성 스냅샷이 끝난 시각에서는 %d분 이상 지난 뒤에 잡을 시작한다('스냅샷 뒤 잡 시작까지 최소'). 모니터링은 "
                "잡 시작 %d분 전에 시작한다. 시작 시각을 적은 케이스는 준비를 그 시각 %d분 전에 시작하므로 변경 작업이 %d분 안에 끝나면 "
                "적은 시각에 잡을 시작한다. 두 대기 때문에 적은 시각을 지킬 수 없으면 가능한 가장 이른 시각에 시작하고 진행 로그에 WARN "
                "을 남긴다." % (int(st.get("seq_settle_min", 30)), int(st.get("seq_snap_gap_min", 5)), ld[0],
                             int(st.get("seq_prep_lead_min", 60)),
                             max(0, int(st.get("seq_prep_lead_min", 60)) - int(st.get("seq_settle_min", 30)))),
                "스냅샷 때 VM 에서 실행할 명령(설정 영역): 적어 두면 구성 스냅샷 때 케이스의 켜져 있는 VM 마다 접속해 그 명령을 실행한다. "
                "게스트 구성을 VCF Operations 에 기록하는 스크립트(guest_config_push)를 VM 안의 경로로 적는다. 결과는 스냅샷 csv 의 "
                "snapshot command 행과 진행 로그에 남고, 실패해도 케이스는 진행한다. P 블록(점검)이 VM 에 그 파일이 있는지 확인한다.",
                "잡 종료 확인(확인하는 케이스): 탭 1 의 프로세스 이름으로 각 VM 에서 프로세스를 찾는다. 잡이 있는 모든 VM 에서 한 번 이상 보인 뒤 "
                "%d회 연속으로 없으면 종료로 판정한다. 잡 시작 뒤 %d분 안에 한 번도 보이지 않으면 진행을 멈춘다."
                % (int(st.get("seq_gone_count", 3)), int(st.get("seq_start_wait_min", 10))),
                "확인하는 때: 잡이 실행되는 동안 VM 접속을 줄이려고, 잡 시작 %d초 뒤에 한 번 확인한 다음 %d분(설정 영역의 '잡 종료 확인 간격')마다 "
                "확인한다. 모든 VM 에서 프로세스가 없어진 뒤에는 1분 간격으로 다시 확인해 종료를 확정한다. 그래서 종료를 아는 시점은 실제 "
                "종료보다 최대 그 간격만큼 늦고, 간격이 2분을 넘으면 리포트용 CSV 의 잡 종료 시각은 비워 둔다(성능 리포트 생성기가 정한다)."
                % (int(st.get("seq_first_check_sec", 60)), poll_minutes(st.get("seq_poll_sec", 1800))),
                "설정 칸: 두 번 누르면 탭 3 의 프로파일을 고르는 창이 열린다. 여러 개를 고르면 탭 3 의 순서대로 합치고, 같은 키가 겹치면 "
                "아래쪽 프로파일의 값을 쓴다.",
                "최대 대기: 잡을 확인하는 케이스는 잡 시작부터 이 시간(시간:분, 예 12:00)이 지나도 잡이 끝나지 않으면 진행을 멈춘다. "
                "잡을 확인하지 않는 케이스는 이 시간이 지나면 모니터링을 중지하고 케이스를 끝낸다. 비우면 제한이 없다.",
                "ESX 호스트가 없는 케이스는 VM 전원을 다루지 않고, 잡, 모니터링, 설정이 있는 VM 행만 쓴다. 그 VM 은 켜져 있어야 한다.",
                "잡 없이 설정만 지정한 케이스는 설정만 적용하고 다음 케이스로 넘어간다. 모니터링만 있고 잡이 없는 케이스는 오류다.",
                "[명령어 생성] 을 누르면 넣을 케이스를 고르는 창이 열린다. 처음에는 적용 체크하지 않은 케이스가 골라져 있다. "
                "진행이 중간에 멈추면 끝난 케이스를 적용 체크하고 다시 생성해 나머지를 이어서 실행한다.",
                "적용 체크한 케이스의 시작 시각은 그 케이스의 잡 시작 기록이다. 시작 시각 칸에 +간격을 적은 케이스를 적용 체크하면 칸의 "
                "값을 날짜 시각으로 바꿔 고정한다(마지막 [명령어 생성] 때 계획한 시각). 적용 체크한 케이스의 시작 시각은 고칠 수 없다. "
                "고치려면 적용 체크를 끄고 고친 뒤 다시 체크한다.",
                "[기록 대조]: 적용 체크한 케이스의 시작 시각을 테스트 기록 CSV 의 마지막 생성 기록이나 예전 표 파일(백업)과 대조한다. "
                "값이 다른 케이스를 보이고, 고른 케이스의 값을 기준 값으로 되돌린다. 바꾸기 전의 표는 데이터 폴더의 backup 아래에 저장한다.",
                "칸 고치기: 칸을 두 번 누르거나 행을 고르고 Enter(또는 F2)를 누른다. [+ 케이스 추가] 를 누르면 새 케이스의 이름 칸이 바로 "
                "열린다. 케이스 이름과 VM 이름은 영문, 숫자, . _ - 만 쓴다(공백, 한글 불가). 한글은 설명 칸에 적는다.",
                "새 케이스는 VM %d대(ESX 호스트 + %s01, %s02)가 기본이다. [+ VM 추가] 는 선택한 케이스에 다음 번호 VM 을 추가한다. "
                "[+ 케이스 추가] 는 선택한 케이스를 VM 구성과 설정까지 복사한다." % (n, p, p),
                "삭제, 위로, 아래로: 케이스 행을 고르면 케이스를, VM 행을 고르면 그 VM 을 대상으로 한다. 케이스의 순서가 실행 순서이고, "
                "VM 의 순서가 VM 을 켜는 순서다.",
                "테스트 그룹, 종료 기준 VM: 성능 리포트 생성기에 넘기는 값이다. 잡 시작과 종료 시각은 점프 VM 이 실제 시각으로 "
                "기록한다(점프 VM 로그 폴더의 tcrun-<실행 ID>-<ESX 호스트>-perfreport_cases.csv).",
            ]
        return [
            "케이스 행(옅은 파란 바탕, 굵은 글씨): 적용 체크, 케이스 이름, 설명, ESX 호스트, ESX 모니터링, 간격, 잡 시작, "
            "모니터링 중지, 테스트 그룹, 잡 종료 (리포트)",
            "VM 행(└): VM 이름, 잡, 그 VM 의 모니터링, 종료 기준 VM",
            "칸을 두 번 누르거나 행을 고르고 Enter(또는 F2)를 누르면 고친다. 적용 칸을 누르면(또는 Space) 체크되고, 체크한 케이스는 "
            "[명령어 생성] 의 선택 창에서 처음에 빠진 채로 나온다. "
            "표는 바뀔 때마다 데이터 폴더의 test_cases.json 에 저장된다.",
            "새 케이스는 VM %d대(ESX 호스트 + %s01, %s02)가 기본이다. [+ VM 추가] 는 선택한 케이스에 다음 번호 VM(%s03, %s04 …)을 추가한다. "
            "ESX 호스트를 바꾸면 자동으로 붙은 VM 이름도 따라 바뀐다." % (n, p, p, p, p),
            "잡이 (없음) 이면 모니터링만 등록한다. 잡과 모니터링이 모두 없는 VM 은 등록하지 않는다.",
            "+ 케이스 추가: 선택한 케이스를 VM 구성까지 복사한다. 잡 시작 = 윗줄 잡 시작 + 간격. 설명, 테스트 그룹, 종료 기준 VM 은 "
            "복사하고 잡 종료 (리포트) 는 비운다.",
            "삭제, 위로, 아래로: 케이스 행을 고르면 케이스를, VM 행을 고르면 그 VM 을 대상으로 한다. "
            "위로, 아래로는 적용 체크하지 않은 케이스끼리는 순서만 바꾸고 시각은 그 행에 남는다.",
            "적용 체크한 케이스는 등록한 기록으로 고정한다: 간격, 잡 시작, 모니터링 중지 칸은 고칠 수 없고, 다른 케이스를 추가하거나 "
            "삭제하거나 순서를 바꿔도 그 케이스의 간격, 잡 시작, 모니터링 중지, 잡 종료 (리포트) 는 바뀌지 않는다. 위로, 아래로 옮기면 "
            "이 값들이 케이스와 함께 움직인다. 고치려면 적용 체크를 끄고 고친 뒤 다시 체크한다.",
            "바로 윗줄이 적용 체크한 케이스인 행은 윗줄과 이어지지 않는다: 다른 행을 고칠 때 다시 계산하지 않고, 잡 시작을 직접 적어도 "
            "간격을 바꾸지 않는다. 그 행을 추가한 직후와 그 행의 간격을 직접 고칠 때만 잡 시작 = 윗줄 잡 시작 + 간격으로 계산한다.",
            "시각 형식: 2026-11-09 10:00:00, 간격 형식: 12:00",
            "설명: 케이스 행에 시험 조건을 적는다(한글 가능). 테스트 기록 CSV 와 리포트용 CSV 에 들어가고, 붙여넣기 블록과 "
            "crontab 줄에는 들어가지 않는다.",
            "테스트 그룹, 잡 종료 (리포트), 종료 기준 VM: 성능 리포트 생성기에 넘기는 값이다. crontab 에는 쓰지 않는다. "
            "테스트 그룹이 같은 케이스는 리포트 생성기가 HTML 하나에 모은다. 잡 종료를 비우면 '자동' 으로 보이고 리포트 생성기가 "
            "종료 시각을 정한다. 종료 기준 VM 은 VM 행의 칸을 두 번 누르면 '기준' 이 되고 다시 누르면 해제된다. 여러 대를 지정할 수 있다.",
            "[리포트용 CSV 저장]: 저장할 케이스를 고르고 저장 위치를 정한다. 고른 케이스는 이 표의 순서대로 저장한다. 성능 리포트 "
            "생성기의 '케이스 정보 파일' 에서 이 파일을 고른다. VM 열에는 잡이나 모니터링을 지정한 VM 이 들어간다. "
            "비교에 쓰는 기준은 여기서 정하지 않는다. 성능 리포트 생성기에서 정한다.",
            "[기록 대조]: 적용 체크한 케이스의 잡 시작, 간격, 모니터링 중지, 잡 종료 (리포트) 를 테스트 기록 CSV 의 마지막 생성 "
            "기록이나 예전 표 파일(백업)과 대조한다. 값이 다른 칸을 보이고, 고른 케이스의 값을 기준 값으로 되돌린다. 바꾸기 전의 "
            "표는 데이터 폴더의 backup 아래에 저장한다.",
            "모니터링 시작 = 잡 시작 %d분 전. 모니터링 중지 = 자동(다음 케이스 잡 시작 %d분 전, 마지막 케이스는 잡 시작 + 간격 - %d분), "
            "직접 입력, 중지 없음 중 하나. 이 시각은 그 케이스의 모든 VM 과 ESX 에 같이 적용된다." % (ld[0], ld[1], ld[1]),
        ]

    def build_run_tab(self):
        f = self.tab_page("1. 실행 명령")
        cols = [dict(key="app", title="App명", width=110), dict(key="name", title="명령어 명", width=140),
                dict(key="desc", title="잡 조건 설명", width=200),
                dict(key="proc", title="프로세스 이름", width=150),
                dict(key="cmd", title="경로/실행 명령 (절대 경로, 변수 <vm이름> <실행시각>)", width=640, stretch=True,
                     edit_width=700)]
        tb = self.toolbar(f)
        ttk.Button(tb, text="+ 행 추가", style="Green.TButton",
                   command=lambda: self.add_cmd_row(self.t_run, "run")).pack(side="left")
        ttk.Button(tb, text="삭제", command=lambda: self.del_cmd_row(self.t_run, "run")).pack(side="left", padx=self.px(6))
        self.help_button(tb, "1. 실행 명령", self.RUN_HELP)
        self.t_run = EditableTable(f, self, cols, height=8)
        self.t_run.rows = self.run_cmds
        self.t_run.on_commit = self.commit_run
        self.hint(f, "칸을 두 번 누르면 고친다. 명령은 절대 경로로 입력하고 변수 <vm이름> <실행시각> 을 쓸 수 있다. "
                     "프로세스 이름은 순서 실행에서 잡이 끝났는지 확인하는 데 쓴다. 자세한 설명은 [도움말].").pack(
            side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_run.pack(fill="both", expand=True)
        self.t_run.refresh()

    def build_mon_tab(self):
        f = self.tab_page("2. 모니터링 명령")
        cols = [dict(key="app", title="App명", width=100), dict(key="name", title="명령어 명", width=130),
                dict(key="desc", title="설명", width=150),
                dict(key="where", title="실행 위치", width=80, kind="combo", values=lambda: ["VM", "ESX"]),
                dict(key="start", title="시작 명령", width=480, stretch=True, edit_width=600),
                dict(key="stop", title="중지 명령", width=320, edit_width=500)]
        tb = self.toolbar(f)
        ttk.Button(tb, text="+ 행 추가", style="Green.TButton",
                   command=lambda: self.add_cmd_row(self.t_mon, "mon")).pack(side="left")
        ttk.Button(tb, text="삭제", command=lambda: self.del_cmd_row(self.t_mon, "mon")).pack(side="left", padx=self.px(6))
        self.help_button(tb, "2. 모니터링 명령", self.MON_HELP)
        self.t_mon = EditableTable(f, self, cols, height=8)
        self.t_mon.rows = self.mon_cmds
        self.t_mon.on_commit = self.commit_mon
        self.hint(f, "칸을 두 번 누르면 고친다. 실행 위치가 VM 이면 VM 의 crontab, ESX 이면 ESX 호스트의 crontab 에 등록한다. "
                     "자세한 설명은 [도움말].").pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_mon.pack(fill="both", expand=True)
        self.t_mon.refresh()

    def build_prof_tab(self):
        f = self.tab_page("3. 설정 프로파일")
        cols = [dict(key="name", title="프로파일 이름", width=170),
                dict(key="target", title="대상", width=110, kind="combo",
                     values=lambda: [core.PROFILE_TARGET_LABELS[k] for k in core.PROFILE_TARGETS]),
                dict(key="text", title="설정 내용 (두 번 누르면 입력 칸이 열림. 한 줄에 키 = 값 하나)", width=640, stretch=True,
                     kind="popup", popup=self.edit_profile_text),
                dict(key="desc", title="설명", width=280, edit_width=420)]
        tb = self.toolbar(f)
        ttk.Button(tb, text="+ 행 추가", style="Green.TButton", command=self.add_prof_row).pack(side="left")
        ttk.Button(tb, text="삭제", command=self.del_prof_row).pack(side="left", padx=self.px(6))
        ttk.Button(tb, text="기본 프로파일 추가", command=self.add_default_profiles).pack(side="left")
        self.help_button(tb, "3. 설정 프로파일", self.PROF_HELP)
        self.t_prof = EditableTable(f, self, cols, height=8)
        self.t_prof.rows = self.profiles
        self.t_prof.cell_value = self.prof_value
        self.t_prof.display = self.prof_display
        self.t_prof.on_commit = self.commit_prof
        self.hint(f, "순서 실행에서 케이스를 시작하기 전에 적용할 ESX 호스트 설정과 VM 설정(vmx 키)이다. 설정 내용 칸을 두 번 누르면 "
                     "입력 칸이 열린다. 탭 4 의 설정 칸에서 케이스 행은 대상이 ESX 호스트인 프로파일을, VM 행은 대상이 VM 인 "
                     "프로파일을 고른다. 자세한 설명은 [도움말].").pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.t_prof.pack(fill="both", expand=True)
        self.t_prof.refresh()

    def build_case_tab(self):
        f = self.tab_page("4. 테스트 케이스")
        job_values = lambda: [NONE_JOB] + [r["name"] for r in self.run_cmds if r.get("name")]
        cols = [dict(key="name", title="케이스 / VM", width=205), dict(key="desc", title="설명", width=160, edit_width=420),
                dict(key="esx", title="ESX 호스트", width=85),
                dict(key="power", title="전원", width=50, values=lambda: [core.POWER_LABELS["on"], core.POWER_LABELS["off"]]),
                dict(key="job", title="잡", width=95, values=job_values),
                dict(key="mon", title="모니터링", width=180, popup=self.edit_monitors, stretch=True),
                dict(key="prof", title="설정", width=200, popup=self.edit_profiles),
                dict(key="max_wait", title="최대 대기", width=80),
                dict(key="seq_start", title="시작 시각", width=150, popup=self.locked_notice),
                dict(key="interval", title="간격", width=50, popup=self.locked_notice),
                dict(key="start", title="잡 시작 (KST)", width=140, popup=self.locked_notice),
                dict(key="mon_start", title="모니터링 시작", width=95),
                dict(key="mon_stop", title="모니터링 중지", width=135, popup=self.edit_stop),
                dict(key="group", title="테스트 그룹", width=90),
                dict(key="end_time", title="잡 종료 (리포트)", width=140),
                dict(key="end_ref", title="종료 기준 VM", width=95, popup=self.toggle_end_ref)]
        px = self.px
        tb = self.toolbar(f)
        ttk.Button(tb, text="+ 케이스 추가", style="Green.TButton", command=self.add_case).pack(side="left")
        ttk.Button(tb, text="+ VM 추가", style="Purple.TButton", command=self.add_vm).pack(side="left", padx=(px(6), 0))
        ttk.Button(tb, text="삭제", command=self.del_row).pack(side="left", padx=(px(16), 0))
        ttk.Button(tb, text="위로", command=lambda: self.move_row(-1)).pack(side="left", padx=(px(6), 0))
        ttk.Button(tb, text="아래로", command=lambda: self.move_row(1)).pack(side="left", padx=(px(6), 0))
        ttk.Button(tb, text="명령어 생성", style="Red.TButton", command=self.generate).pack(side="right", ipadx=px(10),
                                                                                    padx=(px(8), 0))
        self.help_button(tb, "4. 테스트 케이스", self.case_help)
        ttk.Button(tb, text="리포트용 CSV 저장", style="Blue.TButton", command=self.export_report).pack(side="right")
        ttk.Button(tb, text="기록 대조", command=self.compare_records).pack(side="right", padx=(0, px(6)))
        t = EditableTable(f, self, cols, height=10,
                          check=dict(title="적용", width=48, on=self.img_on, off=self.img_off, toggle=self.toggle_applied))
        self.t_case = t
        t.rows = self.cases
        t.row_ids = self.case_row_ids
        t.cell_value = self.case_value
        t.cell_kind = self.case_kind
        t.row_check = self.case_check
        t.row_tags = self.case_tags
        t.display = self.case_display
        t.on_commit = self.commit_case
        t.before_refresh = self.calc_schedule
        t.tree.tag_configure("case", background=COLORS["case"], font=self.bold)
        t.tree.tag_configure("vm", background="white")
        self.case_hint = self.hint(f, "")
        self.case_hint.pack(side="bottom", fill="x", pady=(px(6), 0))
        t.pack(fill="both", expand=True)
        t.refresh()

    CASE_HINT = {
        "time": "칸을 두 번 누르거나 행을 고르고 Enter 를 누르면 고친다. 케이스 행(옅은 파란 바탕)은 케이스 설정, VM 행(└)은 그 VM 의 잡과 모니터링. "
                "[명령어 생성] 을 누르면 넣을 케이스를 고르는 창이 열린다. 테스트 그룹, 잡 종료 (리포트), 종료 기준 VM 은 "
                "리포트용 CSV 에만 들어간다. 적용 체크한 케이스의 간격, 잡 시작, 모니터링 중지는 고정된다(순서를 바꿔도 케이스와 함께 "
                "움직인다). 자세한 설명은 [도움말].",
        "seq": "순서 실행: 점프 VM 이 케이스를 표의 순서대로 준비하고(설정 적용, VM 전원, 구성 스냅샷) 시작 시각 칸에 적은 시각에 잡을 "
               "시작한다. 시작 시각은 날짜 시각(2026-11-20 10:00:00), 시:분(10:00), +간격(+8:00 = 앞 케이스 시작 8시간 뒤)으로 적는다. "
               "이렇게 적은 케이스는 잡이 실행되는 동안 VM 에 접속하지 않는다. '자동' 이라고 적으면 앞 케이스의 잡 프로세스가 끝난 것을 "
               "확인한 뒤 이어서 시작한다(확인하러 VM 에 접속한다). "
               "케이스 행의 설정 칸은 ESX 호스트 설정, VM 행의 설정 칸은 VM 설정(탭 3 의 프로파일)이다. "
               "칸을 두 번 누르거나 행을 고르고 Enter 를 누르면 고친다. [명령어 생성] 을 누르면 넣을 케이스를 고르는 창이 열린다. "
               "자세한 설명은 [도움말].",
    }
    RESULT_HINT = {
        "time": "블록마다 [복사] 를 누른 뒤 붙여넣을 창에 붙여넣는다. A 는 점프 VM 에서 처음 한 번만 붙여넣고, "
                "VM password: 가 나오면 VM 비밀번호를 입력한다. D 는 VM 의 crontab 을 모두 지울 때만 B 보다 먼저 "
                "점프 VM 에 붙여넣는다.",
        "seq": "블록마다 [복사] 를 누른 뒤 점프 VM 터미널에 붙여넣는다. A 는 처음 한 번만 붙여넣고, 끝에 나오는 한 줄을 각 ESX 호스트의 "
               "root SSH 창에 붙여넣어 키를 등록한다. 그 뒤 P(점검)에서 CHECK RESULT: OK 를 확인하고 S(실행)를 붙여넣는다. "
               "D 는 VM 의 crontab 을 모두 지울 때만 쓴다.",
    }
    BLOCK_TITLES = {
        "time": ("B. VM 등록 블록 (점프 VM 터미널)", "C. ESX 블록 (root SSH 창, 시각은 UTC)"),
        "seq": ("P. 점검 블록 (점프 VM 터미널, 아무것도 바꾸지 않음)", "S. 실행 블록 (점프 VM 터미널, 진행 시작)"),
    }

    def mode(self):
        return "time" if self.v_mode.get() == MODE_LABELS["time"] else "seq"

    def apply_mode(self):
        """진행 방식에 맞게 화면을 바꾼다: 설정 영역의 칸, 탭 4 의 열, 탭 5 의 블록 제목과 실행 계획 칸."""
        m = self.mode()
        seq = m == "seq"
        px = self.px
        self.f_time.pack_forget()
        self.f_seq.pack_forget()
        (self.f_seq if seq else self.f_time).pack(side="left")
        if seq:
            self.row_seq2.pack(fill="x", pady=(px(6), 0), before=self.row3)
            self.row_seq.pack(fill="x", pady=(px(6), 0), before=self.row3)
        else:
            self.row_seq.pack_forget()
            self.row_seq2.pack_forget()
        hide = SEQ_HIDE if seq else TIME_HIDE
        self.t_case.set_visible([c["key"] for c in self.t_case.columns if c["key"] not in hide])
        self.case_hint.configure(text=self.CASE_HINT[m])
        self.result_hint.configure(text=self.RESULT_HINT[m])
        self.blk_vm.frame.configure(text=self.BLOCK_TITLES[m][0])
        self.blk_esx.frame.configure(text=self.BLOCK_TITLES[m][1])
        # 순서 실행에서는 블록 칸을 낮게 하고 실행 계획 칸과 높이를 나눠 쓴다
        for blk in (self.blk_setup, self.blk_vm, self.blk_esx, self.blk_wipe):
            blk.configure(height=4 if seq else 10)
        if seq:
            self.esx_pick.pack_forget()
            self.lf_plan.grid(row=1, column=0, sticky="nsew", pady=(px(8), 0))
            self.result_area.rowconfigure(1, weight=2)
        else:
            self.esx_pick.pack(side="left")
            self.lf_plan.grid_remove()
            self.result_area.rowconfigure(1, weight=0)
        self.t_case.refresh()

    def mode_changed(self):
        if self.loading:
            return
        self.finish_all_edits()
        self.autosave("settings")
        self.apply_mode()
        self.show_messages("진행 방식: %s. 탭 4 에서 [명령어 생성] 을 누르면 여기에 결과가 나온다." % MODE_LABELS[self.mode()], [], [])

    def build_result_tab(self):
        f = self.tab_page("5. 생성 결과")
        px = self.px
        tb = self.toolbar(f)
        self.help_button(tb, "5. 생성 결과", lambda: [x.replace("{D}", self.jump_dir_text()) for x in self.RESULT_HELP_SEQ]
                         if self.mode() == "seq" else self.RESULT_HELP)
        self.result_hint = self.hint(tb, "")
        self.result_hint.pack(side="left", fill="x", expand=True)
        self.res_text = self.text_box(f, height=5, wrap="word", font=self.small)
        self.res_text.pack(fill="x")
        make_readonly(self.res_text)
        self.res_text.tag_configure("ok", foreground="#1e7b34", font=self.bold)
        self.res_text.tag_configure("err", foreground="#b03a2e")
        self.res_text.tag_configure("warn", foreground="#9a6700")
        set_text(self.res_text, "탭 4 에서 [명령어 생성] 을 누르면 여기에 결과가 나온다.")
        # 블록 칸(위)과 실행 계획 칸(아래, 순서 실행에서만)을 한 틀에 두고 높이를 나눠 쓴다. 창이 낮아도 둘 다 보이게 한다
        area = ttk.Frame(f)
        area.pack(fill="both", expand=True, pady=(px(8), 0))
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.result_area = area
        pw = ttk.PanedWindow(area, orient="horizontal")
        pw.grid(row=0, column=0, sticky="nsew")
        self.blk_setup = self._block_panel(pw, "A. 점프 VM 최초 1회 설정", lambda: self.copy_block("setup"))
        self.blk_vm = self._block_panel(pw, "B. VM 등록 블록 (점프 VM 터미널)", lambda: self.copy_block("vm"))
        self.blk_esx = self._block_panel(pw, "C. ESX 블록 (root SSH 창, 시각은 UTC)", lambda: self.copy_block("esx"),
                                         esx=True)
        self.blk_wipe = self._block_panel(pw, "D. VM crontab 전체 삭제", lambda: self.copy_block("wipe"))
        # 순서 실행에서만 보이는 실행 계획: 케이스마다 점프 VM 이 하는 일과 등록할 명령
        self.lf_plan = ttk.LabelFrame(area, text="실행 계획 (공통 규칙과 케이스별 값)", padding=(px(8), px(4)))
        hb = ttk.Frame(self.lf_plan)
        hb.pack(fill="x")
        ttk.Button(hb, text="크게 보기", command=self.open_plan_window).pack(side="right")
        body = ttk.Frame(self.lf_plan)
        body.pack(fill="both", expand=True)
        self.plan_text = self.text_box(body, wrap="none", font=self.small, height=8)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.plan_text.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=self.plan_text.xview)
        self.plan_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.plan_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        make_readonly(self.plan_text)
        self.tag_plan(self.plan_text)

    def tag_plan(self, t):
        t.tag_configure("plan_head", font=self.bold, foreground=COLORS["blue_d"])
        t.tag_configure("plan_case", font=self.bold)

    def show_plan(self, t, text):
        """실행 계획 글을 넣고 제목 줄([...])과 케이스 줄(1. 이름)을 굵게 한다."""
        set_text(t, text)
        for n, line in enumerate(text.split("\n"), 1):
            if line.startswith("["):
                t.tag_add("plan_head", "%d.0" % n, "%d.end" % n)
            elif re.match(r"^\d+\. ", line):
                t.tag_add("plan_case", "%d.0" % n, "%d.end" % n)

    def open_plan_window(self):
        """실행 계획을 큰 창으로 본다(줄을 창 너비에 맞춰 접는다)."""
        text = self.plan_text.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo(TITLE, "실행 계획이 없다. 탭 4 에서 [명령어 생성] 을 누르면 만들어진다.", parent=self.root)
            return
        top = tk.Toplevel(self.root)
        top.title("실행 계획")
        top.geometry("%dx%d" % (self.px(1100), self.px(760)))
        body = ttk.Frame(top, padding=self.px(8))
        body.pack(fill="both", expand=True)
        t = self.text_box(body, wrap="word", font=tkfont.nametofont("TkDefaultFont"), height=30)
        ys = ttk.Scrollbar(body, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=ys.set)
        t.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tag_plan(t)
        self.show_plan(t, text)
        make_readonly(t)
        ttk.Button(top, text="닫기", command=top.destroy).pack(side="right", padx=self.px(8), pady=(0, self.px(8)))
        self.plan_window = top

    def _block_panel(self, pw, title, copy_cmd, esx=False):
        px = self.px
        lf = ttk.LabelFrame(pw, text=title, padding=(px(8), px(4)))
        pw.add(lf, weight=1)
        hb = ttk.Frame(lf)
        hb.pack(fill="x")
        ttk.Button(hb, text="복사", style="Red.TButton", command=copy_cmd, width=-4).pack(side="right")   # 칸이 좁아도 호스트 선택 칸이 보이게
        if esx:
            self.esx_pick = ttk.Frame(hb)             # 시각 지정에서만 보인다 (ESX 호스트마다 블록이 다름)
            self.esx_pick.pack(side="left")
            ttk.Label(self.esx_pick, text="ESX 호스트").pack(side="left")
            self.cb_esx = ttk.Combobox(self.esx_pick, textvariable=self.v_esx_host, state="readonly", width=14)
            self.cb_esx.pack(side="left", padx=px(6))
            self.cb_esx.bind("<<ComboboxSelected>>", lambda e: self.show_esx_block())
        body = ttk.Frame(lf)
        body.pack(fill="both", expand=True, pady=(px(6), 0))
        t = self.text_box(body, wrap="none", font=self.mono_font, width=40, height=10)
        ys = ttk.Scrollbar(body, orient="vertical", command=t.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        t.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        make_readonly(t)
        t.frame = lf
        return t

    # ------------------------------------------------------------ 상태
    def _settings_changed(self, *a):
        if self.loading:            # 데이터를 다시 읽는 중에는 저장하지 않는다
            return
        self.autosave("settings")
        if hasattr(self, "t_case"):
            self.t_case.refresh()

    SAVE_NAMES = (("설정", "settings"), ("탭 1 실행 명령", "run"), ("탭 2 모니터링 명령", "mon"),
                  ("탭 3 설정 프로파일", "profiles"), ("탭 4 테스트 케이스", "cases"))

    def update_status(self):
        failed = [name for name, key in self.SAVE_NAMES if self.dirty[key]]
        tail = ""
        if self.last_saved:
            tail = "   자동 저장 %s" % self.last_saved.strftime("%H:%M:%S")
        if failed:
            tail += "   저장 실패: " + ", ".join(failed)
        self.v_status.set("데이터 폴더: %s   테스트 케이스 파일: %s%s" % (self.base, self.case_file_label(), tail))

    def autosave(self, kind):
        """설정, 탭 1, 탭 2, 탭 3, 탭 4 를 바뀔 때마다 데이터 폴더에 저장한다. 실패하면 상태 줄과 한 번의 알림으로 알린다."""
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
            elif kind == "profiles":
                core.save_profiles(self.base, self.profiles)
            else:
                core.save_cases(self.base, self.cases, core.case_file(self.settings))
            self.dirty[kind] = False
            self.last_saved = dt.datetime.now()
        except OSError as e:
            self.dirty[kind] = True
            if not self.save_warned.get(kind):
                self.save_warned[kind] = True
                name = dict((k, n) for n, k in self.SAVE_NAMES)[kind]
                where = core.cases_path(self.base, core.case_file(self.settings)) if kind == "cases" else self.base
                messagebox.showerror(TITLE, "%s 을 자동 저장하지 못함\n%s\n%s\n\n쓸 수 있는 위치인지 확인할 것. 엑셀 등이 이 파일을 열고 있으면 닫을 것.\n"
                                            "저장될 때까지 상태 줄에 '저장 실패' 가 보인다." % (name, where, e), parent=self.root)
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

    def vm_prefix(self):
        return self.settings.get("vm_prefix") or "ev"

    def finish_all_edits(self):
        for t in (getattr(self, n, None) for n in ("t_run", "t_mon", "t_prof", "t_case")):
            if t is not None:
                t.finish_edit()

    def jump_dir_text(self):
        """점프 VM 작업 폴더(화면의 칸, 비우면 ~/.tcgen)."""
        return core.jump_dir({"jump_dir": self.v_jdir.get()})

    def current_settings(self, show_error=True):
        ld = self.leads()
        if ld is None or ld[0] < 0 or ld[1] < 0:
            if show_error:
                messagebox.showerror(TITLE, "모니터링 시작, 중지 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        try:
            settle = int(self.v_settle.get())
        except ValueError:
            settle = -1
        if settle < 0:
            if show_error:
                messagebox.showerror(TITLE, "'변경 작업 뒤 잡 시작까지 최소' 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        try:
            snapgap = int(self.v_snapgap.get())
        except ValueError:
            snapgap = -1
        if snapgap < 0:
            if show_error:
                messagebox.showerror(TITLE, "'스냅샷 뒤 잡 시작까지 최소' 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        try:
            gap = int(self.v_gap.get())
        except ValueError:
            gap = -1
        if gap < 0:
            if show_error:
                messagebox.showerror(TITLE, "'케이스 사이 간격' 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        try:
            poll = int(self.v_poll.get())
        except ValueError:
            poll = 0
        if poll < 1:
            if show_error:
                messagebox.showerror(TITLE, "'잡 종료 확인 간격' 분은 1 이상의 정수로 입력", parent=self.root)
            return None
        try:
            spacing = int(self.v_spacing.get())
        except ValueError:
            spacing = 0
        if spacing < 1:
            if show_error:
                messagebox.showerror(TITLE, "'잡 시작 최소 간격' 분은 1 이상의 정수로 입력", parent=self.root)
            return None
        try:
            prep = int(self.v_prep.get())
        except ValueError:
            prep = -1
        if prep < 0:
            if show_error:
                messagebox.showerror(TITLE, "'시작 시각 준비' 분은 0 이상의 정수로 입력", parent=self.root)
            return None
        s = dict(self.settings)
        if poll != poll_minutes(s.get("seq_poll_sec", core.DEFAULT_SETTINGS["seq_poll_sec"])):
            s["seq_poll_sec"] = poll * 60
        s.update(record_csv=self.v_record.get().strip(), log_dir=self.v_log.get().strip(), vm_user=self.v_user.get().strip(),
                 mon_lead_min=ld[0], stop_lead_min=ld[1], run_mode=self.mode(), jump_log_dir=self.v_jlog.get().strip(),
                 esx_addr=self.v_addr.get().strip(), seq_settle_min=settle, seq_gap_min=gap, seq_start_spacing_min=spacing,
                 seq_prep_lead_min=prep, jump_dir=self.v_jdir.get().strip(), seq_snap_gap_min=snapgap,
                 seq_guest_cmd=" ".join(self.v_gcmd.get().split()))
        return s

    # ------------------------------------------------------------ 테스트 케이스 파일 (불러오기, 다른 이름으로 저장, 자동 백업)
    CASE_FILE_TYPES = [("테스트 케이스 파일", "*.csv *.json"), ("CSV", "*.csv"), ("JSON", "*.json"), ("모든 파일", "*.*")]

    def case_file_label(self):
        cf = core.case_file(self.settings)
        return os.path.basename(cf) if cf else "기본 (%s)" % core.CASES_FILE

    def refresh_case_file_box(self):
        """테스트 케이스 파일 칸에 지금 쓰는 파일의 경로를 보인다."""
        cf = core.case_file(self.settings)
        self.v_cfile.set(cf if cf else "%s   (지정하지 않음: 데이터 폴더에 자동 저장)" % core.cases_path(self.base))

    def _save_before_file_change(self):
        """지금 파일의 표와 설정을 저장한다. 저장하지 못하면 알리고 False (파일을 바꾸면 고친 내용을 잃기 때문에 바꾸지 않는다)."""
        self.finish_all_edits()
        st = self.current_settings()
        if st is None:                      # 설정 영역의 분 칸이 숫자가 아님
            return False
        self.settings = st
        if not self.save_everything():
            messagebox.showerror(TITLE, "지금 테스트 케이스 표를 저장하지 못해 파일을 바꾸지 않았음.\n쓸 수 있는 위치인지 확인할 것\n%s"
                                 % core.cases_path(self.base, core.case_file(self.settings)), parent=self.root)
            return False
        return True

    def use_case_file(self, path, cases=None, note=""):
        """테스트 케이스 파일을 path 로 바꾼다(빈 값 = 기본 파일). cases 를 주면 그 표를 path 에 먼저 저장한다(다른 이름으로 저장).
        그 뒤 path 의 표를 화면에 읽고, 이 파일에서 마지막에 쓰던 기록 CSV 와 로그 폴더로 바꾼다. 바꿨으면 True."""
        try:
            if cases is not None:
                core.save_cases(self.base, cases, path)
            new_cases = core.load_cases(self.base, path)
            st = core.switch_case_file(self.base, self.settings, path)
            core.save_settings(self.base, core.resolve_paths(self.base, st))
        except (OSError, ValueError) as e:
            messagebox.showerror(TITLE, "테스트 케이스 파일을 바꾸지 못함\n%s\n%s" % (path or core.cases_path(self.base), e), parent=self.root)
            return False
        self.settings = st
        self.loading = True
        try:
            self.cases[:] = new_cases
            paths = core.resolve_paths(self.base, st)
            self.v_record.set(paths["record_csv"])
            self.v_log.set(paths["log_dir"])
            self.refresh_case_file_box()
        finally:
            self.loading = False
        self.sched = []
        self.t_case.refresh(0)
        self.show_messages("테스트 케이스 파일: %s. 탭 4 에서 [명령어 생성] 을 누르면 여기에 결과가 나온다." % self.case_file_label(), [], [])
        self.update_status()
        if note:
            self.v_status.set(note)
        return True

    def _case_file_start_dir(self):
        cf = core.case_file(self.settings)
        d = os.path.dirname(cf) if cf else (core.load_config().get("case_file_dir") or "")
        return d if d and os.path.isdir(d) else self.base

    def _remember_case_file_dir(self, path):
        try:
            cfg = core.load_config()
            cfg["case_file_dir"] = os.path.dirname(os.path.abspath(path))
            core.save_config(cfg)
        except OSError:
            pass

    def load_case_file(self):
        """[불러오기]: 고른 파일의 테스트 케이스 표를 불러오고, 그 뒤로는 그 파일에 자동 저장한다."""
        if not self._save_before_file_change():
            return
        p = filedialog.askopenfilename(parent=self.root, title="불러올 테스트 케이스 파일 선택", initialdir=self._case_file_start_dir(),
                                       filetypes=self.CASE_FILE_TYPES)
        if not p:
            return
        p = os.path.normpath(p)
        try:
            n = len(core.load_cases(self.base, p))       # 바꾸기 전에 읽을 수 있는 파일인지 본다
        except (OSError, ValueError) as e:
            messagebox.showerror(TITLE, "이 파일은 불러올 수 없음\n%s\n\n%s\n\n[다른 이름으로 저장] 으로 만든 테스트 케이스 파일(.csv, .json)을 고를 것"
                                 % (p, e), parent=self.root)
            return
        if self.use_case_file(p, note="불러옴: %s (케이스 %d개). 고치면 이 파일에 자동 저장된다." % (p, n)):
            self._remember_case_file_dir(p)

    def save_case_file_as(self):
        """[다른 이름으로 저장]: 지금 표를 고른 파일에 저장하고, 그 뒤로는 그 파일에 자동 저장한다. 이전 파일은 마지막 상태로 남는다."""
        if not self._save_before_file_change():
            return
        cf = core.case_file(self.settings)
        name = os.path.basename(cf) if cf else "test_cases_%s.csv" % dt.datetime.now().strftime("%Y%m%d")
        p = filedialog.asksaveasfilename(parent=self.root, title="테스트 케이스 표를 저장할 파일 (이 파일에 자동 저장하게 된다)",
                                         initialdir=self._case_file_start_dir(), initialfile=name, defaultextension=".csv",
                                         filetypes=self.CASE_FILE_TYPES)
        if not p:
            return
        p = os.path.normpath(p)
        if self.use_case_file(p, cases=list(self.cases), note="저장함: %s (케이스 %d개). 고치면 이 파일에 자동 저장된다." % (p, len(self.cases))):
            self._remember_case_file_dir(p)

    AUTO_BACKUP_CHECK_MS = 30 * 60 * 1000

    def auto_backup_tick(self):
        """하루 두 번(오전, 오후) 데이터 파일을 데이터 폴더의 backup/auto 에 복사한다. 생성기가 켜져 있는 동안 30분마다 확인한다."""
        try:
            if not any(self.dirty.values()):
                dest = core.auto_backup(self.base, self.settings)
                if dest:
                    self.last_auto_backup = dest
        except OSError:
            pass
        try:
            self.root.after(self.AUTO_BACKUP_CHECK_MS, self.auto_backup_tick)
        except tk.TclError:
            pass

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

    def _backup_start_dir(self):
        d = (core.load_config().get("backup_dir") or "").strip()
        return d if d and os.path.isdir(d) else self.base

    def backup(self):
        """데이터 파일과 testcases 폴더를 고른 폴더 아래 tcgen2_backup_<년월일시분초> 로 복사한다. 고른 폴더는 다음에도 쓴다."""
        self.save_everything()
        parent = filedialog.askdirectory(parent=self.root, mustexist=False, initialdir=self._backup_start_dir(),
                                         title="백업을 저장할 폴더 선택 (그 안에 tcgen2_backup_년월일시분초 폴더를 만든다)")
        if not parent:
            return
        parent = os.path.normpath(parent)
        try:
            dest, done = core.backup_to(self.base, parent, s=self.settings)
        except OSError as e:
            messagebox.showerror(TITLE, "백업하지 못함\n%s\n%s" % (parent, e), parent=self.root)
            return
        if not done:
            try:
                os.rmdir(dest)
            except OSError:
                pass
            messagebox.showinfo(TITLE, "백업할 파일이 없음. 데이터 폴더에 저장된 파일이 없음\n%s" % self.base, parent=self.root)
            return
        try:
            cfg = core.load_config()
            cfg["backup_dir"] = parent
            core.save_config(cfg)
        except OSError:
            pass
        if messagebox.askyesno(TITLE, "백업함: %s\n\n%s\n\n복원은 [백업 불러오기] 에서 이 폴더를 고른다.\n백업 폴더를 열까요?"
                               % (dest, "\n".join(done)), parent=self.root):
            open_folder(dest)

    def restore_backup(self):
        """고른 백업 폴더의 데이터 파일로 현재 데이터를 바꾼다. 바꾸기 전 현재 파일은 데이터 폴더 backup 아래에 복사한다."""
        self.save_everything()
        src = filedialog.askdirectory(parent=self.root, mustexist=True, initialdir=self._backup_start_dir(),
                                      title="불러올 백업 폴더 선택 (tcgen2_backup_년월일시분초 폴더 또는 예전 생성기 폴더)")
        if not src:
            return
        src = os.path.normpath(src)
        if not core.has_data(src):
            messagebox.showerror(TITLE, "이 폴더에는 tcgen 데이터 파일이 없음\n%s\n\n필요한 파일: %s"
                                 % (src, ", ".join(core.DATA_FILES)), parent=self.root)
            return
        got = core.backup_case_file(src)
        extra = ""
        if got and got[1]:
            extra = ("\n\n이 백업에는 테스트 케이스 파일의 사본이 있다. 아래 파일도 백업한 때의 내용으로 바꾼다(바꾸기 전 내용은 같은 "
                     "backup 폴더에 복사).\n%s" % got[0])
        elif got:
            extra = ("\n\n이 백업에는 테스트 케이스 파일의 사본이 없다. 탭 4 의 표는 아래 파일의 지금 내용 그대로다.\n%s" % got[0])
        if not messagebox.askyesno(TITLE, "이 백업으로 현재 데이터를 바꿀까요?\n\n백업 폴더: %s\n데이터 폴더: %s\n\n"
                                          "바꾸기 전 현재 파일은 데이터 폴더의 backup 아래에 복사해 둔다. 테스트 기록, 로그 파일은 같은 이름이 "
                                          "없을 때만 복사한다.%s" % (src, self.base, extra), parent=self.root):
            return
        try:
            keep, done = core.restore_from(src, self.base)
        except (OSError, ValueError) as e:
            messagebox.showerror(TITLE, "백업을 불러오지 못함\n%s" % e, parent=self.root)
            return
        self.reload_data()
        messagebox.showinfo(TITLE, "불러옴: %s\n\n%s\n\n불러오기 전 데이터: %s" % (src, "\n".join(done), keep), parent=self.root)

    def open_data_dir(self):
        open_folder(self.base)

    def change_data_dir(self):
        """데이터 폴더를 바꾸고 그 경로를 config.json 에 적는다. 다음 실행부터 그 폴더를 쓴다."""
        self.save_everything()
        new = filedialog.askdirectory(parent=self.root, mustexist=False, initialdir=os.path.dirname(self.base),
                                      title="새 데이터 폴더 선택")
        if not new:
            return
        new = os.path.normpath(new)
        if core.same_dir(new, self.base):
            return
        if core._inside(new, self.prog_dir):
            messagebox.showerror(TITLE, "생성기 폴더 안은 데이터 폴더로 쓸 수 없음. 다시 빌드할 때 지워질 수 있다.\n%s" % new,
                                 parent=self.root)
            return
        if core.has_data(new):
            if not messagebox.askyesno(TITLE, "고른 폴더에 이미 tcgen 데이터가 있음.\n%s\n\n그 데이터를 불러와 쓸까요? "
                                              "지금 데이터는 지금 폴더에 그대로 남는다." % new, parent=self.root):
                return
            copy_current = False
        else:
            if not messagebox.askyesno(TITLE, "지금 데이터를 새 폴더로 복사하고 새 폴더를 데이터 폴더로 쓸까요?\n\n지금: %s\n새 폴더: %s\n\n"
                                              "지금 폴더의 파일은 지우지 않는다." % (self.base, new), parent=self.root):
                return
            copy_current = True
        try:
            done = core.change_data_dir(self.base, new, copy_current=copy_current)
        except OSError as e:
            messagebox.showerror(TITLE, "데이터 폴더를 바꾸지 못함\n%s\n%s" % (new, e), parent=self.root)
            return
        old = self.base
        self.base = new
        self.reload_data()
        messagebox.showinfo(TITLE, "데이터 폴더를 바꿈\n\n이전: %s\n지금: %s\n%s\n\n위치는 %s 에 저장되어 다음 실행에도 쓴다."
                            % (old, new, ("\n복사한 항목:\n" + "\n".join(done)) if done else "",
                               os.path.join(core.config_dir(), core.CONFIG_FILE)), parent=self.root)

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
        i = table.selected_index()
        i = len(table.rows) if i is None else i + 1
        table.rows.insert(i, row)
        self.autosave(kind)
        table.refresh(i)
        table.start_edit(str(i), 0)

    def del_cmd_row(self, table, kind):
        table.finish_edit()
        i = table.selected_index()
        if i is None:
            return
        name = table.rows[i].get("name", "")
        used = self.cases_using(kind, name) if name else []
        msg = "'%s' 행을 삭제할까요?" % (name or "%d행" % (i + 1))
        if used:
            msg += "\n\n탭 4 케이스 %d개가 이 명령을 쓰고 있음: %s" % (len(used), ", ".join(used[:5]))
        if not messagebox.askyesno(TITLE, msg, parent=self.root):
            return
        del table.rows[i]
        self.autosave(kind)
        table.refresh(i)
        self.t_case.refresh()

    def cases_using(self, kind, name):
        out = []
        for c in self.cases:
            vms = c.get("vms") or []
            if kind == "run" and any(v.get("job") == name for v in vms):
                out.append(c.get("name") or "?")
            if kind == "mon" and (name in (c.get("esx_monitors") or []) or any(name in (v.get("monitors") or []) for v in vms)):
                out.append(c.get("name") or "?")
            if kind == "prof" and (name in (c.get("esx_profiles") or []) or any(name in (v.get("profiles") or []) for v in vms)):
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

    def commit_run(self, iid, key, text):
        i = int(iid)
        row = self.run_cmds[i]
        if key == "name":
            name = self._name_check(self.t_run, i, text)
            if name is None:
                return
            old = row.get("name", "")
            row["name"] = name
            if old and name != old:
                for c in self.cases:
                    for v in c.get("vms") or []:
                        if v.get("job") == old:
                            v["job"] = name or NONE_JOB
        elif key == "cmd":
            row["cmd"] = text.strip()
            self._cmd_warn(row["cmd"], "경로/실행 명령")
        elif key == "proc":
            errs = core.check_proc_name(text.strip(), "프로세스 이름")
            if errs:
                messagebox.showerror(TITLE, "\n".join(errs), parent=self.root)
                return
            row["proc"] = text.strip()
        else:
            row[key] = text.strip()
        self.autosave("run")
        if key == "name":
            self.autosave("cases")          # 이름을 바꾸면 탭 4 의 잡 칸도 따라 바뀜
        self.t_run.refresh(i)
        self.t_case.refresh()

    def commit_mon(self, iid, key, text):
        i = int(iid)
        row = self.mon_cmds[i]
        if key == "name":
            name = self._name_check(self.t_mon, i, text)
            if name is None:
                return
            old = row.get("name", "")
            row["name"] = name
            if old and name != old:
                for c in self.cases:
                    c["esx_monitors"] = [name if n == old else n for n in (c.get("esx_monitors") or []) if (n != old or name)]
                    for v in c.get("vms") or []:
                        v["monitors"] = [name if n == old else n for n in (v.get("monitors") or []) if (n != old or name)]
        elif key == "where":
            old = (row.get("where") or "VM").upper()
            row["where"] = text
            if old != text.upper() and row.get("name"):
                used = self.cases_using("mon", row["name"])
                for c in self.cases:          # VM 모니터링과 ESX 모니터링은 고르는 곳이 달라 선택을 해제한다
                    c["esx_monitors"] = [n for n in (c.get("esx_monitors") or []) if n != row["name"]]
                    for v in c.get("vms") or []:
                        v["monitors"] = [n for n in (v.get("monitors") or []) if n != row["name"]]
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
            self.autosave("cases")          # 탭 4 의 모니터링 선택도 따라 바뀜
        self.t_mon.refresh(i)
        self.t_case.refresh()

    # ------------------------------------------------------------ 탭 3 설정 프로파일
    def prof_value(self, iid, key):
        i = int(iid)
        if i >= len(self.profiles):
            return ""
        p = self.profiles[i]
        if key == "target":
            return core.PROFILE_TARGET_LABELS["ESX" if p.get("target") == "ESX" else "VM"]
        return p.get(key, "")

    def prof_display(self, iid, key):
        if key != "text":
            return None
        i = int(iid)
        lines = [x.strip() for x in (self.profiles[i].get("text") or "").split("\n")] if i < len(self.profiles) else []
        lines = [x for x in lines if x and not x.startswith("#")]
        return ";  ".join(lines) if lines else "(비어 있음. 두 번 눌러 입력)"

    def add_prof_row(self):
        t = self.t_prof
        t.finish_edit()
        i = t.selected_index()
        i = len(self.profiles) if i is None else i + 1
        self.profiles.insert(i, core.new_profile())
        self.autosave("profiles")
        t.refresh(i)
        t.start_edit(str(i), 0)

    def add_default_profiles(self):
        """표에 없는 이름의 기본 프로파일만 끝에 추가한다. 이름이 같은 행은 내용이 달라도 그대로 둔다."""
        t = self.t_prof
        t.finish_edit()
        added = core.add_default_profiles(self.profiles)
        if not added:
            messagebox.showinfo(TITLE, "기본 프로파일 %d개가 이미 표에 모두 있음.\n\n"
                                       "내용을 처음 값으로 복원하려면 그 행을 삭제한 뒤 이 버튼을 다시 누른다."
                                % len(core.DEFAULT_PROFILES), parent=self.root)
            return
        self.autosave("profiles")
        t.refresh(len(self.profiles) - len(added))
        messagebox.showinfo(TITLE, "기본 프로파일 %d개를 표 끝에 추가함:\n%s\n\n이름이 같은 행은 그대로 두었다."
                            % (len(added), ", ".join(added)), parent=self.root)

    def del_prof_row(self):
        t = self.t_prof
        t.finish_edit()
        i = t.selected_index()
        if i is None:
            return
        name = self.profiles[i].get("name", "")
        used = self.cases_using("prof", name) if name else []
        msg = "'%s' 프로파일을 삭제할까요?" % (name or "%d행" % (i + 1))
        if used:
            msg += "\n\n탭 4 케이스 %d개가 이 프로파일을 쓰고 있음: %s" % (len(used), ", ".join(used[:5]))
        if not messagebox.askyesno(TITLE, msg, parent=self.root):
            return
        del self.profiles[i]
        self.autosave("profiles")
        t.refresh(i)
        self.t_case.refresh()

    def _prof_unselect(self, name):
        """대상이 바뀐 프로파일은 고르는 곳(케이스 행, VM 행)이 달라지므로 케이스의 선택을 해제한다."""
        for c in self.cases:
            c["esx_profiles"] = [n for n in (c.get("esx_profiles") or []) if n != name]
            for v in c.get("vms") or []:
                v["profiles"] = [n for n in (v.get("profiles") or []) if n != name]

    def commit_prof(self, iid, key, text):
        i = int(iid)
        row = self.profiles[i]
        t = text.strip()
        touched_cases = False
        if key == "name":
            if t and not core.NAME_RE.match(t):
                messagebox.showerror(TITLE, "프로파일 이름은 영문, 숫자, . _ - 만 사용: '%s'" % t, parent=self.root)
                return
            if t and any(j != i and r.get("name", "").strip() == t for j, r in enumerate(self.profiles)):
                messagebox.showerror(TITLE, "같은 프로파일 이름이 이미 있음: '%s'" % t, parent=self.root)
                return
            old = row.get("name", "")
            row["name"] = t
            if old and t != old:
                for c in self.cases:
                    c["esx_profiles"] = [t if n == old else n for n in (c.get("esx_profiles") or []) if (n != old or t)]
                    for v in c.get("vms") or []:
                        v["profiles"] = [t if n == old else n for n in (v.get("profiles") or []) if (n != old or t)]
                touched_cases = True
        elif key == "target":
            new = "ESX" if t == core.PROFILE_TARGET_LABELS["ESX"] else "VM"
            if new != row.get("target"):
                _, errs = core.parse_profile_text(row.get("text"), new)
                if errs and not messagebox.askyesno(
                        TITLE, "대상을 %s (으)로 바꾸면 지금 설정 내용이 형식에 맞지 않음\n\n%s\n\n그래도 바꿀까요? 바꾼 뒤 설정 내용을 "
                               "고쳐야 한다." % (core.PROFILE_TARGET_LABELS[new], "\n".join(errs[:8])), parent=self.root):
                    return
                row["target"] = new
                used = self.cases_using("prof", row["name"]) if row.get("name") else []
                if used:
                    self._prof_unselect(row["name"])
                    touched_cases = True
                    messagebox.showinfo(TITLE, "대상이 바뀌어 케이스 %d개에서 '%s' 선택을 해제함: %s"
                                        % (len(used), row["name"], ", ".join(used[:5])), parent=self.root)
        else:
            row[key] = " ".join(t.split())
        self.autosave("profiles")
        if touched_cases:
            self.autosave("cases")
        self.t_prof.refresh(i)
        self.t_case.refresh()

    def edit_profile_text(self, iid):
        """설정 내용 칸: 여러 줄 입력 창. 한 줄에 '키 = 값' 하나. [확인] 에서 형식을 검사한다."""
        self.t_prof.finish_edit()
        i = int(iid)
        if i >= len(self.profiles):
            return
        row = self.profiles[i]
        esx = row.get("target") == "ESX"
        C, px = COLORS, self.px
        top = tk.Toplevel(self.root, bg=C["card"])
        top.title("설정 내용 - %s (대상 %s)" % (row.get("name") or "%d행" % (i + 1), core.PROFILE_TARGET_LABELS["ESX" if esx else "VM"]))
        top.transient(self.root)
        fr = ttk.Frame(top, padding=(px(12), px(10), px(12), px(10)))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="한 줄에 '키 = 값' 하나를 적는다. # 으로 시작하는 줄은 설명이다.", style="Bold.TLabel").pack(anchor="w")
        if esx:
            tip = ("ESX 호스트 고급 설정: /구역/옵션 = 값 또는 구역.옵션 = 값. 예: Numa.LocalityWeightActionAffinity = 0\n"
                   "재부팅해야 적용되는 호스트 설정은 넣지 않는다(수동으로 적용).")
        else:
            tip = ("VM 설정(vmx 키): vmx 파일의 줄(키 = \"값\")을 그대로 붙여넣어도 된다. 예: sched.mem.pin = TRUE\n"
                   "값을 비우면 그 키의 줄을 vmx 에서 지운다. 예: numa.nodeAffinity =")
        ttk.Label(fr, text=tip, style="Hint.TLabel", justify="left").pack(anchor="w", pady=(px(2), px(6)))
        body = ttk.Frame(fr)
        body.pack(fill="both", expand=True)
        txt = self.text_box(body, wrap="none", font=self.mono_font, width=84, height=14, undo=True)
        ys = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        txt.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        txt.insert("1.0", (row.get("text") or "").rstrip("\n"))
        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(px(10), 0))

        def ok():
            lines = [x.rstrip() for x in txt.get("1.0", "end-1c").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
            text = "\n".join(lines).strip("\n")
            _, errs = core.parse_profile_text(text, row.get("target"))
            if errs:
                messagebox.showerror(TITLE, "형식에 맞지 않는 줄 %d건. 고친 뒤 다시 [확인] 을 누를 것.\n\n%s%s"
                                     % (len(errs), "\n".join(errs[:10]), "\n..." if len(errs) > 10 else ""), parent=top)
                return
            row["text"] = (text + "\n") if text else ""
            top.destroy()
            self.autosave("profiles")
            self.t_prof.refresh(i)

        ttk.Button(bf, text="취소", command=top.destroy).pack(side="right")
        ttk.Button(bf, text="확인", style="Blue.TButton", command=ok).pack(side="right", padx=(0, px(6)))
        ttk.Label(bf, text="Ctrl+Enter 로도 확인", style="Hint.TLabel").pack(side="left")
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Control-Return>", lambda e: (ok(), "break")[1])
        self.prof_win, self.prof_text, self.prof_ok = top, txt, ok
        self._center(top)
        top.grab_set()
        txt.focus_set()

    # ------------------------------------------------------------ 탭 4 (케이스 행 iid = c<케이스 순번>, VM 행 = c<순번>v<VM 순번>)
    def calc_schedule(self):
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        # 시각 지정: 적용 체크한 케이스의 자동 모니터링 중지 시각을 고정해 둔다. 다음 케이스가 바뀌어도 등록한 기록이 달라지지 않게 한다
        if not self.loading and self.mode() == "time" and core.freeze_applied(self.cases, ld[0], ld[1]):
            self.mark_cases()
        self.sched = core.case_schedule(self.cases, ld[0], ld[1])

    LOCKED_TEXT = ("적용 체크한 케이스의 간격, 잡 시작, 모니터링 중지는 고칠 수 없음.\n\n이미 등록한 케이스의 기록이라 고정해 둔다. "
                   "다른 케이스를 추가하거나 순서를 바꿔도 이 값은 바뀌지 않는다.\n고치려면 적용 체크를 먼저 끄고, 고친 뒤 다시 체크할 것.")

    LOCKED_TEXT_SEQ = ("적용 체크한 케이스의 시작 시각은 고칠 수 없음.\n\n이미 실행한 케이스의 기록이라 고정해 둔다. "
                       "고치려면 적용 체크를 먼저 끄고, 고친 뒤 다시 체크할 것.")

    def is_locked(self, c):
        """시각 지정에서 적용 체크한 케이스의 시각 칸(간격, 잡 시작, 모니터링 중지)은 고정한다."""
        return bool(c and c.get("applied")) and self.mode() == "time"

    def seq_locked(self, c):
        """순서 실행에서 적용 체크한 케이스의 시작 시각 칸은 고정한다(그 케이스의 잡 시작 기록이다)."""
        return bool(c and c.get("applied")) and self.mode() == "seq"

    def locked_notice(self, iid=None):
        messagebox.showinfo(TITLE, self.LOCKED_TEXT_SEQ if self.mode() == "seq" else self.LOCKED_TEXT, parent=self.root)

    @staticmethod
    def parse_iid(iid):
        m = re.match(r"^c(\d+)(?:v(\d+))?$", iid or "")
        if not m:
            return None, None
        return int(m.group(1)), (int(m.group(2)) if m.group(2) is not None else None)

    def case_row_ids(self):
        out = []
        for i, c in enumerate(self.cases):
            out.append("c%d" % i)
            out += ["c%dv%d" % (i, k) for k in range(len(c.get("vms") or []))]
        return out

    def _get(self, iid):
        """iid 의 (케이스 순번, VM 순번 또는 None, 케이스, VM 또는 None). 없는 행이면 케이스가 None."""
        i, k = self.parse_iid(iid)
        if i is None or i >= len(self.cases):
            return None, None, None, None
        c = self.cases[i]
        if k is None:
            return i, None, c, None
        vms = c.get("vms") or []
        return (i, k, c, vms[k]) if k < len(vms) else (None, None, None, None)

    def case_value(self, iid, key):
        i, k, c, v = self._get(iid)
        if c is None:
            return ""
        if k is None:
            return c.get(key, "") if key in ("name", "desc", "esx", "interval", "start", "group", "end_time", "max_wait",
                                             "seq_start") else ""
        if key == "power":
            return core.POWER_LABELS[v.get("power") or "on"]
        return v.get(key, "") if key in ("name", "job") else ""

    def case_kind(self, iid, col):
        i, k, c, v = self._get(iid)
        key = col["key"]
        if c is None:
            return "readonly"
        if k is None:
            if key in ("interval", "start") and self.is_locked(c):
                return "popup"              # 고치는 칸 대신 고정 안내(locked_notice)를 보인다
            if key == "seq_start" and self.seq_locked(c):
                return "popup"
            return {"name": "entry", "desc": "entry", "esx": "entry", "mon": "popup", "interval": "entry", "start": "entry",
                    "mon_stop": "popup", "group": "entry", "end_time": "entry", "prof": "popup",
                    "max_wait": "entry", "seq_start": "entry"}.get(key, "readonly")
        return {"name": "entry", "job": "combo", "power": "combo", "mon": "popup", "end_ref": "popup",
                "prof": "popup"}.get(key, "readonly")

    def case_check(self, iid):
        i, k, c, v = self._get(iid)
        if c is None or k is not None:
            return None
        return bool(c.get("applied"))

    def case_tags(self, iid):
        i, k, c, v = self._get(iid)
        if c is None:
            return ()
        tags = ["case" if k is None else "vm"]
        if c.get("applied"):
            tags.append("applied")
        return tags

    def case_display(self, iid, key):
        i, k, c, v = self._get(iid)
        if c is None:
            return ""
        if k is not None:
            if key == "name":
                return "   └ " + (v.get("name") or "(이름 없음)")
            if key == "job":
                return v.get("job") or NONE_JOB
            if key == "mon":
                return ", ".join(v.get("monitors") or []) or NONE_JOB
            if key == "end_ref":
                return "기준" if v.get("end_ref") else ""
            if key == "prof":
                return ", ".join(v.get("profiles") or []) or NONE_JOB
            if key == "power":
                return core.POWER_LABELS[v.get("power") or "on"]
            return ""
        if key == "power":
            return ""
        if key == "prof":
            ep = c.get("esx_profiles") or []
            return (", ".join(ep) + " (ESX)") if ep else NONE_JOB
        if key == "max_wait":
            return c.get("max_wait") or "제한 없음"
        if key == "seq_start":
            return self.seq_start_text(c)
        s = self.sched[i] if i < len(self.sched) else {}
        has_mon = core.case_has_monitors(c)
        if key in ("job", "end_ref"):
            return ""
        if key == "end_time":
            return c.get("end_time") or "자동"
        if key == "mon":
            em = c.get("esx_monitors") or []
            return (", ".join(em) + " (ESX)") if em else NONE_JOB
        if key == "mon_start":
            return core.fmt_short(s["mon_start"]) if (has_mon and s.get("mon_start")) else "-"
        if key == "mon_stop":
            if not has_mon:
                return "-"
            mode = c.get("stop_mode") or "auto"
            if mode == "none":
                return "중지 없음"
            if s.get("stop_error") or not s.get("mon_stop"):
                return "확인 필요"
            return core.fmt_short(s["mon_stop"]) + (" (직접)" if mode == "manual" else " (자동)")
        return None

    SEQ_UNSET = "입력 필요"
    SEQ_AUTO = "자동 (잡 확인)"

    @staticmethod
    def seq_is_auto(c):
        return (c.get("seq_start") or "").strip().lower() in ("자동", "auto")

    @staticmethod
    def case_has_job(c):
        """잡이 있는 케이스인가. 잡이 없는 케이스(설정과 VM 전원만 맞춤)는 순서 실행에서 시작 시각을 쓰지 않는다."""
        return any(core._has_job(v) for v in (c.get("vms") or []))

    def seq_start_text(self, c):
        """순서 실행의 시작 시각 칸에 보이는 글. 시작 시각을 직접 적는 것이 기본이다(사용자 2026-11-17 "프로세스 체크해서 다음 작업 실행하는
        기능은 부하때문에 주로 안쓸꺼같아 ... 기존 처럼 내가 잡 시작을 테스트케이스 기록에 남길께"). 빈 칸은 아직 적지 않은 것이고,
        프로세스를 확인하며 이어서 실행하는 방식은 '자동' 이라고 적었을 때만 쓴다. 적용 체크한 케이스와 잡이 없는 케이스(시작 시각을
        쓰지 않는다)의 빈 칸은 그대로 비워 보인다."""
        v = (c.get("seq_start") or "").strip()
        if not v:
            return "" if (c.get("applied") or not self.case_has_job(c)) else self.SEQ_UNSET
        if self.seq_is_auto(c):
            return self.SEQ_AUTO if self.case_has_job(c) else "자동"
        return v

    def seq_unset_names(self, cases, picked):
        """고른 케이스(picked: 순번의 집합) 가운데 잡이 있는데 시작 시각이 비어 있는 케이스의 이름. 이런 케이스가 있으면 [명령어 생성] 이
        만들지 않는다. 잡이 없는 케이스(설정과 VM 전원만 맞춤)는 시작 시각을 쓰지 않으므로 넣지 않는다."""
        return [c.get("name") or "%d번째 케이스" % (i + 1) for i, c in enumerate(cases)
                if i in picked and self.case_has_job(c) and not (c.get("seq_start") or "").strip()]

    def seq_auto_names(self, cases, picked):
        """고른 케이스 가운데 잡이 있고 시작 시각이 '자동' 인 케이스의 이름(생성할 때 잡 실행 중 VM 접속을 경고로 알린다)."""
        return [c.get("name") or "%d번째 케이스" % (i + 1) for i, c in enumerate(cases)
                if i in picked and self.case_has_job(c) and self.seq_is_auto(c)]

    def mark_cases(self):
        """탭 4 이 바뀔 때마다 test_cases.json 에 저장한다(적용 체크 포함)."""
        self.autosave("cases")

    def toggle_applied(self, iid):
        self.t_case.finish_edit()
        i, k, c, v = self._get(iid)
        if c is None or k is not None:
            return
        c["applied"] = not c.get("applied")
        note = ""
        if not c["applied"]:
            c["applied_stop"] = ""          # 고정해 둔 자동 중지 시각을 지운다. 다시 체크하면 그때의 표로 다시 고정한다
        elif self.mode() == "seq":
            note = self.fix_seq_start(i, c)
        self.mark_cases()
        self.t_case.refresh(iid)
        if note:
            messagebox.showinfo(TITLE, note, parent=self.root)

    def fix_seq_start(self, i, c):
        """순서 실행에서 적용 체크할 때: 시작 시각 칸의 +간격을 날짜 시각으로 바꿔 고정한다(사용자 결정 2026-11-17. 적용 체크한 케이스의
        칸이 +8:00 으로 남으면 표에서 그 케이스의 잡 시작을 알 수 없다). 값은 마지막 [명령어 생성] 의 계획 시각이다: 이 창에서 방금
        만든 결과, 없으면 테스트 기록 CSV 의 마지막 생성 기록(그때 적은 +간격이 지금 칸과 같을 때), 그것도 없으면 지금 표로 계산한 값.
        바꾸지 못했으면 알릴 글을 돌려준다(바꿨거나 +간격이 아니면 빈 글)."""
        raw = (c.get("seq_start") or "").strip()
        try:
            off = core.parse_seq_offset(raw)
        except ValueError:
            off = None
        if off is None:
            return ""
        name = (c.get("name") or "").strip()
        at = None
        res = self.result
        if res and res.get("mode") == "seq":
            for pc in res.get("cases") or []:
                if pc.get("name") == name and pc.get("start_at") and (pc["case"].get("seq_start") or "").strip() == raw:
                    at = pc["start_at"]
        if at is None:
            st = self.current_settings(show_error=False) or self.settings
            rows = core.read_record_rows(core.resolve_paths(self.base, st)["record_csv"])
            at = core.seq_start_from_record(rows, name, raw)
        if at is None:
            at = core.seq_start_by_table(self.cases, i)
        if at is None:
            return ("케이스 %s 의 시작 시각 %s 을(를) 날짜 시각으로 바꾸지 못했음.\n\n같은 ESX 호스트의 앞 케이스 시작 시각을 알 수 없음"
                    "(앞 케이스가 없거나, 자동이거나, 날짜가 없음).\n적용 체크를 끄고 시작 시각 칸에 날짜 시각을 직접 적은 뒤 다시 체크할 것."
                    % (name or "(이름 없음)", raw))
        c["seq_start"] = core.fmt_dt(at)
        return ""

    def toggle_end_ref(self, iid):
        """VM 행의 종료 기준 VM 칸: 누를 때마다 지정, 해제."""
        self.t_case.finish_edit()
        i, k, c, v = self._get(iid)
        if c is None or k is None:
            return
        v["end_ref"] = not v.get("end_ref")
        self.mark_cases()
        self.t_case.refresh(iid)

    def commit_case(self, iid, key, text):
        i, k, c, v = self._get(iid)
        if c is None:
            return
        t = text.strip()
        if k is None and key in ("interval", "start") and self.is_locked(c):
            self.locked_notice()
            return
        if k is None and key == "seq_start" and self.seq_locked(c):
            self.locked_notice()
            return
        if k is None:
            if key in ("name", "esx"):
                if t and not core.NAME_RE.match(t):
                    if key == "name":
                        msg = ("케이스 이름은 영문, 숫자, . _ - 만 쓸 수 있음: '%s'\n\n공백, 한글, 괄호 같은 글자는 쓸 수 없다. 케이스 이름은 "
                               "VM 의 crontab 줄과 점프 VM 의 진행 파일에 그대로 들어가기 때문이다. 한글 설명은 '설명' 칸에 적는다." % t)
                    else:
                        msg = "ESX 호스트 이름은 영문, 숫자, . _ - 만 쓸 수 있음: '%s'" % t
                    messagebox.showerror(TITLE, msg, parent=self.root)
                    return
                if key == "esx":
                    core.rename_vms_for_esx(c, c.get("esx", ""), t, self.vm_prefix())
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
                # 간격을 윗줄과의 차이로 맞춘다. 윗줄이 적용 체크한 케이스이면 간격을 그대로 둔다(지난 케이스와의 차이는 며칠이라, 그 값을
                # 간격에 넣으면 마지막 케이스의 자동 모니터링 중지 시각이 며칠 뒤가 된다)
                core.fit_interval(self.cases, i)
                core.recalc_starts(self.cases, i + 1)
            elif key == "max_wait":
                try:
                    mw = core.parse_interval(t)
                except ValueError:
                    messagebox.showerror(TITLE, "최대 대기는 시간:분 으로 입력 (예: 12:00, 8). 비우면 제한 없음: '%s'" % t,
                                         parent=self.root)
                    return
                c["max_wait"] = core.fmt_interval(mw) if mw else ""
            elif key == "seq_start":
                try:
                    if t.lower() in ("자동", "auto") or t == self.SEQ_AUTO:
                        c["seq_start"] = "자동"         # 프로세스를 확인하며 앞 케이스가 끝난 뒤 이어서 실행(잡 실행 중 VM 접속)
                    elif t in ("", "-", self.SEQ_UNSET):
                        c["seq_start"] = ""             # 아직 적지 않음: [명령어 생성] 이 알린다
                    elif core.SEQ_START_RE.match(t):
                        # 시:분만 적으면 지금부터 처음 오는 그 시각으로 날짜를 붙여 저장한다(표에 날짜가 남게)
                        c["seq_start"] = core.fmt_dt(core.parse_seq_start(t))
                    else:
                        c["seq_start"] = core.fmt_seq_start(t)
                except ValueError as ex:
                    messagebox.showerror(TITLE, "시작 시각 칸. %s\n\n잡을 시작할 시각을 적는다: 2026-11-20 10:00:00 또는 10:00(지금부터 처음 "
                                         "오는 그 시각), 앞 케이스 시작 시각 기준의 간격은 +8:00.\n프로세스를 확인하며 앞 케이스가 끝난 뒤 "
                                         "이어서 실행하려면 '자동' 이라고 적는다(잡이 실행되는 동안 VM 에 접속한다)." % ex, parent=self.root)
                    return
            elif key in ("desc", "group"):
                c[key] = " ".join(t.split())          # 줄바꿈, 탭은 빈칸 하나로
            elif key == "end_time":
                try:
                    e = core.parse_end(t)
                except ValueError as ex:
                    messagebox.showerror(TITLE, str(ex), parent=self.root)
                    return
                self.calc_schedule()
                job = self.sched[i].get("job") if i < len(self.sched) else None
                if e is not None and job is not None and e <= job:
                    messagebox.showerror(TITLE, "잡 종료 %s 가 잡 시작 %s 보다 빠르거나 같음" % (core.fmt_dt(e), core.fmt_dt(job)),
                                         parent=self.root)
                    return
                c["end_time"] = core.fmt_dt(e) if e is not None else ""
        else:
            if key == "name":
                if t and not core.NAME_RE.match(t):
                    messagebox.showerror(TITLE, "VM 이름은 영문, 숫자, . _ - 만 사용: '%s'" % t, parent=self.root)
                    return
                v["name"] = t
            elif key == "job":
                v["job"] = t or NONE_JOB
            elif key == "power":
                v["power"] = "off" if t == core.POWER_LABELS["off"] else "on"
        self.mark_cases()
        self.t_case.refresh(iid)

    def _selected_case(self):
        sel = self.t_case.selected()
        i, k, c, v = self._get(sel) if sel else (None, None, None, None)
        return i, k, c, v

    def add_case(self):
        self.t_case.finish_edit()
        i, k, c, v = self._selected_case()
        if not self.cases:
            row = core.new_case("", next_hour(), count=int(self.settings.get("default_vms", 2)), prefix=self.vm_prefix())
            row["seq_start"] = row["start"]      # 순서 실행의 시작 시각도 다음 정시로 넣어 둔다(고쳐 쓴다)
            self.cases.append(row)
            new = 0
        else:
            src_i = len(self.cases) - 1 if i is None else i
            src = self.cases[src_i]
            new = src_i + 1
            row = copy.deepcopy(src)
            row["name"] = unique_name(src.get("name", ""), set(x.get("name", "") for x in self.cases))
            row["applied"] = False          # 새로 만든 케이스는 아직 등록 전
            row["applied_stop"] = ""
            row["end_time"] = ""            # 잡 종료는 케이스마다 다르므로 복사하지 않음 (자동)
            # 시각은 복사하지 않고 비워 둔다(같은 시각에 두 케이스를 시작할 수 없다. 빈 칸은 '입력 필요' 로 보인다).
            # +간격과 자동은 복사한다(앞 케이스 기준이라 그대로 뜻이 맞음)
            ss = (src.get("seq_start") or "").strip()
            row["seq_start"] = ss if (ss.startswith("+") or self.seq_is_auto(src)) else ""
            if row.get("stop_mode") == "manual":
                row["stop_mode"], row["stop_time"] = "auto", ""
            self.cases.insert(new, row)
            core.recalc_starts(self.cases, new)
        self.mark_cases()
        self.t_case.refresh("c%d" % new)
        # 이름 칸을 바로 편집 상태로 연다. 복사한 케이스는 자동 이름(-r2)이 골라진 채로 열려, 바로 치면 새 이름으로 바뀐다
        self.t_case.start_edit("c%d" % new, 0)

    def add_vm(self):
        """선택한 케이스(케이스 행이나 그 아래 VM 행)에 다음 번호 VM 을 추가한다."""
        self.t_case.finish_edit()
        i, k, c, v = self._selected_case()
        if c is None:
            messagebox.showinfo(TITLE, "VM 을 추가할 케이스를 먼저 고를 것 (케이스 행이나 그 아래 VM 행)", parent=self.root)
            return
        p = self.vm_prefix()
        n = core.next_vm_number(c, p)
        c.setdefault("vms", []).append(core.new_vm(core.vm_name(c.get("esx"), n, p)))
        iid = "c%dv%d" % (i, len(c["vms"]) - 1)
        self.mark_cases()
        self.t_case.refresh(iid)
        if not c["vms"][-1]["name"]:
            self.t_case.start_edit(iid, 0)

    def del_row(self):
        """케이스 행이면 케이스, VM 행이면 그 VM 을 삭제한다."""
        self.t_case.finish_edit()
        i, k, c, v = self._selected_case()
        if c is None:
            return
        cname = c.get("name") or "%d번째 케이스" % (i + 1)
        if k is not None:
            if not messagebox.askyesno(TITLE, "VM '%s' 를 케이스 '%s' 에서 삭제할까요?" % (v.get("name") or "이름 없음", cname),
                                       parent=self.root):
                return
            del c["vms"][k]
            self.mark_cases()
            self.t_case.refresh(("c%dv%d" % (i, min(k, len(c["vms"]) - 1))) if c["vms"] else "c%d" % i)
            return
        if not messagebox.askyesno(TITLE, "케이스 '%s' 를 삭제할까요? (VM %d대 포함)\n다른 케이스의 잡 시작 시각은 바뀌지 않는다."
                                   % (cname, len(c.get("vms") or [])), parent=self.root):
            return
        del self.cases[i]
        core.fit_interval(self.cases, i)        # 윗줄이 달라진 행의 간격 맞추기. 적용 체크한 케이스와 그 바로 아래 행은 그대로 둔다
        self.mark_cases()
        self.t_case.refresh(("c%d" % min(i, len(self.cases) - 1)) if self.cases else None)

    def move_row(self, step):
        """케이스 행: 케이스 순서를 바꾼다. 시각 지정에서 두 케이스 모두 적용 체크하지 않았으면 잡 시작, 간격, 모니터링 중지 설정,
        잡 종료 (리포트) 는 그 자리에 남기고, 하나라도 적용 체크한 케이스이면 값이 모두 케이스를 따라간다(core.swap_cases).
        VM 행: 케이스 안에서 VM 순서를 바꾼다."""
        self.t_case.finish_edit()
        i, k, c, v = self._selected_case()
        if c is None:
            return
        if k is not None:
            vms = c["vms"]
            j = k + step
            if not (0 <= j < len(vms)):
                return
            vms[k], vms[j] = vms[j], vms[k]
            self.mark_cases()
            self.t_case.refresh("c%dv%d" % (i, j))
            return
        j = i + step
        if not (0 <= j < len(self.cases)):
            return
        core.swap_cases(self.cases, i, j, self.mode() == "time")
        self.mark_cases()
        self.t_case.refresh("c%d" % j)

    def edit_monitors(self, iid):
        """VM 행: 그 VM 의 모니터링(실행 위치 VM). 케이스 행: ESX 모니터링(실행 위치 ESX)."""
        self.t_case.finish_edit()
        i, k, c, v = self._get(iid)
        if c is None:
            return
        cname = c.get("name") or "%d번째 케이스" % (i + 1)
        if k is None:
            def apply(sel):
                c["esx_monitors"] = sel
            self._monitor_dialog(iid, "ESX", c.get("esx_monitors") or [], apply,
                                 "ESX 모니터링 선택 - %s / %s" % (cname, c.get("esx") or "ESX 호스트 없음"),
                                 "이 케이스의 ESX 호스트에서 실행할 모니터링 (실행 위치가 ESX 인 명령만 보임)")
        else:
            def apply(sel):
                v["monitors"] = sel
            self._monitor_dialog(iid, "VM", v.get("monitors") or [], apply,
                                 "VM 모니터링 선택 - %s / %s" % (cname, v.get("name") or "이름 없음"),
                                 "이 VM 에서 실행할 모니터링 (실행 위치가 VM 인 명령만 보임)")

    def _monitor_dialog(self, iid, where, current, apply, title, head):
        rows = [(m["name"], "%s / %s" % (m.get("app") or "-", m["name"]), m.get("desc") or "") for m in self.mon_cmds
                if m.get("name") and (m.get("where") or "VM").strip().upper() == where]
        self._check_dialog(iid, rows, current, apply, title, head + ". 기본은 모두 선택 안 함.",
                           "탭 2 에 실행 위치가 %s 인 모니터링 명령을 먼저 등록할 것" % where, "탭 2 에 없음")

    def edit_profiles(self, iid):
        """설정 칸. 케이스 행: 대상이 ESX 호스트인 프로파일. VM 행: 대상이 VM 인 프로파일. 여러 개를 고를 수 있다."""
        self.t_case.finish_edit()
        i, k, c, v = self._get(iid)
        if c is None:
            return
        cname = c.get("name") or "%d번째 케이스" % (i + 1)
        target = "ESX" if k is None else "VM"

        def summary(p):
            items, errs = core.parse_profile_text(p.get("text"), target)
            if errs:
                return "형식 오류 %d건. 탭 3 에서 고칠 것" % len(errs)
            text = ", ".join(("%s = %s" % kv) if kv[1] != "" else "%s (삭제)" % kv[0] for kv in items[:3])
            return text + (" 외 %d개" % (len(items) - 3) if len(items) > 3 else "")

        rows = [(p["name"], p["name"], " ".join(x for x in (p.get("desc") or "", summary(p)) if x))
                for p in self.profiles if p.get("name") and (p.get("target") or "VM") == target]
        if k is None:
            def apply(sel):
                c["esx_profiles"] = sel
            self._check_dialog(iid, rows, c.get("esx_profiles") or [], apply,
                               "ESX 호스트 설정 선택 - %s / %s" % (cname, c.get("esx") or "ESX 호스트 없음"),
                               "이 케이스를 시작하기 전에 ESX 호스트에 적용할 설정 프로파일 (대상이 ESX 호스트인 프로파일만 보임). "
                               "여러 개를 고르면 탭 3 의 순서대로 합친다.",
                               "탭 3 에 대상이 ESX 호스트인 설정 프로파일을 먼저 등록할 것", "탭 3 에 없음")
        else:
            def apply(sel):
                v["profiles"] = sel
            self._check_dialog(iid, rows, v.get("profiles") or [], apply,
                               "VM 설정 선택 - %s / %s" % (cname, v.get("name") or "이름 없음"),
                               "이 케이스를 시작하기 전에 이 VM 에 적용할 설정 프로파일 (대상이 VM 인 프로파일만 보임). "
                               "여러 개를 고르면 탭 3 의 순서대로 합친다.",
                               "탭 3 에 대상이 VM 인 설정 프로파일을 먼저 등록할 것", "탭 3 에 없음")

    def _check_dialog(self, iid, rows, current, apply, title, head, empty_msg, missing_word):
        """여러 개를 고르는 창. rows = [(이름, 보이는 글, 설명)]. 고른 이름을 rows 순서대로 apply 에 넘긴다."""
        names = [r[0] for r in rows]
        missing = [n for n in current if n not in names]
        if not rows and not missing:
            messagebox.showinfo(TITLE, empty_msg, parent=self.root)
            return
        top = tk.Toplevel(self.root, bg=COLORS["card"])
        top.title(title)
        top.transient(self.root)
        fr = ttk.Frame(top, padding=self.px(10))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=head, style="Bold.TLabel", wraplength=self.px(720), justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, self.px(8)))
        vars_ = []
        r = 1
        for name, label, desc in rows:
            var = tk.BooleanVar(value=name in current)
            ttk.Checkbutton(fr, text=label, variable=var).grid(row=r, column=0, sticky="w", padx=(0, 16))
            if desc:
                ttk.Label(fr, text=desc, style="Hint.TLabel").grid(row=r, column=1, sticky="w")
            vars_.append((name, var))
            r += 1
        for n in missing:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(fr, text="%s (%s)" % (n, missing_word), variable=var).grid(row=r, column=0, sticky="w")
            vars_.append((n, var))
            r += 1
        bf = ttk.Frame(fr)
        bf.grid(row=r, column=0, columnspan=2, sticky="e", pady=(self.px(10), 0))

        def ok():
            apply([n for n, var in vars_ if var.get()])
            top.destroy()
            self.mark_cases()
            self.t_case.refresh(iid)

        ttk.Button(bf, text="모두 해제", command=lambda: [var.set(False) for _, var in vars_]).pack(side="left", padx=(0, 20))
        ttk.Button(bf, text="확인", command=ok, style="Blue.TButton").pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left")
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Return>", lambda e: ok())
        self._center(top)
        top.grab_set()

    def edit_stop(self, iid):
        self.t_case.finish_edit()
        i, k, c, v = self._get(iid)
        if c is None or k is not None:
            return
        if self.is_locked(c):
            self.locked_notice()
            return
        if not core.case_has_monitors(c):
            messagebox.showinfo(TITLE, "이 케이스는 모니터링을 고르지 않았음. VM 행이나 케이스 행의 모니터링 칸에서 먼저 선택할 것",
                                parent=self.root)
            return
        self.calc_schedule()
        s = self.sched[i]
        ld = self.leads() or (self.settings["mon_lead_min"], self.settings["stop_lead_min"])
        top = tk.Toplevel(self.root, bg=COLORS["card"])
        top.title("모니터링 중지 - %s" % (c.get("name") or "%d번째 케이스" % (i + 1)))
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
            self.t_case.refresh(iid)

        ttk.Button(bf, text="확인", command=ok, style="Blue.TButton").pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left")
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Return>", lambda e: ok())
        self._center(top)
        top.grab_set()

    def export_report(self):
        """[리포트용 CSV 저장]: 저장할 케이스를 고르게 한 뒤 성능 리포트 생성기용 케이스 CSV 를 표 순서대로 쓴다.
        비교에 쓰는 기준은 여기서 정하지 않는다(성능 리포트 생성기에서 정한다)."""
        self.finish_all_edits()
        if self.mode() == "seq":
            st = self.current_settings(show_error=False) or self.settings
            messagebox.showinfo(TITLE, "순서 실행에서는 리포트용 CSV 를 여기서 만들지 않는다.\n\n잡 시작과 종료의 실제 시각은 점프 VM 이 "
                                       "진행하면서 점프 VM 로그 폴더의 perfreport_cases.csv 에 기록한다.\n\n"
                                       "%s/tcrun-<실행 ID>-<ESX 호스트>-perfreport_cases.csv\n\n"
                                       "이 파일을 PC 로 가져와 성능 리포트 생성기의 '케이스 정보 파일' 에서 고른다. 테스트 그룹, 설명, "
                                       "종료 기준 VM 은 탭 4 에 적은 값이 들어간다." % core.jump_log_dir(st), parent=self.root)
            return
        if not self.cases:
            messagebox.showinfo(TITLE, "탭 4 에 테스트 케이스를 먼저 추가할 것", parent=self.root)
            return
        self.calc_schedule()
        C, px = COLORS, self.px
        top = tk.Toplevel(self.root, bg=C["card"])
        top.title("리포트용 CSV 저장")
        top.transient(self.root)
        fr = ttk.Frame(top, padding=(px(14), px(10), px(14), px(12)))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="성능 리포트 생성기의 케이스 정보 파일로 저장할 케이스를 고른다.", style="Bold.TLabel").pack(anchor="w")
        ttk.Label(fr, text="저장 칸을 누르면(또는 Space) 체크된다. 고른 케이스를 탭 4 의 표 순서대로 저장한다. "
                           "비교에 쓰는 기준은 여기서 정하지 않는다. 성능 리포트 생성기에서 정한다.",
                  style="Hint.TLabel").pack(anchor="w", pady=(px(2), px(8)))
        rows = []
        for i, c in enumerate(self.cases):
            s = self.sched[i] if i < len(self.sched) else {}
            rows.append({"sel": False, "i": i, "name": c.get("name") or "(이름 없음)", "desc": c.get("desc", ""),
                         "esx": c.get("esx", ""), "start": core.fmt_dt(s["job"]) if s.get("job") else "확인 필요",
                         "vms": ", ".join(core.report_vms(c)) or "(없음)", "applied": "예" if c.get("applied") else ""})
        cols = [dict(key="name", title="케이스", width=210, kind="readonly"),
                dict(key="desc", title="설명", width=170, kind="readonly"),
                dict(key="esx", title="ESX 호스트", width=85, kind="readonly"),
                dict(key="start", title="잡 시작 (KST)", width=140, kind="readonly"),
                dict(key="vms", title="리포트 VM", width=300, kind="readonly", stretch=True),
                dict(key="applied", title="적용", width=50, kind="readonly")]
        v_count = tk.StringVar()

        def picked():
            return [r for r in rows if r["sel"]]

        def update():
            v_count.set("고른 케이스 %d개" % len(picked()))

        def toggle(iid):
            r = rows[int(iid)]
            r["sel"] = not r["sel"]
            table.refresh(iid)
            update()

        def set_all(on):
            for r in rows:
                r["sel"] = on
            table.refresh()
            update()

        table = EditableTable(fr, self, cols, height=min(12, max(5, len(rows))),
                              check=dict(title="저장", width=48, on=self.img_on, off=self.img_off, toggle=toggle, key="sel"))
        table.rows = rows
        table.row_tags = lambda iid: ("pick",) if rows[int(iid)]["sel"] else (("odd",) if int(iid) % 2 else ())
        table.tree.tag_configure("pick", background=C["case"])
        table.pack(fill="both", expand=True)
        table.refresh(0)
        self.report_table = table
        opt = ttk.Frame(fr)
        opt.pack(fill="x", pady=(px(8), 0))
        ttk.Button(opt, text="모두 선택", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(opt, text="모두 해제", command=lambda: set_all(False)).pack(side="left", padx=(px(6), px(16)))
        ttk.Label(opt, textvariable=v_count).pack(side="left")
        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(px(12), 0))

        def save():
            sel = picked()
            if not sel:
                messagebox.showinfo(TITLE, "저장할 케이스를 하나 이상 고를 것", parent=top)
                return
            out, errors, warnings = core.report_rows(self.cases, self.sched, [r["i"] for r in sel])
            if errors:
                messagebox.showerror(TITLE, "오류 %d건. 저장하지 않았음. 탭 4 에서 고친 뒤 다시 저장할 것.\n\n%s"
                                     % (len(errors), "\n".join(errors[:12])), parent=top)
                return
            if warnings and not messagebox.askyesno(TITLE, "경고 %d건\n\n%s\n\n이대로 저장할까요?"
                                                    % (len(warnings), "\n".join(warnings[:12])), parent=top):
                return
            cfg = core.load_config()
            st = self.current_settings(show_error=False) or self.settings
            start_dir = cfg.get("report_dir") or ""
            if not os.path.isdir(start_dir):
                start_dir = core.resolve_paths(self.base, st)["log_dir"]
                try:
                    os.makedirs(start_dir, exist_ok=True)
                except OSError:
                    start_dir = self.base
            now = dt.datetime.now()
            path = filedialog.asksaveasfilename(parent=top, title="리포트용 CSV 저장 위치", initialdir=start_dir,
                                                initialfile=core.REPORT_PREFIX + now.strftime("%Y%m%d_%H%M") + ".csv",
                                                defaultextension=".csv", filetypes=[("CSV", "*.csv")])
            if not path:
                return
            try:
                keep = core.write_report_csv(path, out, backup_dir=os.path.join(self.base, "backup"), now=now)
            except OSError as e:
                messagebox.showerror(TITLE, "리포트용 CSV 를 저장하지 못함\n%s\n%s\n\n엑셀 등이 이 파일을 열고 있으면 닫고 다시 저장할 것"
                                     % (path, e), parent=top)
                return
            cfg["report_dir"] = os.path.dirname(os.path.abspath(path))
            try:
                core.save_config(cfg)
            except OSError:
                pass
            top.destroy()
            self.v_status.set("리포트용 CSV 저장: %s (케이스 %d개, 표 순서)" % (path, len(out)))
            messagebox.showinfo(TITLE, "리포트용 CSV 를 저장했다. 성능 리포트 생성기의 '케이스 정보 파일' 에서 이 파일을 고른다.\n\n"
                                       "%s\n케이스 %d개, 표 순서대로 저장%s"
                                % (path, len(out), ("\n\n같은 이름의 이전 파일을 복사해 둠: %s" % keep) if keep else ""),
                                parent=self.root)

        ttk.Button(bf, text="취소", command=top.destroy).pack(side="right")
        ttk.Button(bf, text="저장", style="Blue.TButton", command=save).pack(side="right", padx=(0, px(6)))
        update()
        top.bind("<Escape>", lambda e: top.destroy())
        self._center(top)
        top.grab_set()
        table.tree.focus_set()

    # ------------------------------------------------------------ 기록 대조
    def compare_records(self):
        """[기록 대조]: 적용 체크한 케이스의 시각 값을 기준(테스트 기록 CSV 의 마지막 생성 기록, 또는 예전 표 파일)과 대조해 다른 값을
        보이고, 고른 케이스의 값을 기준 값으로 되돌린다 (사용자 2026-11-17 "정합성안맞는경유필요 함")."""
        self.finish_all_edits()
        st = self.current_settings()
        if st is None:
            return
        if not any(c.get("applied") for c in self.cases):
            messagebox.showinfo(TITLE, "적용 체크한 케이스가 없음. 기록 대조는 적용 체크한 케이스의 값을 본다.", parent=self.root)
            return
        seq = self.mode() == "seq"
        paths = core.resolve_paths(self.base, st)
        C, px = COLORS, self.px
        top = tk.Toplevel(self.root, bg=C["card"])
        top.title("기록 대조: 적용 체크한 케이스")
        top.transient(self.root)
        self.cmp_win = top
        fr = ttk.Frame(top, padding=(px(14), px(10), px(14), px(12)))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="적용 체크한 케이스의 시각 값을 기준과 대조한다.", style="Bold.TLabel").pack(anchor="w")
        ttk.Label(fr, text="기준과 값이 다른 칸만 보인다. 되돌릴 케이스를 체크하고 [기준 값으로 되돌리기] 를 누르면 그 케이스의 값을 기준 "
                           "값으로 바꾼다. 바꾸기 전의 표는 데이터 폴더의 backup 아래에 저장한다. "
                           + ("대조하는 칸: 시작 시각." if seq else "대조하는 칸: 잡 시작, 간격, 모니터링 중지, 잡 종료 (리포트)."),
                  style="Hint.TLabel", wraplength=px(900), justify="left").pack(anchor="w", pady=(px(2), px(6)))
        v_ref, v_sum = tk.StringVar(), tk.StringVar()
        ttk.Label(fr, textvariable=v_ref).pack(anchor="w")
        ttk.Label(fr, textvariable=v_sum).pack(anchor="w", pady=(px(2), px(8)))
        rows, state = [], {"diffs": []}
        cols = [dict(key="name", title="케이스", width=200, kind="readonly"),
                dict(key="field", title="칸", width=110, kind="readonly"),
                dict(key="cur", title="지금 값", width=230, kind="readonly"),
                dict(key="ref", title="기준 값", width=230, kind="readonly", stretch=True),
                dict(key="gen", title="기준 기록의 생성 시각", width=160, kind="readonly")]

        def toggle(iid):
            r = rows[int(iid)]
            how = not r["sel"]
            for x in rows:
                if x["d"] == r["d"]:
                    x["sel"] = how
            table.refresh(iid)

        def set_all(how):
            for x in rows:
                x["sel"] = how
            table.refresh()

        table = EditableTable(fr, self, cols, height=12,
                              check=dict(title="되돌림", width=56, on=self.img_on, off=self.img_off, toggle=toggle, key="sel"))
        table.rows = rows
        table.row_tags = lambda iid: ("pick",) if rows[int(iid)]["sel"] else (("odd",) if int(iid) % 2 else ())
        table.tree.tag_configure("pick", background=C["case"])
        table.pack(fill="both", expand=True)
        self.cmp_table = table

        def load(kind, path=""):
            if kind == "record":
                rec = core.read_record_rows(paths["record_csv"])
                diffs, missing, same = core.record_diffs(self.cases, rec, seq, st["mon_lead_min"], st["stop_lead_min"])
                v_ref.set("기준: 테스트 기록 CSV 의 마지막 생성 기록 (%s)" % paths["record_csv"])
            else:
                try:
                    old = core.load_cases(self.base, path)
                except (OSError, ValueError) as e:
                    messagebox.showerror(TITLE, "표 파일을 읽지 못함\n%s\n%s" % (path, e), parent=top)
                    return
                diffs, missing, same = core.table_diffs(self.cases, old, seq)
                v_ref.set("기준: 예전 표 파일 (%s)" % path)
            state["diffs"] = diffs
            del rows[:]
            for n, d in enumerate(diffs):
                for m, (label, cur, ref) in enumerate(d["items"]):
                    rows.append({"sel": True, "d": n, "name": d["name"] if m == 0 else "", "field": label, "cur": cur, "ref": ref,
                                 "gen": d.get("ref", "") if m == 0 else ""})
            table.refresh(0 if rows else None)
            text = "적용 체크한 케이스 %d개: 기준과 다른 케이스 %d개, 같은 케이스 %d개, 기준에 없는 케이스 %d개" % (
                len(diffs) + same + len(missing), len(diffs), same, len(missing))
            if missing:
                text += " (%s%s)" % (", ".join(missing[:6]), " ..." if len(missing) > 6 else "")
            v_sum.set(text)

        def pick_file():
            start = os.path.join(self.base, "backup")
            path = filedialog.askopenfilename(parent=top, title="기준으로 쓸 예전 표 파일", filetypes=self.CASE_FILE_TYPES,
                                              initialdir=start if os.path.isdir(start) else self.base)
            if path:
                load("table", path)

        def go():
            picked = sorted(set(x["d"] for x in rows if x["sel"]))
            if not picked:
                messagebox.showinfo(TITLE, "되돌릴 케이스를 하나 이상 체크할 것", parent=top)
                return
            if not messagebox.askyesno(TITLE, "케이스 %d개의 값을 기준 값으로 바꾼다.\n바꾸기 전의 표는 데이터 폴더의 backup 아래에 "
                                              "저장한다.\n\n계속할까요?" % len(picked), parent=top):
                return
            try:
                keep = core.keep_cases_copy(self.base, self.cases, "before_record_restore")
            except OSError as e:
                messagebox.showerror(TITLE, "바꾸기 전의 표를 저장하지 못해 값을 바꾸지 않았음\n%s" % e, parent=top)
                return
            n = core.restore_cases(self.cases, [state["diffs"][k] for k in picked])
            self.calc_schedule()
            self.mark_cases()
            self.t_case.refresh()
            top.destroy()
            messagebox.showinfo(TITLE, "케이스 %d개의 값을 기준 값으로 바꿨음.\n바꾸기 전의 표: %s" % (n, keep), parent=self.root)

        opt = ttk.Frame(fr)
        opt.pack(fill="x", pady=(px(8), 0))
        ttk.Button(opt, text="테스트 기록 CSV 로 대조", command=lambda: load("record")).pack(side="left")
        ttk.Button(opt, text="예전 표 파일로 대조", command=pick_file).pack(side="left", padx=(px(6), px(16)))
        ttk.Button(opt, text="모두 선택", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(opt, text="모두 해제", command=lambda: set_all(False)).pack(side="left", padx=(px(6), 0))
        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(px(12), 0))
        ttk.Button(bf, text="닫기", command=top.destroy).pack(side="right")
        ttk.Button(bf, text="기준 값으로 되돌리기", style="Red.TButton", command=go).pack(side="right", padx=(0, px(6)))
        self.cmp_load, self.cmp_go, self.cmp_rows = load, go, rows
        load("record")
        top.bind("<Escape>", lambda e: top.destroy())
        self._center(top)
        top.grab_set()
        table.tree.focus_set()

    def _center(self, top):
        top.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - top.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - top.winfo_reqheight()) // 3)
        top.geometry("+%d+%d" % (x, y))

    # ------------------------------------------------------------ 생성
    def generate(self):
        """[명령어 생성]: 블록에 넣을 케이스를 고르는 창을 연다. 창의 [생성] 이 generate_picked 를 부른다.
        처음에는 적용 체크하지 않은 케이스가 골라져 있다."""
        self.finish_all_edits()
        if not self.cases:
            messagebox.showinfo(TITLE, "탭 4 에 테스트 케이스를 먼저 추가할 것", parent=self.root)
            return
        if self.current_settings() is None:
            return
        self.calc_schedule()
        seq = self.mode() == "seq"
        C, px = COLORS, self.px
        top = tk.Toplevel(self.root, bg=C["card"])
        top.title("명령어 생성: 넣을 테스트 케이스 선택")
        top.transient(self.root)
        self.gen_pick_win = top
        fr = ttk.Frame(top, padding=(px(14), px(10), px(14), px(12)))
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="블록에 넣을 테스트 케이스를 고른다.", style="Bold.TLabel").pack(anchor="w")
        ttk.Label(fr, text="넣기 칸을 누르면(또는 Space) 체크가 바뀐다. 처음에는 적용 체크하지 않은 케이스가 체크되어 있다. "
                           + ("고른 케이스를 탭 4 의 표 순서대로 진행한다. 순서는 탭 4 의 [위로], [아래로] 로 바꾼다."
                              if seq else "고르지 않은 케이스도 다른 케이스의 시각 계산에는 그대로 쓴다."),
                  style="Hint.TLabel").pack(anchor="w", pady=(px(2), px(8)))
        rows = []
        for i, c in enumerate(self.cases):
            sc = self.sched[i] if i < len(self.sched) else {}
            if seq:
                when = self.seq_start_text(c)
            else:
                when = core.fmt_dt(sc["job"]) if sc.get("job") else "확인 필요"
            rows.append({"sel": not c.get("applied"), "i": i, "name": c.get("name") or "(이름 없음)", "desc": c.get("desc", ""),
                         "esx": c.get("esx", ""), "vms": ", ".join(core.report_vms(c)) or "(없음)", "when": when,
                         "applied": "예" if c.get("applied") else ""})
        cols = [dict(key="name", title="케이스", width=210, kind="readonly"),
                dict(key="desc", title="설명", width=200, kind="readonly"),
                dict(key="esx", title="ESX 호스트", width=90, kind="readonly"),
                dict(key="vms", title="잡이나 모니터링이 있는 VM", width=280, kind="readonly", stretch=True),
                dict(key="when", title=("시작 시각" if seq else "잡 시작 (KST)"), width=140, kind="readonly"),
                dict(key="applied", title="적용", width=50, kind="readonly")]
        v_count = tk.StringVar()

        def update():
            v_count.set("고른 케이스 %d개 / 전체 %d개" % (sum(1 for r in rows if r["sel"]), len(rows)))

        def toggle(iid):
            r = rows[int(iid)]
            r["sel"] = not r["sel"]
            table.refresh(iid)
            update()

        def set_all(how):
            for r in rows:
                r["sel"] = how(r)
            table.refresh()
            update()

        table = EditableTable(fr, self, cols, height=min(14, max(5, len(rows))),
                              check=dict(title="넣기", width=48, on=self.img_on, off=self.img_off, toggle=toggle, key="sel"))
        table.rows = rows
        table.row_tags = lambda iid: ("pick",) if rows[int(iid)]["sel"] else (("odd",) if int(iid) % 2 else ())
        table.tree.tag_configure("pick", background=C["case"])
        table.pack(fill="both", expand=True)
        table.refresh(0)
        self.gen_pick_table = table
        opt = ttk.Frame(fr)
        opt.pack(fill="x", pady=(px(8), 0))
        ttk.Button(opt, text="모두 선택", command=lambda: set_all(lambda r: True)).pack(side="left")
        ttk.Button(opt, text="모두 해제", command=lambda: set_all(lambda r: False)).pack(side="left", padx=(px(6), 0))
        ttk.Button(opt, text="적용 체크하지 않은 케이스만",
                   command=lambda: set_all(lambda r: not self.cases[r["i"]].get("applied"))).pack(side="left", padx=(px(6), px(16)))
        ttk.Label(opt, textvariable=v_count).pack(side="left")
        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(px(12), 0))

        def go():
            picks = [r["i"] for r in rows if r["sel"]]
            if not picks:
                messagebox.showinfo(TITLE, "넣을 케이스를 하나 이상 고를 것", parent=top)
                return
            top.destroy()
            self.generate_picked(picks)

        ttk.Button(bf, text="취소", command=top.destroy).pack(side="right")
        ttk.Button(bf, text="생성", style="Red.TButton", command=go).pack(side="right", padx=(0, px(6)))
        update()
        top.bind("<Escape>", lambda e: top.destroy())
        self._center(top)
        top.grab_set()
        table.tree.focus_set()

    def generate_picked(self, picks):
        """고른 케이스(탭 4 의 순번)만 넣어 블록과 기록을 만든다. 고르지 않은 케이스는 적용 체크한 케이스처럼 다룬다
        (블록과 기록에서 빠지고, 시각 지정의 시각 계산에는 남는다). 탭 4 의 적용 체크는 바꾸지 않는다."""
        s = self.current_settings()
        if s is None:
            return
        picked = set(picks)
        cases = copy.deepcopy(self.cases)
        for i, c in enumerate(cases):
            c["applied"] = i not in picked
        paths = core.resolve_paths(self.base, s)
        seq = core.run_mode(paths) == "seq"
        if seq:
            # 잡이 있는데 시작 시각을 적지 않은 케이스가 있으면 만들지 않는다. 빈 칸을 자동(프로세스 확인)으로 넘기지 않기 위해서다
            unset = self.seq_unset_names(cases, picked)
            if unset:
                msg = ("시작 시각이 비어 있는 케이스 %d개: %s\n\n탭 4 의 시작 시각 칸에 잡을 시작할 시각을 적을 것 "
                       "(예: 2026-11-20 10:00:00, 10:00, 앞 케이스 기준 +8:00).\n프로세스를 확인하며 앞 케이스가 끝난 뒤 이어서 "
                       "실행하려면 '자동' 이라고 적는다. 자동은 잡이 실행되는 동안 VM 에 접속한다."
                       % (len(unset), ", ".join(unset[:12]) + (" ..." if len(unset) > 12 else "")))
                self.show_messages("오류 1건. 블록과 기록을 만들지 않았음. 고친 뒤 다시 생성할 것.", [msg.replace("\n\n", " ").replace("\n", " ")], [])
                self.nb.select(4)
                messagebox.showerror(TITLE, msg, parent=self.root)
                return
            plan, errors, warnings, _ = core.build_seq(cases, self.run_cmds, self.mon_cmds, self.profiles, paths)
            empty = not plan
            auto = self.seq_auto_names(cases, picked)
            if auto and not errors:
                poll = int(paths.get("seq_poll_sec", 1800))
                warnings = warnings + [
                    "시작 시각이 자동인 케이스 %d개(%s): 잡이 실행되는 동안 프로세스를 확인하러 VM 에 접속함(잡 시작 %d초 뒤 한 번, 그 뒤 "
                    "%s마다). 접속을 없애려면 시작 시각을 적을 것" % (
                        len(auto), ", ".join(auto[:8]) + (" ..." if len(auto) > 8 else ""), int(paths.get("seq_first_check_sec", 60)),
                        ("%d분" % (poll // 60)) if poll % 60 == 0 else ("%d초" % poll))]
        else:
            entries, errors, warnings, _ = core.build_entries(cases, self.run_cmds, self.mon_cmds, paths)
            warnings = warnings + core.profile_warnings_time(cases)
            empty = not entries
        if not errors and empty:
            if seq:
                errors = ["고른 케이스에 실행할 내용이 없음. 고른 케이스의 VM 에 잡을 지정하거나 설정 프로파일을 고를 것"]
            else:
                errors = ["고른 케이스에 등록할 줄이 없음. 고른 케이스의 VM 에 잡이나 모니터링을 하나 이상 지정할 것"]
        if errors:
            self.show_messages("오류 %d건. 블록과 기록을 만들지 않았음. 고친 뒤 다시 생성할 것." % len(errors), errors, warnings)
            self.nb.select(4)
            messagebox.showerror(TITLE, "오류 %d건\n\n%s%s" % (len(errors), "\n".join(errors[:12]),
                                                              "\n..." if len(errors) > 12 else ""), parent=self.root)
            return
        if warnings and not messagebox.askyesno(
                TITLE, "경고 %d건\n\n%s%s\n\n계속 생성할까요? 생성하면 테스트 기록 CSV 에 행이 추가된다."
                % (len(warnings), "\n".join(warnings[:12]), "\n..." if len(warnings) > 12 else ""), parent=self.root):
            return
        try:
            res = core.generate(cases, self.run_cmds, self.mon_cmds, paths, write=True,
                                backup_dir=os.path.join(self.base, "backup"), profiles=self.profiles)
        except (OSError, ValueError) as e:
            messagebox.showerror(TITLE, "기록 파일을 저장하지 못해 블록을 만들지 않았음.\n%s\n\n"
                                        "테스트 기록 CSV 를 엑셀 등이 열고 있으면 닫고 다시 생성할 것." % e, parent=self.root)
            return
        if res["errors"]:
            self.show_messages("오류", res["errors"], res["warnings"])
            return
        if res.get("mode") != "seq":
            # D 블록 확인 창의 '줄이 지워지는 케이스' 는 실제로 적용 체크한 케이스로 다시 계산한다(고르지 않았을 뿐인 케이스는 등록된 줄이 없다)
            res["wipe_conflicts"] = core.wipe_conflicts(self.cases, res["sched"], res["wipe_hosts"], res["gen_time"])
        self.result = res
        self.show_result(res)
        self.nb.select(4)

    def show_messages(self, head, errors, warnings):
        self.result = None
        t = self.res_text
        t.delete("1.0", "end")
        t.insert("end", head + "\n", "err" if errors else "ok")
        for e in errors:
            t.insert("end", "오류: " + e + "\n", "err")
        for w in warnings:
            t.insert("end", "경고: " + w + "\n", "warn")
        for blk in (self.blk_setup, self.blk_vm, self.blk_esx, self.blk_wipe, self.plan_text):
            set_text(blk, "")
        self.cb_esx.configure(values=[])
        self.v_esx_host.set("")

    def show_result_seq(self, res):
        """순서 실행의 결과: 블록 A, P, S, D 와 실행 계획."""
        t = self.res_text
        t.delete("1.0", "end")
        t.insert("end", "생성 %s.  테스트 기록 CSV 에 %d행 추가: %s\n" % (core.fmt_dt(res["gen_time"]), res["record_rows"],
                                                                 res["record"]), "ok")
        t.insert("end", "로그: %s\n" % res["log"], "ok")
        hosts = ["ESX 호스트 없음" if h == "-" else h for h in res["chains"]]
        t.insert("end", "순서 실행. 실행 ID %s. 케이스 %d개, 선택 창에서 뺀 케이스 %d개. ESX 호스트별 진행 %d개: %s\n"
                 % (res["run_id"], len(res["cases"]), res.get("skipped", 0), len(hosts), ", ".join(hosts)))
        t.insert("end", "붙여넣는 순서: A 는 점프 VM 에서 처음 한 번, 그 뒤 P(점검)에서 CHECK RESULT: OK 를 확인하고 S(실행). "
                        "점프 VM 로그 폴더: %s\n" % res["jump_log_dir"])
        if any(pc["host_opts"] or any(vm["set"] for vm in pc["vms"]) for pc in res["cases"]):
            t.insert("end", "설정을 적용하는 케이스가 있다. 값이 현재 값과 다르면 그 케이스의 VM 을 자동으로 종료하고 다시 켠다.\n", "warn")
        nochk = [pc["name"] for pc in res["cases"] if pc.get("nocheck")]
        chk = [pc["name"] for pc in res["cases"] if not pc.get("nocheck") and any(vm["job"] for vm in pc["vms"])]
        if nochk:
            t.insert("end", "잡 확인: 시작 시각을 적은 케이스 %d개는 확인하지 않음(잡이 실행되는 동안 VM 접속 없음)%s\n" % (
                len(nochk), (", 확인하는 케이스 %d개(%s)" % (len(chk), ", ".join(chk[:8]))) if chk else ""))
        offs = ["%s(%s)" % (pc["name"], ", ".join(vm["name"] for vm in pc["vms"] if vm["power"] == "off"))
                for pc in res["cases"] if any(vm["power"] == "off" for vm in pc["vms"])]
        if offs:
            t.insert("end", "전원 끔인 VM 이 있는 케이스: %s. 그 VM 이 켜져 있으면 자동으로 끈다.\n" % ", ".join(offs), "warn")
        if res.get("record_added"):
            t.insert("end", "기존 테스트 기록 CSV 의 머리글 끝에 새 열을 덧붙였음: %s. 덧붙이기 전 원본: %s\n"
                     % (", ".join(res["record_added"]), res.get("record_backup")), "warn")
        for w in res["warnings"]:
            t.insert("end", "경고: " + w + "\n", "warn")
        set_text(self.blk_setup, res["setup"])
        set_text(self.blk_vm, res["check"])
        set_text(self.blk_esx, res["run"])
        set_text(self.blk_wipe, res.get("vm_wipe") or "(VM 없음)\n")
        self.show_plan(self.plan_text, res["plan_view"])
        self.cb_esx.configure(values=[])
        self.v_esx_host.set("")

    def show_result(self, res):
        if res.get("mode") == "seq":
            self.show_result_seq(res)
            return
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
        t.insert("end", "생성한 케이스 %d개, 선택 창에서 뺀 케이스 %d개\n" % (made, res.get("skipped", 0)))
        t.insert("end", "VM %d대: %s\n" % (len(vm_cnt), ", ".join("%s %d줄" % kv for kv in vm_cnt.items()) or "없음"))
        t.insert("end", "ESX %d대: %s\n" % (len(esx_cnt), ", ".join("%s %d줄" % kv for kv in esx_cnt.items()) or "없음"))
        if res.get("record_added"):
            t.insert("end", "기존 테스트 기록 CSV 의 머리글 끝에 새 열을 덧붙였음: %s. 덧붙이기 전 원본: %s\n"
                     % (", ".join(res["record_added"]), res.get("record_backup")), "warn")
        for w in res["warnings"]:
            t.insert("end", "경고: " + w + "\n", "warn")
        if res.get("wipe_conflicts"):
            t.insert("end", "주의: 적용 체크한 케이스 %s 는 실행할 시각이 남아 있고 D 블록의 VM 을 쓴다. D 블록을 붙여넣으면 그 케이스의 "
                            "crontab 줄도 지워진다. 그 케이스도 다시 등록하려면 적용 체크를 끄고 다시 생성할 것\n"
                     % ", ".join(res["wipe_conflicts"]), "warn")
        set_text(self.blk_setup, res["setup"])
        set_text(self.blk_vm, res["vm"] or "(VM 에 등록할 줄 없음)\n")
        set_text(self.blk_wipe, res.get("vm_wipe") or "(VM 없음)\n")
        hosts = list(res["esx"].keys())
        self.cb_esx.configure(values=hosts)
        self.v_esx_host.set(hosts[0] if hosts else "")
        self.show_esx_block()

    def show_esx_block(self):
        res = self.result
        h = self.v_esx_host.get()
        if res and res.get("mode") == "seq":
            return
        if not res or not res["esx"]:
            set_text(self.blk_esx, "(ESX 에 등록할 줄 없음)\n" if res else "")
            return
        set_text(self.blk_esx, res["esx"].get(h, ""))

    def copy_block(self, which):
        res = self.result
        if not res:
            messagebox.showinfo(TITLE, "탭 4 에서 [명령어 생성] 을 먼저 누를 것", parent=self.root)
            return
        seq = res.get("mode") == "seq"
        if which == "setup":
            text, label = res["setup"], "A. 점프 VM 최초 1회 설정"
        elif which == "vm" and seq:
            text, label = res["check"], "P. 점검 블록"
        elif which == "esx" and seq:
            text, label = res["run"], "S. 실행 블록"
            sets = [pc["name"] for pc in res["cases"] if pc["host_opts"] or any(vm["set"] for vm in pc["vms"])]
            msg = ("S 블록을 점프 VM 터미널에 붙여넣으면 케이스 %d개의 진행이 바로 시작된다 (실행 ID %s).\n\n"
                   "먼저 P(점검) 블록에서 CHECK RESULT: OK 를 확인했는지 볼 것." % (len(res["cases"]), res["run_id"]))
            if sets:
                msg += ("\n\n설정을 적용하는 케이스: %s\n값이 현재 값과 다르면 그 케이스의 VM 을 자동으로 종료하고 다시 켠다."
                        % ", ".join(sets[:8]))
            offs = [pc["name"] for pc in res["cases"] if any(vm["power"] == "off" for vm in pc["vms"])]
            if offs:
                msg += "\n\n전원 끔인 VM 이 있는 케이스: %s\n그 VM 이 켜져 있으면 자동으로 끈다." % ", ".join(offs[:8])
            if not messagebox.askyesno(TITLE, msg + "\n\n복사할까요?", parent=self.root):
                return
        elif which == "vm":
            text, label = res["vm"], "B. VM 등록 블록"
        elif which == "wipe":
            text, label = res.get("vm_wipe", ""), "D. VM crontab 전체 삭제"
            hosts = res.get("wipe_hosts") or []
            if text:
                msg = ("D 블록을 점프 VM 터미널에 붙여넣으면 VM %d대(%s)의 crontab 줄이 모두 지워진다. tcgen 이 등록하지 않은 줄도 "
                       "지워진다.\n\n지우기 전 내용은 각 VM 홈 폴더의 tcgen_crontab_backup-<시각>.txt 에 저장된다. "
                       "그 VM 에서 crontab <그 파일> 을 실행하면 되돌릴 수 있다." % (len(hosts), ", ".join(hosts)))
                if res.get("wipe_conflicts"):
                    msg += "\n\n적용 체크한 케이스 %s 의 줄도 지워진다." % ", ".join(res["wipe_conflicts"])
                if seq:
                    msg += ("\n\n순서 실행이 진행 중일 때 붙여넣으면 등록된 줄이 지워져 잡이 시작되지 않는다. "
                            "진행 상태는 sh %s/tcrun status 로 확인한다." % self.jump_dir_text())
                if not messagebox.askyesno(TITLE, msg + "\n\n복사할까요?", parent=self.root):
                    return
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
