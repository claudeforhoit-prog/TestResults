"""창 화면: 케이스 정보 CSV 와 데이터 폴더를 고르고 [리포트 생성] 을 누르면 docx · html 을 만든다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import palette as P
from . import settings as settings_mod
from . import roles as R
from .metrics import ROLE_DESC

CORE_PROFILE = 'profiles/analysis/vm_core.yaml'
GUIDE = ('문서와 HTML 에는 핵심 성능 지표, 원인 분석 지표, 세부 분석 지표, 상충 지표, 측정 조건 확인 항목 순서로 나오고, '
         '같은 역할 안에서는 순서 번호대로 나옵니다. 역할을 하나도 지정하지 않으면 기본 리포트 양식으로 만듭니다.')


def role_editor(root, names, roles, on_ok):
    """지표 역할 지정 창. [확인] 을 누르면 on_ok(역할) 을 부른다. [취소] 나 창 닫기는 바꾼 내용을 버린다.
    반환: 창 요소와 동작 함수(창 없이 시험할 때 쓴다)."""
    model = R.RoleModel(names, roles)
    win = tk.Toplevel(root)
    win.title('지표 역할 지정')
    win.geometry('1000x660')
    win.transient(root)

    top = tk.Frame(win, padx=10, pady=8)
    top.pack(fill='x')
    tk.Label(top, text='지표 검색').pack(side='left')
    query = tk.StringVar()
    tk.Entry(top, textvariable=query, width=40).pack(side='left', padx=6)
    count = tk.Label(top, fg='#55544f')
    count.pack(side='left', padx=10)

    tk.Label(win, anchor='w', padx=10, fg='#55544f',
             text='지표 줄을 누르면 체크(☑)가 바뀝니다. 역할 버튼을 누르면 체크한 지표에 그 역할이 지정되고 체크가 풀립니다.'
             ).pack(fill='x')

    desc = tk.Label(win, anchor='w', justify='left', padx=10, pady=4, wraplength=960, text=GUIDE)
    desc.pack(side='bottom', fill='x')
    note = tk.Label(win, anchor='w', justify='left', padx=10, fg='#9a3412', wraplength=960)
    note.pack(side='bottom', fill='x')
    mid = tk.Frame(win, padx=10, pady=6)
    mid.pack(fill='both', expand=True)
    cols = ('선택', '지표', '분류', '역할', '순서')
    tree = ttk.Treeview(mid, columns=cols, show='headings', selectmode='none')
    for c, w, anchor in zip(cols, (50, 430, 110, 170, 50), ('center', 'w', 'w', 'w', 'center')):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor=anchor, stretch=(c == '지표'))
    sb = ttk.Scrollbar(mid, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side='left', fill='both', expand=True)
    sb.pack(side='left', fill='y')

    side = tk.Frame(mid, padx=10)
    side.pack(side='left', fill='y')

    iids = {}

    def update_count():
        count.config(text=f'체크한 지표 {len(model.checked)}개, 역할을 지정한 지표 {len(model.roles)}개, 전체 지표 {len(model.names)}개')

    def refresh(keep=True):
        y = tree.yview()[0]
        tree.delete(*tree.get_children())
        iids.clear()
        for i, (m, cat, role, order, on) in enumerate(model.rows()):
            iid = f'r{i}'
            iids[iid] = m
            tree.insert('', 'end', iid=iid, values=('☑' if on else '☐', m, cat, role, order))
        if keep:
            tree.yview_moveto(y)
        update_count()

    def toggle(m):
        model.toggle(m)
        for iid, name in iids.items():
            if name == m:
                vals = list(tree.item(iid, 'values'))
                vals[0] = '☑' if m in model.checked else '☐'
                tree.item(iid, values=vals)
        update_count()

    def click(e):
        if tree.identify_region(e.x, e.y) in ('cell', 'tree'):
            iid = tree.identify_row(e.y)
            if iid in iids:
                toggle(iids[iid])

    def search(*_):
        model.query = query.get()
        refresh(keep=False)

    def need_checks():
        if model.checked:
            note.config(text='')
            return True
        note.config(text='먼저 지표를 체크하십시오.')
        return False

    def assign(role):
        if need_checks():
            model.assign(role)
            model.clear_checks()
            refresh()

    def unassign():
        if need_checks():
            model.unassign()
            model.clear_checks()
            refresh()

    def move(delta):
        if not need_checks():
            return
        if not any(m in model.roles for m in model.checked):
            note.config(text='순서는 역할을 지정한 지표만 바꿀 수 있습니다.')
            return
        model.move(delta)
        refresh()

    def clear():
        model.clear_checks()
        note.config(text='')
        refresh()

    def load_path(p):
        try:
            missing = model.load(p)
        except SystemExit as e:
            messagebox.showerror('역할 파일', str(e), parent=win)
            return None
        model.clear_checks()
        refresh(keep=False)
        msg = f'불러왔습니다: {os.path.basename(p)} ({R.summary(model.roles)})'
        if missing:
            msg += (f'\n이번 데이터에 없는 지표 {len(missing)}개는 뺐습니다: ' + ', '.join(missing[:5])
                    + (f' 외 {len(missing) - 5}개' if len(missing) > 5 else ''))
        note.config(text=msg)
        return missing

    def load():
        p = filedialog.askopenfilename(parent=win, title='역할 파일 불러오기',
                                       filetypes=[('역할 파일 (CSV)', '*.csv'), ('모든 파일', '*.*')])
        if p:
            load_path(p)

    def save_path(p):
        model.save(p)
        note.config(text=f'저장했습니다: {p}')

    def save():
        if not model.roles:
            note.config(text='저장할 역할 지정이 없습니다.')
            return
        p = filedialog.asksaveasfilename(parent=win, title='역할 파일 저장', defaultextension='.csv',
                                         initialfile='metric_roles.csv', filetypes=[('역할 파일 (CSV)', '*.csv')])
        if p:
            save_path(p)

    def ok():
        on_ok(R.normalize(model.roles))
        win.destroy()

    def show(text):
        desc.config(text=text)

    tk.Label(side, text='체크한 지표의 역할', anchor='w').pack(fill='x')
    for role in R.ROLES:
        b = tk.Button(side, text=role, width=20, command=lambda r=role: assign(r))
        b.pack(fill='x', pady=2)
        b.bind('<Enter>', lambda e, r=role: show(f'{r}: {ROLE_DESC[r]}'))
        b.bind('<Leave>', lambda e: show(GUIDE))
    ttk.Separator(side).pack(fill='x', pady=8)
    for text, cmd in (('역할 해제', unassign), ('위로', lambda: move(-1)), ('아래로', lambda: move(1)),
                      ('체크 모두 해제', clear)):
        tk.Button(side, text=text, width=20, command=cmd).pack(fill='x', pady=2)
    ttk.Separator(side).pack(fill='x', pady=8)
    for text, cmd in (('불러오기…', load), ('저장…', save)):
        tk.Button(side, text=text, width=20, command=cmd).pack(fill='x', pady=2)
    tk.Button(side, text='취소', width=20, command=win.destroy).pack(side='bottom', fill='x', pady=2)
    tk.Button(side, text='확인', width=20, command=ok).pack(side='bottom', fill='x', pady=2)

    tree.bind('<ButtonRelease-1>', click)
    query.trace_add('write', search)
    refresh(keep=False)
    win.grab_set()
    return {'win': win, 'tree': tree, 'model': model, 'query': query, 'note': note, 'toggle': toggle,
            'assign': assign, 'unassign': unassign, 'move': move, 'clear': clear,
            'load_path': load_path, 'save_path': save_path, 'ok': ok}


def build(root):
    """기본 창을 구성한다. 반환: 입력 값과 동작 함수(창 없이 시험할 때 쓴다)."""
    root.title('성능 리포트 생성기')
    root.geometry('860x890')
    v = {k: tk.StringVar() for k in ('cases', 'data', 'out', 'split', 'title', 'baseline', 'selection')}
    core = tk.BooleanVar(value=False)
    include_un = tk.BooleanVar(value=True)
    role_sum = tk.StringVar(value=R.summary({}))
    pal_var = tk.StringVar(value=P.PALETTES[P.DEFAULT_PALETTE][0])
    width_var = tk.StringVar(value='16.5')
    to_gb = tk.BooleanVar(value=False)      # 단위 변환: 기본은 원래 단위 그대로
    to_gbps = tk.BooleanVar(value=False)
    saved = settings_mod.load()             # TAT 비교 기준값과 설명: 바꿀 때까지 저장해 둔다
    tat_base = tk.StringVar(value=str(saved.get('tat_baseline', '')))
    tat_note = tk.StringVar(value=str(saved.get('tat_baseline_note', '')))
    end_refs = tk.StringVar(value=str(saved.get('end_ref_vms', '')))      # 종료 기준 VM: 바꿀 때까지 저장
    q = queue.Queue()
    last = {}
    state = {'roles': {}}   # 확인을 누른 지표 역할 {지표: (역할, 순서)}

    frm = tk.Frame(root, padx=12, pady=10)
    frm.pack(fill='x')
    frm.columnconfigure(1, weight=1)

    def row(r, label, key, pick=None, hint=''):
        tk.Label(frm, text=label, anchor='w', width=16).grid(row=r, column=0, sticky='w', pady=3)
        tk.Entry(frm, textvariable=v[key]).grid(row=r, column=1, sticky='we')
        if pick:
            tk.Button(frm, text='찾기…', command=pick).grid(row=r, column=2, padx=6)
        if hint:
            tk.Label(frm, text=hint, fg='#77766f', anchor='w').grid(row=r + 1, column=1, sticky='w')

    def pick_cases():
        p = filedialog.askopenfilename(title='케이스 정보 파일', filetypes=[('케이스 정보 (CSV · 엑셀)', '*.csv *.xlsx'), ('모든 파일', '*.*')])
        if p:
            v['cases'].set(p)
            if not v['data'].get():
                v['data'].set(os.path.dirname(p))
            if not v['out'].get():
                v['out'].set(os.path.join(os.path.dirname(p), 'out_' + os.path.splitext(os.path.basename(p))[0]))

    row(0, '케이스 정보 파일', 'cases', pick_cases, 'CSV 또는 엑셀(.xlsx) · 열: 케이스명, 설명, 호스트 이름, 잡 시작 시각, 잡 종료 시각, VM(선택)')
    row(2, '데이터 폴더', 'data', lambda: v['data'].set(filedialog.askdirectory(title='데이터 폴더') or v['data'].get()),
        '이 폴더(하위 폴더 포함)의 CSV · 로그 파일을 모두 읽는다')
    row(4, '결과 폴더', 'out', lambda: v['out'].set(filedialog.askdirectory(title='결과 폴더') or v['out'].get()))
    row(5, 'TC별 정리 폴더 (선택)', 'split', lambda: v['split'].set(filedialog.askdirectory(title='TC별 정리 폴더') or v['split'].get()),
        '지정하면 원본에서 케이스마다 그 호스트·VM 의 실행 구간(앞뒤 10분) 행만 잘라 <폴더>/<케이스명>/ 에 저장')
    row(7, '제목 (선택)', 'title')
    row(8, '기준 케이스 (선택)', 'baseline', hint='비우면 케이스 정보의 첫 번째 케이스')
    tk.Checkbutton(frm, text='VM 핵심 지표만 넣기 (CPU · 메모리 등 8종)', variable=core).grid(row=10, column=1, sticky='w')

    tk.Label(frm, text='그래프 색 계열', anchor='w', width=16).grid(row=11, column=0, sticky='w', pady=3)
    ttk.Combobox(frm, textvariable=pal_var, values=[n for n, _ in P.PALETTES], state='readonly', width=22).grid(row=11, column=1, sticky='w')
    tk.Label(frm, text='그래프 폭 (cm)', anchor='w', width=16).grid(row=12, column=0, sticky='w', pady=3)
    wf = tk.Frame(frm)
    wf.grid(row=12, column=1, sticky='w')
    tk.Spinbox(wf, from_=10, to=31.5, increment=0.5, textvariable=width_var, width=6).pack(side='left')
    tk.Label(wf, text='기본 16.5 (세로 페이지 본문 폭). 넓히면 가로 방향 페이지. 컨플루언스로 가져올 문서는 31.5 (최대)',
             fg='#77766f').pack(side='left', padx=8)

    tk.Label(frm, text='지표 역할 (선택)', anchor='w', width=16).grid(row=13, column=0, sticky='w', pady=(8, 3))
    rf = tk.Frame(frm)
    rf.grid(row=13, column=1, sticky='we', pady=(8, 3))
    role_btn = tk.Button(rf, text='지표 역할 지정…')
    role_btn.pack(side='left')
    tk.Label(rf, textvariable=role_sum, anchor='w', justify='left', wraplength=560).pack(side='left', padx=8)
    un_chk = tk.Checkbutton(frm, text='역할을 지정하지 않은 지표도 넣기 (문서는 Appendix A)', variable=include_un, state='disabled')
    un_chk.grid(row=14, column=1, sticky='w')
    row(15, 'HTML 선택 파일 (선택)', 'selection',
        lambda: v['selection'].set(filedialog.askopenfilename(title='HTML 선택 파일', filetypes=[('HTML 선택 파일 (JSON)', '*.json'), ('모든 파일', '*.*')]) or v['selection'].get()),
        "HTML 의 '선택 내보내기' 로 저장한 파일. 지정하면 그 케이스 · VM · 지표만으로 만든다")
    tk.Label(frm, text='단위 변환 (선택)', anchor='w', width=16).grid(row=17, column=0, sticky='w', pady=3)
    uf = tk.Frame(frm)
    uf.grid(row=17, column=1, sticky='w')
    tk.Checkbutton(uf, text='크기를 GB 로 (KB · MB · Bytes → GB)', variable=to_gb).pack(side='left')
    tk.Checkbutton(uf, text='네트워크 속도를 Gbps 로 (KBps · Mbps → Gbps)', variable=to_gbps).pack(side='left', padx=12)
    tk.Label(frm, text='TAT 비교 기준 (선택)', anchor='w', width=16).grid(row=18, column=0, sticky='w', pady=3)
    bf = tk.Frame(frm)
    bf.grid(row=18, column=1, sticky='we')
    tk.Entry(bf, textvariable=tat_base, width=12).pack(side='left')
    tk.Label(bf, text='초 · 설명').pack(side='left', padx=(4, 4))
    tk.Entry(bf, textvariable=tat_note, width=42).pack(side='left', fill='x', expand=True)
    tk.Label(frm, text='잡 TAT 원장이 있을 때 문서 · HTML 에 저하율을 적는다. 비우면 넣지 않는다. 값과 설명은 바꿀 때까지 저장된다',
             fg='#77766f', anchor='w').grid(row=19, column=1, sticky='w')
    tk.Label(frm, text='종료 기준 VM (선택)', anchor='w', width=16).grid(row=20, column=0, sticky='w', pady=3)
    tk.Entry(frm, textvariable=end_refs).grid(row=20, column=1, sticky='we')
    tk.Label(frm, text='잡 종료 시각을 비운 케이스에서 이 VM 들의 잡 종료(TAT 로그)와 CPU 사용률로 종료 시각을 정한다. 여러 대면 가장 늦게 끝난 VM 기준. '
                       '이름 · 패턴을 ; 로 구분 (예: *app01;*app02). 비우면 케이스의 모든 VM',
             fg='#77766f', anchor='w', justify='left', wraplength=640).grid(row=21, column=1, sticky='w')

    btns = tk.Frame(root, padx=12)
    btns.pack(fill='x')
    run_btn = tk.Button(btns, text='리포트 생성', width=14)
    run_btn.pack(side='left')
    open_btn = tk.Button(btns, text='결과 폴더 열기', width=14, state='disabled')
    open_btn.pack(side='left', padx=6)

    log = scrolledtext.ScrolledText(root, height=18, font=('Malgun Gothic', 9))
    log.pack(fill='both', expand=True, padx=12, pady=10)

    def set_roles(r):
        state['roles'] = r
        role_sum.set(R.summary(r))
        un_chk.config(state='normal' if r else 'disabled')

    def list_worker(cases, data, analysis, gb=False, gbps=False):
        from .build import list_metrics
        try:
            q.put(('metrics', list_metrics(cases, data, log=lambda m: q.put(('log', m)), analysis=analysis, to_gb=gb, to_gbps=gbps)))
        except SystemExit as e:
            q.put(('merr', str(e)))
        except Exception as e:  # 화면에 원인을 그대로 보인다
            q.put(('merr', f'{type(e).__name__}: {e}'))

    def open_roles():
        if not os.path.isfile(v['cases'].get()) or not os.path.isdir(v['data'].get()):
            messagebox.showwarning('입력', '케이스 정보 파일과 데이터 폴더를 먼저 고르십시오.')
            return
        role_btn.config(state='disabled')
        log.insert('end', '── 지표 목록 읽기 시작\n')
        threading.Thread(target=list_worker, args=(v['cases'].get(), v['data'].get(), CORE_PROFILE if core.get() else None,
                                                   to_gb.get(), to_gbps.get()),
                         daemon=True).start()

    def worker(kw):
        from .build import run_csv
        try:
            rec = run_csv(log=lambda m: q.put(('log', m)), **kw)
            q.put(('done', rec))
        except SystemExit as e:
            q.put(('err', str(e)))
        except Exception as e:  # 화면에 원인을 그대로 보인다
            q.put(('err', f'{type(e).__name__}: {e}'))

    def start():
        if not os.path.isfile(v['cases'].get()):
            messagebox.showwarning('입력', '케이스 정보 파일(CSV 또는 엑셀)을 고르십시오.')
            return
        if not os.path.isdir(v['data'].get()):
            messagebox.showwarning('입력', '데이터 폴더를 고르십시오.')
            return
        try:
            width = float(width_var.get())
        except ValueError:
            messagebox.showwarning('입력', '그래프 폭은 숫자(cm)로 적으십시오. 예: 16.5')
            return
        base_txt = tat_base.get().strip().replace(',', '')
        try:
            base = float(base_txt) if base_txt else None
            if base is not None and base <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning('입력', 'TAT 비교 기준은 0 보다 큰 숫자(초)로 적으십시오. 쓰지 않으려면 비웁니다. 예: 21721.65')
            return
        settings_mod.save({'tat_baseline': base_txt, 'tat_baseline_note': tat_note.get().strip(), 'end_ref_vms': end_refs.get().strip()})
        kw = dict(cases_csv=v['cases'].get(), data_dir=v['data'].get(), out_dir=v['out'].get() or None,
                  title=v['title'].get() or None, baseline=v['baseline'].get() or None,
                  analysis=CORE_PROFILE if core.get() else None, split_dir=v['split'].get() or None,
                  roles=dict(state['roles']) or None, include_unassigned=include_un.get(),
                  palette=pal_var.get(), chart_width_cm=width, selection=v['selection'].get() or None,
                  to_gb=to_gb.get(), to_gbps=to_gbps.get(), tat_baseline=base, tat_baseline_note=tat_note.get().strip(),
                  end_ref_vms=end_refs.get().strip())
        run_btn.config(state='disabled')
        log.insert('end', f'── 시작: {kw["cases_csv"]}\n── 지표 역할: {R.summary(state["roles"])}\n')
        threading.Thread(target=worker, args=(kw,), daemon=True).start()

    def poll():
        while not q.empty():
            kind, val = q.get()
            if kind == 'log':
                log.insert('end', val + '\n')
            elif kind == 'done':
                last['dir'] = os.path.dirname(val['outputs'][0])
                open_btn.config(state='normal')
                run_btn.config(state='normal')
                log.insert('end', '── 완료\n')
            elif kind == 'metrics':
                role_btn.config(state='normal')
                if val:
                    log.insert('end', f'── 지표 {len(val)}개를 읽었습니다\n')
                    role_editor(root, val, state['roles'], set_roles)
                else:
                    messagebox.showwarning('지표 역할', '케이스에 해당하는 데이터 파일에서 지표를 찾지 못했습니다.')
            elif kind == 'merr':
                role_btn.config(state='normal')
                log.insert('end', f'── 지표 목록 읽기 실패: {val}\n')
                messagebox.showerror('지표 역할', val)
            else:
                run_btn.config(state='normal')
                log.insert('end', f'── 실패: {val}\n')
                messagebox.showerror('실패', val)
            log.see('end')
        root.after(200, poll)

    def open_dir():
        d = last.get('dir')
        if not d:
            return
        if sys.platform.startswith('win'):
            os.startfile(d)
        elif sys.platform == 'darwin':
            subprocess.run(['open', d])
        else:
            subprocess.run(['xdg-open', d])

    run_btn.config(command=start)
    open_btn.config(command=open_dir)
    role_btn.config(command=open_roles)
    root.after(200, poll)
    return {'v': v, 'core': core, 'include_un': include_un, 'role_sum': role_sum, 'state': state, 'last': last,
            'pal_var': pal_var, 'width_var': width_var, 'to_gb': to_gb, 'to_gbps': to_gbps, 'tat_base': tat_base, 'tat_note': tat_note,
            'end_refs': end_refs,
            'set_roles': set_roles, 'open_roles': open_roles, 'start': start, 'log': log,
            'role_btn': role_btn, 'run_btn': run_btn, 'un_chk': un_chk}


def main():
    root = tk.Tk()
    build(root)
    root.mainloop()
