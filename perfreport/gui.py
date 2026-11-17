"""창 화면: 케이스 정보 파일과 데이터 폴더를 고르고, 탭 1 에서 만들 케이스 · 설명 · 기준선을 정한 뒤 [리포트 생성] 을 누르면 docx · html 을 만든다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import compare as CMP
from . import palette as P
from . import profiles as PF
from . import settings as settings_mod
from . import summary as SM
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


COLORS = dict(bg='#f3f5f8', card='#ffffff', line='#d5dbe3', head='#e8edf4', ink='#1f2937', mut='#6b7280',
              blue='#2a6fd6', blue_d='#1f5bb5', green='#1f9d6b', green_d='#17845a', purple='#7a4fbf', purple_d='#6540a3',
              red='#c0392b', red_d='#a93226', case='#edf3fc', sel='#cfe0fb', off='#9aa3af', dark='#2f3a48',
              tab='#e3e8ef', tab_h='#d5dce6', field='#aeb7c2')
NO_BASE = '(기준선 없음)'
NO_REF = '(비교 안 함)'
KINDS = ('BM', 'VM')
FONT = 'Malgun Gothic'


def apply_style(root):
    """테스트 실행 명령 생성기와 같은 모양: clam 테마, 선택한 탭은 파란 바탕 흰 굵은 글씨, 색 버튼."""
    C = COLORS
    st = ttk.Style(root)
    st.theme_use('clam')
    root.configure(bg=C['bg'])
    st.configure('.', font=(FONT, 10), background=C['card'], foreground=C['ink'])
    st.configure('Bg.TFrame', background=C['bg'])
    st.configure('TFrame', background=C['card'])
    st.configure('TLabel', background=C['card'], foreground=C['ink'])
    st.configure('Hint.TLabel', background=C['card'], foreground=C['mut'], font=(FONT, 9))
    st.configure('Head.TLabel', background=C['card'], foreground=C['blue'], font=(FONT, 10, 'bold'))
    st.configure('TCheckbutton', background=C['card'])
    st.configure('TEntry', fieldbackground='#ffffff', bordercolor=C['field'])
    st.configure('TButton', padding=(10, 4), background='#ffffff', bordercolor=C['field'])
    st.map('TButton', background=[('active', C['head'])])
    for name, a, b in (('Green', 'green', 'green_d'), ('Purple', 'purple', 'purple_d'), ('Red', 'red', 'red_d'), ('Blue', 'blue', 'blue_d')):
        st.configure(f'{name}.TButton', background=C[a], foreground='#ffffff', bordercolor=C[b], font=(FONT, 10, 'bold'))
        st.map(f'{name}.TButton', background=[('disabled', C['field']), ('active', C[b])], foreground=[('disabled', '#ffffff')])
    st.configure('TNotebook', background=C['bg'], borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure('TNotebook.Tab', padding=(18, 7), background=C['tab'], foreground=C['ink'], font=(FONT, 10))
    st.map('TNotebook.Tab', background=[('selected', C['blue']), ('active', C['tab_h'])], foreground=[('selected', '#ffffff')],
           font=[('selected', (FONT, 10, 'bold'))], padding=[('selected', (18, 7))])
    st.configure('Treeview', rowheight=26, background='#ffffff', fieldbackground='#ffffff', bordercolor=C['line'])
    st.configure('Treeview.Heading', background=C['head'], foreground=C['ink'], font=(FONT, 10, 'bold'), padding=(4, 5))
    st.map('Treeview', background=[('selected', C['sel'])], foreground=[('selected', C['ink'])])
    st.configure('TCombobox', fieldbackground='#ffffff')
    st.map('TCombobox', fieldbackground=[('readonly', '#ffffff')], foreground=[('readonly', C['ink'])],
           selectbackground=[('readonly', '#ffffff')], selectforeground=[('readonly', C['ink'])])


def base_label(b):
    """기준선 목록에 보이는 글: 이름 · TAT."""
    try:
        return f'{b["name"]} · {float(str(b.get("tat", "")).replace(",", "")):,.2f} s'
    except (TypeError, ValueError):
        return f'{b["name"]} · TAT 없음'


def edit_cell(tree, iid, col, values=None, on_done=None):
    """표의 칸 위에 입력란(values 가 있으면 목록)을 띄워 고친다. Enter · 다른 곳 누름 = 반영, Esc = 취소."""
    box = tree.bbox(iid, col)
    if not box:
        return None
    x, y, w, h = box
    var = tk.StringVar(value=tree.set(iid, col))
    w_ = (ttk.Combobox(tree, textvariable=var, values=list(values), state='readonly') if values is not None
          else ttk.Entry(tree, textvariable=var))
    w_.place(x=x, y=y, width=max(w, 120), height=h)
    w_.focus_set()
    done = {'v': False}

    def commit(_=None):
        if done['v']:
            return
        done['v'] = True
        val = var.get()
        w_.destroy()
        if on_done:
            on_done(iid, col, val)

    def cancel(_=None):
        done['v'] = True
        w_.destroy()

    w_.bind('<Return>', commit)
    w_.bind('<Escape>', cancel)
    w_.bind('<FocusOut>', commit)
    if values is not None:
        w_.bind('<<ComboboxSelected>>', commit)
    else:
        w_.select_range(0, 'end')
    return {'widget': w_, 'var': var, 'commit': commit, 'cancel': cancel}


def profile_bar(parent, kind, hint, get_data, apply_data, default_data):
    """설정 묶음 줄: 목록에서 고르고, 새로 만들기 · 복제 · 이름 바꾸기 · 삭제 · 내보내기 · 가져오기. 값이 바뀌면 save() 로 바로 저장한다.
    get_data(): 지금 화면 값 → dict. apply_data(dict): 묶음 값을 화면에 넣는다. default_data(): 새 묶음의 처음 값.
    반환: 동작 함수 묶음(창 없이 시험할 때는 이름 · 경로를 인자로 준다)."""
    C = COLORS
    key = 'cur_' + kind
    if not PF.names(kind):
        PF.save(kind, PF.DEFAULT_NAME, default_data())
    cur = settings_mod.load().get(key)
    if cur not in PF.names(kind):
        cur = PF.names(kind)[0]
    var = tk.StringVar(value=cur)
    hold = {'loading': False}
    bar = tk.Frame(parent, bg='#f6f8fb', highlightbackground=C['line'], highlightthickness=1, padx=8, pady=5)
    bar.pack(fill='x', pady=(0, 8))
    tk.Label(bar, text=PF.KINDS[kind], bg='#f6f8fb', fg=C['blue'], font=(FONT, 10, 'bold')).pack(side='left', padx=(0, 8))
    cb = ttk.Combobox(bar, textvariable=var, values=PF.names(kind), state='readonly', width=24)
    cb.pack(side='left')

    def save():
        if not hold['loading']:
            PF.save(kind, var.get(), get_data())

    def switch(name):
        hold['loading'] = True
        try:
            var.set(name)
            cb.config(values=PF.names(kind))
            settings_mod.save({key: name})
            apply_data(PF.load(kind, name))
        finally:
            hold['loading'] = False

    def ask(title, init=''):
        return simpledialog.askstring(PF.KINDS[kind], title, initialvalue=init, parent=parent.winfo_toplevel())

    def ok_name(name):
        name = PF.clean_name(name)
        if not name:
            return None
        if name in PF.names(kind):
            messagebox.showwarning(PF.KINDS[kind], f'같은 이름이 이미 있습니다: {name}')
            return None
        return name

    def new(name=None):
        name = ok_name(name if name is not None else ask('새 묶음의 이름'))
        if name:
            PF.save(kind, name, default_data())
            switch(name)
        return name

    def copy(name=None):
        name = ok_name(name if name is not None else ask('복제한 묶음의 이름', var.get() + ' 복사'))
        if name:
            PF.save(kind, name, get_data())
            switch(name)
        return name

    def ren(name=None):
        name = ok_name(name if name is not None else ask('바꿀 이름', var.get()))
        if name and PF.rename(kind, var.get(), name):
            switch(name)
        return name

    def remove(confirm=True):
        if len(PF.names(kind)) <= 1:
            messagebox.showinfo(PF.KINDS[kind], '마지막 하나는 지울 수 없습니다.')
            return False
        if confirm and not messagebox.askyesno(PF.KINDS[kind], f'"{var.get()}" 을 지울까요?'):
            return False
        PF.delete(kind, var.get())
        switch(PF.names(kind)[0])
        return True

    def export(path=None):
        path = path or filedialog.asksaveasfilename(title='내보내기', defaultextension='.json', initialfile=var.get() + '.json',
                                                    filetypes=[('설정 묶음 (JSON)', '*.json')])
        if path:
            save()
            PF.export_to(kind, var.get(), path)
        return path

    def imp(path=None):
        path = path or filedialog.askopenfilename(title='가져오기', filetypes=[('설정 묶음 (JSON)', '*.json'), ('모든 파일', '*.*')])
        if not path:
            return None
        try:
            name = PF.import_from(kind, path)
        except (OSError, ValueError) as e:
            messagebox.showerror(PF.KINDS[kind], f'가져오지 못했습니다: {e}')
            return None
        switch(name)
        return name

    cb.bind('<<ComboboxSelected>>', lambda _e: switch(var.get()))
    for text, cmd, style in (('새로 만들기', new, 'Green.TButton'), ('복제', copy, 'TButton'), ('이름 바꾸기', ren, 'TButton'), ('삭제', remove, 'TButton'),
                             ('내보내기', export, 'Blue.TButton'), ('가져오기', imp, 'Blue.TButton')):
        ttk.Button(bar, text=text, style=style, command=cmd, width=len(text) * 2 + 1).pack(side='left', padx=(6, 0))
    tk.Label(bar, text=hint, bg='#f6f8fb', fg=C['mut'], font=(FONT, 9)).pack(side='left', padx=10)
    return {'var': var, 'save': save, 'switch': switch, 'new': new, 'copy': copy, 'rename': ren, 'remove': remove, 'export': export,
            'import': imp, 'load_current': lambda: switch(var.get()), 'combo': cb}


def build(root):
    """기본 창을 구성한다. 반환: 입력 값과 동작 함수(창 없이 시험할 때 쓴다).
    탭 1 테스트 케이스(생성 선택 · 설명 수정 · 기준선), 탭 2 기준선(사전 정의), 탭 3 케이스 리포트 옵션, 탭 4 생성."""
    from .build import cases_from_csv
    C = COLORS
    root.title('성능 리포트 생성기')
    root.geometry('1320x880')
    root.minsize(1180, 740)
    apply_style(root)
    saved = settings_mod.load()
    v = {k: tk.StringVar(value=str(saved.get('path_' + k, ''))) for k in ('cases', 'data', 'out', 'split', 'prev')}
    v.update({k: tk.StringVar() for k in ('title', 'selection')})
    core = tk.BooleanVar(value=False)
    include_un = tk.BooleanVar(value=True)
    role_sum = tk.StringVar(value=R.summary({}))
    pal_var = tk.StringVar(value=P.PALETTES[P.DEFAULT_PALETTE][0])
    width_var = tk.StringVar(value='16.5')
    to_gb = tk.BooleanVar(value=False)      # 단위 변환: 기본은 원래 단위 그대로
    to_gbps = tk.BooleanVar(value=False)
    end_refs = tk.StringVar()               # 종료 기준 VM 기본값 (옵션 프로파일에 저장)
    vm_inc = tk.StringVar()                 # 포함할 VM 기본값 (옵션 프로파일에 저장)
    html_var = tk.BooleanVar(value=True)    # 케이스 리포트의 HTML 도 만들기
    gen_case = tk.BooleanVar(value=bool(saved.get('gen_case', True)))     # 생성 탭: 케이스 리포트 만들기
    gen_cmp = tk.BooleanVar(value=bool(saved.get('gen_cmp', False)))      # 생성 탭: 비교 리포트 만들기
    bulk_var = tk.StringVar(value=NO_BASE)
    vmf_var = tk.StringVar()                # 포함할 VM 일괄 적용 입력 칸
    count_var = tk.StringVar(value='케이스 정보 파일을 고르면 케이스가 표로 나온다')
    q = queue.Queue()
    last = {}
    ref_bulk = tk.StringVar(value=NO_REF)
    prev_info = tk.StringVar()
    state = {'roles': {}, 'cases': [], 'rows': {}, 'edit': None, 'baselines': [], 'metric_names': [], 'priors': [],
             'cmp_rows': [], 'cmp_metrics': []}
    # 이전 판이 settings.json 에 둔 기준선(또는 더 옛 판의 'TAT 비교 기준')은 기준선 프로파일 '기본' 의 처음 값으로 옮긴다
    legacy = [dict(b) for b in saved.get('baselines', []) if isinstance(b, dict) and b.get('name')]
    if not legacy and str(saved.get('tat_baseline', '')).strip():
        legacy.append({'name': str(saved.get('tat_baseline_note') or '기준선').strip(), 'kind': 'BM', 'cond': '',
                       'tat': str(saved['tat_baseline']).strip(), 'note': '이전 판의 TAT 비교 기준에서 옮김'})

    tk.Label(root, text='  성능 리포트 생성기', bg=C['dark'], fg='#ffffff', font=(FONT, 13, 'bold'), anchor='w', pady=7).pack(fill='x')

    # ── 입력 (경로): 바뀌면 바로 저장
    card = ttk.Frame(root, padding=(12, 8))
    card.pack(fill='x', padx=8, pady=(8, 4))
    for c_ in (1, 4):
        card.columnconfigure(c_, weight=1)
    ttk.Label(card, text='입력', style='Head.TLabel').grid(row=0, column=0, sticky='w')
    ttk.Label(card, text='바뀌면 바로 저장된다. 데이터 폴더는 하위 폴더까지 CSV · 로그 파일을 모두 읽는다', style='Hint.TLabel').grid(row=0, column=1, columnspan=5, sticky='w')

    def path_row(r, c0, label, key, pick):
        ttk.Label(card, text=label).grid(row=r, column=c0, sticky='w', pady=3, padx=(0 if c0 == 0 else 14, 6))
        ttk.Entry(card, textvariable=v[key]).grid(row=r, column=c0 + 1, sticky='we')
        ttk.Button(card, text='찾기', command=pick, width=6).grid(row=r, column=c0 + 2, padx=(6, 0))

    def pick_cases():
        p = filedialog.askopenfilename(title='케이스 정보 파일', filetypes=[('케이스 정보 (CSV · 엑셀)', '*.csv *.xlsx'), ('모든 파일', '*.*')])
        if p:
            v['cases'].set(p)
            if not v['data'].get():
                v['data'].set(os.path.dirname(p))
            if not v['out'].get():
                v['out'].set(os.path.join(os.path.dirname(p), 'out_' + os.path.splitext(os.path.basename(p))[0]))
            load_cases()

    def pick_dir(key, title):
        return lambda: v[key].set(filedialog.askdirectory(title=title) or v[key].get())

    path_row(1, 0, '케이스 정보 파일', 'cases', pick_cases)
    path_row(1, 3, '데이터 폴더', 'data', pick_dir('data', '데이터 폴더'))
    path_row(2, 0, '결과 폴더', 'out', pick_dir('out', '결과 폴더'))
    path_row(2, 3, 'TC별 정리 폴더 (선택)', 'split', pick_dir('split', 'TC별 정리 폴더'))
    # 이전 결과 폴더: 이전에 만든 결과 폴더(case_summary 가 있는 곳). 여러 개는 ; 로 구분. 비교 기준 케이스와 비교 리포트에 쓴다
    ttk.Label(card, text='이전 결과 폴더 (선택)').grid(row=3, column=0, sticky='w', pady=3, padx=(0, 6))
    ttk.Entry(card, textvariable=v['prev']).grid(row=3, column=1, columnspan=3, sticky='we')

    def add_prev():
        d = filedialog.askdirectory(title='이전 결과 폴더 (이전에 만든 결과 폴더)')
        if d:
            cur = [x.strip() for x in v['prev'].get().split(';') if x.strip()]
            if d not in cur:
                v['prev'].set(';'.join(cur + [d]))
    ttk.Button(card, text='폴더 추가', command=add_prev).grid(row=3, column=4, sticky='w', padx=(6, 0))
    ttk.Label(card, textvariable=prev_info, style='Hint.TLabel').grid(row=3, column=4, columnspan=2, sticky='e')

    def scan_prev(*_):
        state['priors'] = SM.scan([x.strip() for x in v['prev'].get().split(';') if x.strip()])
        prev_info.set(f'이전 결과 케이스 {len(state["priors"])}건' if v['prev'].get().strip() else '')
        if 'refresh_cases' in hooks:
            hooks['refresh_cases']()
            hooks['cmp_sync']()

    def save_paths(*_):
        settings_mod.save({'path_' + k: v[k].get() for k in ('cases', 'data', 'out', 'split', 'prev')})
    hooks = {}
    for k in ('cases', 'data', 'out', 'split', 'prev'):
        v[k].trace_add('write', save_paths)
    v['prev'].trace_add('write', scan_prev)
    scan_prev()

    nb = ttk.Notebook(root)
    nb.pack(fill='both', expand=True, padx=8, pady=(4, 8))
    tab1, tab2, tab3, tabc, tab4 = (ttk.Frame(nb, padding=10) for _ in range(5))      # tabc = 비교 리포트, tab4 = 생성
    for t_, name in ((tab1, '1. 테스트 케이스'), (tab2, '2. 기준선'), (tab3, '3. 케이스 리포트 옵션'), (tabc, '4. 비교 리포트'), (tab4, '5. 생성')):
        nb.add(t_, text=name)

    # ── 기준선 (탭 2 에서 정의, 탭 1 에서 케이스마다 선택)
    def base_by_name(name):
        return next((b for b in state['baselines'] if b['name'] == name), None)

    def base_choices():
        return [NO_BASE] + [base_label(b) for b in state['baselines']]

    def name_of_choice(text):
        return '' if text == NO_BASE else text.split(' · ')[0]

    def save_baselines():
        pb_base['save']()
        bulk_cb.config(values=base_choices())
        cmp_base_cb.config(values=base_choices())
        refresh_cases()

    # ── 탭 1: 테스트 케이스
    bar = ttk.Frame(tab1)
    bar.pack(fill='x', pady=(0, 6))
    bar3 = ttk.Frame(tab1)
    bar3.pack(fill='x', pady=(0, 6))
    bar2 = ttk.Frame(tab1)
    bar2.pack(fill='x', pady=(0, 8))
    COLS = (('gen', '생성', 46, 'center'), ('id', '케이스명', 140, 'w'), ('label', '설명 (수정 가능)', 180, 'w'), ('group', '테스트 그룹', 90, 'w'),
            ('host', '호스트', 80, 'w'), ('start', '잡 시작', 135, 'w'), ('end', '잡 종료', 135, 'w'), ('vms', '포함할 VM (수정 가능)', 150, 'w'),
            ('end_refs', '종료 기준 VM', 100, 'w'), ('base', '기준선 (TAT)', 170, 'w'), ('ref', '비교 기준 케이스', 190, 'w'))
    wrap = ttk.Frame(tab1)
    wrap.pack(fill='both', expand=True)
    tree = ttk.Treeview(wrap, columns=[c_[0] for c_ in COLS], show='headings', selectmode='extended')
    for key, text, w_, anchor in COLS:
        tree.heading(key, text=text, anchor='w' if anchor == 'w' else 'center')
        tree.column(key, width=w_, anchor=anchor, stretch=key in ('label', 'vms'))
    tree.tag_configure('on', background=C['case'])
    tree.tag_configure('off', background='#ffffff', foreground=C['off'])
    sy = ttk.Scrollbar(wrap, orient='vertical', command=tree.yview)
    sx = ttk.Scrollbar(wrap, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    wrap.columnconfigure(0, weight=1)
    wrap.rowconfigure(0, weight=1)
    tree.grid(row=0, column=0, sticky='nsew')
    sy.grid(row=0, column=1, sticky='ns')
    sx.grid(row=1, column=0, sticky='we')
    ttk.Label(tab1, text='생성 칸을 누르면 체크가 바뀐다. 설명 · 포함할 VM · 기준선 · 비교 기준 케이스 칸은 두 번 눌러 고친다. 체크한 케이스만 리포트를 만든다. '
                         '고친 내용은 자동 저장되고 원본 파일은 바꾸지 않는다.', style='Hint.TLabel').pack(anchor='w', pady=(6, 0))
    ttk.Label(tab1, textvariable=count_var, style='Hint.TLabel').pack(anchor='w')

    def case_key():
        return os.path.abspath(v['cases'].get()) if v['cases'].get() else ''

    def save_cases():
        allst = dict(settings_mod.load().get('case_state', {}))
        allst[case_key()] = {cid: dict(r) for cid, r in state['rows'].items()}
        settings_mod.save({'case_state': allst})

    def refresh_cases():
        tree.delete(*tree.get_children())
        n = 0
        for c_ in state['cases']:
            r = state['rows'][c_['id']]
            b = base_by_name(r.get('base', ''))
            btxt = base_label(b) if b else (f'{r["base"]} (없는 기준선)' if r.get('base') else NO_BASE)
            n += bool(r['gen'])
            tree.insert('', 'end', iid=c_['id'], tags=('on' if r['gen'] else 'off',), values=(
                '☑' if r['gen'] else '☐', c_['id'], r['label'], c_.get('group') or '', c_.get('host') or '',
                f'{c_["start"]:%Y-%m-%d %H:%M:%S}', '자동' if c_.get('end_auto') else f'{c_["end"]:%Y-%m-%d %H:%M:%S}',
                r['vms'], c_.get('end_refs') or '', btxt, ref_text(r.get('ref', ''))))
        if state['cases']:
            count_var.set(f'케이스 {len(state["cases"])}건 중 {n}건 생성 · 기준선 지정 {sum(1 for r in state["rows"].values() if base_by_name(r.get("base", "")))}건')

    def load_cases(show_error=True):
        p = v['cases'].get()
        if not os.path.isfile(p):
            if show_error:
                messagebox.showwarning('입력', '케이스 정보 파일(CSV 또는 엑셀)을 고르십시오.')
            return False
        try:
            cases = cases_from_csv(p)
        except SystemExit as e:
            if show_error:
                messagebox.showerror('케이스 정보 파일', str(e))
            return False
        except Exception as e:  # 화면에 원인을 그대로 보인다
            if show_error:
                messagebox.showerror('케이스 정보 파일', f'{type(e).__name__}: {e}')
            return False
        old = settings_mod.load().get('case_state', {}).get(case_key(), {})
        state['cases'] = cases
        state['rows'] = {}
        for c_ in cases:
            o = old.get(c_['id'], {}) if isinstance(old.get(c_['id']), dict) else {}
            state['rows'][c_['id']] = {'gen': bool(o.get('gen', True)), 'label': str(o.get('label', c_.get('label') or '')),
                                       'base': str(o.get('base', '')), 'vms': str(o.get('vms', c_.get('vms') or '')),
                                       'ref': str(o.get('ref', ''))}
        refresh_cases()
        cmp_sync()
        return True

    def set_gen(ids, val):
        for cid in ids:
            state['rows'][cid]['gen'] = (not state['rows'][cid]['gen']) if val is None else val
        save_cases()
        refresh_cases()

    def cell_done(iid, col, val):
        key = tree['columns'][int(col[1:]) - 1]
        if key == 'label':
            state['rows'][iid]['label'] = val.strip()
        elif key == 'base':
            state['rows'][iid]['base'] = name_of_choice(val)
        elif key == 'vms':
            state['rows'][iid]['vms'] = val.strip()
        elif key == 'ref':
            state['rows'][iid]['ref'] = '' if ref_key(val) == iid else ref_key(val)
        save_cases()
        refresh_cases()

    def on_click(e):
        if tree.identify_region(e.x, e.y) == 'cell' and tree.identify_column(e.x) == '#1':
            iid = tree.identify_row(e.y)
            if iid:
                set_gen([iid], None)
                return 'break'
        return None

    def on_double(e):
        iid, col = tree.identify_row(e.y), tree.identify_column(e.x)
        if not iid or not col:
            return
        key = tree['columns'][int(col[1:]) - 1]
        if key in ('label', 'vms'):
            state['edit'] = edit_cell(tree, iid, col, None, cell_done)
        elif key == 'ref':
            state['edit'] = edit_cell(tree, iid, col, [x for x in ref_choices() if x != iid], cell_done)
        elif key == 'base':
            state['edit'] = edit_cell(tree, iid, col, base_choices(), cell_done)

    def apply_base(all_rows):
        ids = [c_['id'] for c_ in state['cases']] if all_rows else list(tree.selection())
        if not ids:
            messagebox.showinfo('기준선 일괄 적용', '표에서 적용할 케이스를 먼저 고르십시오. (Shift · Ctrl 로 여러 줄 선택)')
            return
        for cid in ids:
            state['rows'][cid]['base'] = name_of_choice(bulk_var.get())
        save_cases()
        refresh_cases()
        tree.selection_set([i for i in ids if tree.exists(i)])

    def ref_choices():
        """비교 기준 케이스 목록: 비교 안 함 + 이번 파일의 케이스 + 이전 결과 폴더의 케이스(이름 뒤에 결과 날짜)."""
        return [NO_REF] + [c_['id'] for c_ in state['cases']] + [p_['display'] for p_ in state['priors']]

    def ref_key(text):
        if not text or text == NO_REF:
            return ''
        p_ = next((x for x in state['priors'] if x['display'] == text), None)
        return p_['key'] if p_ else text

    def ref_text(key):
        if not key:
            return NO_REF
        if SM.is_prior(key):
            p_ = next((x for x in state['priors'] if x['key'] == key), None)
            return p_['display'] if p_ else f'(찾을 수 없는 이전 결과: {os.path.splitext(os.path.basename(key))[0]})'
        return key if any(c_['id'] == key for c_ in state['cases']) else f'{key} (없는 케이스)'

    def apply_ref(all_rows):
        ids = [c_['id'] for c_ in state['cases']] if all_rows else list(tree.selection())
        if not ids:
            messagebox.showinfo('비교 기준 케이스 일괄 적용', '표에서 적용할 케이스를 먼저 고르십시오. (Shift · Ctrl 로 여러 줄 선택)')
            return
        k = ref_key(ref_bulk.get())
        for cid in ids:
            state['rows'][cid]['ref'] = '' if k == cid else k        # 자기 자신은 비교 기준이 될 수 없다
        save_cases()
        refresh_cases()
        tree.selection_set([i for i in ids if tree.exists(i)])

    def apply_vms(all_rows):
        ids = [c_['id'] for c_ in state['cases']] if all_rows else list(tree.selection())
        if not ids:
            messagebox.showinfo('포함할 VM 일괄 적용', '표에서 적용할 케이스를 먼저 고르십시오. (Shift · Ctrl 로 여러 줄 선택)')
            return
        for cid in ids:
            state['rows'][cid]['vms'] = vmf_var.get().strip()
        save_cases()
        refresh_cases()
        tree.selection_set([i for i in ids if tree.exists(i)])

    tree.bind('<Button-1>', on_click)
    tree.bind('<Double-1>', on_double)
    ttk.Button(bar, text='다시 불러오기', style='Green.TButton', command=load_cases).pack(side='left')
    ttk.Button(bar, text='전체 선택', command=lambda: set_gen(list(state['rows']), True)).pack(side='left', padx=(8, 0))
    ttk.Button(bar, text='전체 해제', command=lambda: set_gen(list(state['rows']), False)).pack(side='left', padx=(4, 0))
    ttk.Label(bar, text='기준선 일괄 적용').pack(side='left', padx=(24, 6))
    bulk_cb = ttk.Combobox(bar, textvariable=bulk_var, values=base_choices(), state='readonly', width=28)
    bulk_cb.pack(side='left')
    ttk.Button(bar, text='전체 케이스에 적용', style='Purple.TButton', command=lambda: apply_base(True)).pack(side='left', padx=(6, 0))
    ttk.Button(bar, text='선택한 케이스에 적용', style='Purple.TButton', command=lambda: apply_base(False)).pack(side='left', padx=(4, 0))
    ttk.Label(bar3, text='비교 기준 케이스 일괄 적용').pack(side='left', padx=(0, 6))
    ref_cb = ttk.Combobox(bar3, textvariable=ref_bulk, values=[NO_REF], state='readonly', width=34, postcommand=lambda: ref_cb.config(values=ref_choices()))
    ref_cb.pack(side='left')
    ttk.Button(bar3, text='전체 케이스에 적용', style='Purple.TButton', command=lambda: apply_ref(True)).pack(side='left', padx=(6, 0))
    ttk.Button(bar3, text='선택한 케이스에 적용', style='Purple.TButton', command=lambda: apply_ref(False)).pack(side='left', padx=(4, 0))
    ttk.Label(bar3, text='문서에 기준 케이스 대비 TAT 표와 phase 막대가 들어간다',
              style='Hint.TLabel').pack(side='left', padx=10)
    ttk.Label(bar2, text='포함할 VM 일괄 적용').pack(side='left', padx=(0, 6))
    ttk.Entry(bar2, textvariable=vmf_var, width=30).pack(side='left')
    ttk.Button(bar2, text='전체 케이스에 적용', style='Purple.TButton', command=lambda: apply_vms(True)).pack(side='left', padx=(6, 0))
    ttk.Button(bar2, text='선택한 케이스에 적용', style='Purple.TButton', command=lambda: apply_vms(False)).pack(side='left', padx=(4, 0))
    ttk.Label(bar2, text='이름에 이 글자가 든 VM 만 (쉼표로 여러 개). 비우면 탭 3 기본값, 그것도 비면 전부',
              style='Hint.TLabel').pack(side='left', padx=10)

    # ── 탭 2: 기준선 사전 정의 (기준선 프로파일: 서버마다 하나씩 두고 바꿔 쓴다)
    def apply_bases(d):
        state['baselines'] = [dict(b) for b in d.get('baselines', []) if isinstance(b, dict) and b.get('name')]
        refresh_bases()
        bulk_cb.config(values=base_choices())
        cmp_base_cb.config(values=base_choices())
        refresh_cases()

    pb_base = profile_bar(tab2, 'baselines', '서버마다 하나씩 두고 바꿔 쓴다. 바뀌면 바로 저장',
                          lambda: {'baselines': state['baselines']}, apply_bases, lambda: {'baselines': legacy})
    bbar = ttk.Frame(tab2)
    bbar.pack(fill='x', pady=(0, 8))
    BCOLS = (('name', '기준선 이름', 180), ('kind', '구분 (BM · VM)', 110), ('cond', '조건', 420), ('tat', 'TAT (s)', 120), ('note', '비고', 260))
    btree = ttk.Treeview(tab2, columns=[c_[0] for c_ in BCOLS], show='headings', selectmode='browse', height=12)
    for key, text, w_ in BCOLS:
        btree.heading(key, text=text, anchor='w')
        btree.column(key, width=w_, anchor='w', stretch=key in ('cond', 'note'))
    btree.pack(fill='both', expand=True)
    ttk.Label(tab2, text='칸을 두 번 눌러 고친다. 바뀌면 바로 저장된다. 탭 1 의 기준선 목록에 이름과 TAT 가 나오고, 리포트에 '
                         'Degradation = (TAT − 기준선 TAT) ÷ 기준선 TAT × 100 을 적는다. 기준선은 BM 의 TAT 일 수도, 다른 VM 의 TAT 일 수도 있다.',
              style='Hint.TLabel', wraplength=1150, justify='left').pack(anchor='w', pady=(6, 0))

    def refresh_bases(select=None):
        btree.delete(*btree.get_children())
        for i, b in enumerate(state['baselines']):
            btree.insert('', 'end', iid=str(i), values=(b['name'], b.get('kind', ''), b.get('cond', ''), b.get('tat', ''), b.get('note', '')))
        if select is not None and btree.exists(str(select)):
            btree.selection_set(str(select))

    def base_done(iid, col, val):
        key = btree['columns'][int(col[1:]) - 1]
        b = state['baselines'][int(iid)]
        val = val.strip()
        if key == 'name':
            if not val or any(o is not b and o['name'] == val for o in state['baselines']):
                messagebox.showwarning('기준선', '기준선 이름은 비울 수 없고 서로 달라야 합니다.')
                return
            for allrows in [state['rows']]:          # 이 기준선을 쓰는 케이스의 이름도 같이 바꾼다
                for r in allrows.values():
                    if r.get('base') == b['name']:
                        r['base'] = val
            if state['cases']:
                save_cases()
        if key == 'tat' and val:
            try:
                if float(val.replace(',', '')) <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('기준선', 'TAT 는 0 보다 큰 숫자(초)로 적으십시오. 예: 21721.65')
                return
        b[key] = val
        refresh_bases(iid)
        save_baselines()

    def base_double(e):
        iid, col = btree.identify_row(e.y), btree.identify_column(e.x)
        if iid and col:
            key = btree['columns'][int(col[1:]) - 1]
            state['edit'] = edit_cell(btree, iid, col, KINDS if key == 'kind' else None, base_done)

    def base_add():
        n = 1
        while base_by_name(f'기준선{n}'):
            n += 1
        state['baselines'].append({'name': f'기준선{n}', 'kind': 'BM', 'cond': '', 'tat': '', 'note': ''})
        refresh_bases(len(state['baselines']) - 1)
        save_baselines()

    def base_del():
        sel = btree.selection()
        if sel and messagebox.askyesno('기준선', f'기준선 "{state["baselines"][int(sel[0])]["name"]}" 을 지울까요? 이 기준선을 쓰는 케이스는 기준선 없음이 됩니다.'):
            name = state['baselines'].pop(int(sel[0]))['name']
            for r in state['rows'].values():
                if r.get('base') == name:
                    r['base'] = ''
            if state['cases']:
                save_cases()
            refresh_bases()
            save_baselines()

    def base_move(d):
        sel = btree.selection()
        if not sel:
            return
        i = int(sel[0])
        j = i + d
        if 0 <= j < len(state['baselines']):
            state['baselines'][i], state['baselines'][j] = state['baselines'][j], state['baselines'][i]
            refresh_bases(j)
            save_baselines()

    btree.bind('<Double-1>', base_double)
    ttk.Button(bbar, text='+ 기준선 추가', style='Green.TButton', command=base_add).pack(side='left')
    ttk.Button(bbar, text='삭제', command=base_del).pack(side='left', padx=(8, 0))
    ttk.Button(bbar, text='위로', command=lambda: base_move(-1)).pack(side='left', padx=(4, 0))
    ttk.Button(bbar, text='아래로', command=lambda: base_move(1)).pack(side='left', padx=(4, 0))

    # ── 탭 3: 케이스 리포트 옵션
    t3 = ttk.Frame(tab3)                   # 묶음 줄(pack) 아래에 옵션 칸(grid)
    t3.columnconfigure(1, weight=1)

    def opt_label(r, text):
        ttk.Label(t3, text=text, width=20).grid(row=r, column=0, sticky='w', pady=5)

    def hint(r, text):
        ttk.Label(t3, text=text, style='Hint.TLabel', wraplength=980, justify='left').grid(row=r, column=1, sticky='w')

    opt_label(0, 'HTML 제목 (선택)')
    ttk.Entry(t3, textvariable=v['title']).grid(row=0, column=1, sticky='we')
    hint(1, '비우면 테스트 그룹마다 그 그룹의 케이스 이름으로 만든다')
    cf = ttk.Frame(t3)
    cf.grid(row=2, column=1, sticky='w', pady=3)
    ttk.Checkbutton(cf, text='HTML 도 만들기 (테스트 그룹마다 1개, 생성한 케이스를 담는다)', variable=html_var).pack(side='left')
    ttk.Checkbutton(cf, text='VM 핵심 지표만 넣기 (CPU · 메모리 등 8종)', variable=core).pack(side='left', padx=20)
    opt_label(3, '그래프 색 계열')
    pf = ttk.Frame(t3)
    pf.grid(row=3, column=1, sticky='w')
    ttk.Combobox(pf, textvariable=pal_var, values=[n for n, _ in P.PALETTES], state='readonly', width=22).pack(side='left')
    pal_cv = tk.Canvas(pf, width=8 * 34 + 4, height=22, bg=C['card'], highlightthickness=0)     # 고른 색 계열의 실제 색 (VM 순서대로)
    pal_cv.pack(side='left', padx=10)
    ttk.Label(pf, text='왼쪽부터 VM 순서대로 쓰는 색', style='Hint.TLabel').pack(side='left')

    def draw_palette(*_):
        pal_cv.delete('all')
        for i, col in enumerate(P.palette_colors(P.palette_index(pal_var.get()))[:8]):
            pal_cv.create_rectangle(2 + i * 34, 2, 2 + i * 34 + 28, 20, fill=col, outline=C['mut'])
    pal_var.trace_add('write', draw_palette)
    draw_palette()
    opt_label(4, '그래프 폭 (cm)')
    wf = ttk.Frame(t3)
    wf.grid(row=4, column=1, sticky='w')
    ttk.Spinbox(wf, from_=10, to=31.5, increment=0.5, textvariable=width_var, width=6).pack(side='left')
    ttk.Label(wf, text='기본 16.5 (A4 세로). 넓히면 A4 가로, 27.7 을 넘으면 A3 가로. 컨플루언스로 가져올 문서는 31.5 (최대)',
              style='Hint.TLabel').pack(side='left', padx=8)
    opt_label(5, '지표 역할 (선택)')
    rf = ttk.Frame(t3)
    rf.grid(row=5, column=1, sticky='we')
    role_btn = ttk.Button(rf, text='지표 역할 지정…')
    role_btn.pack(side='left')
    ttk.Label(rf, textvariable=role_sum, wraplength=820, justify='left').pack(side='left', padx=8)
    un_chk = ttk.Checkbutton(t3, text='역할을 지정하지 않은 지표도 넣기 (문서는 Appendix A)', variable=include_un, state='disabled')
    un_chk.grid(row=6, column=1, sticky='w')
    opt_label(7, 'HTML 선택 파일 (선택)')
    sf = ttk.Frame(t3)
    sf.grid(row=7, column=1, sticky='we')
    sf.columnconfigure(0, weight=1)
    ttk.Entry(sf, textvariable=v['selection']).grid(row=0, column=0, sticky='we')
    ttk.Button(sf, text='찾기', width=6, command=lambda: v['selection'].set(
        filedialog.askopenfilename(title='HTML 선택 파일', filetypes=[('HTML 선택 파일 (JSON)', '*.json'), ('모든 파일', '*.*')]) or v['selection'].get())
    ).grid(row=0, column=1, padx=(6, 0))
    hint(8, "HTML 의 '선택 내보내기' 로 저장한 파일. 지정하면 그 케이스 · VM · 지표만으로 만든다")
    opt_label(9, '단위 변환 (선택)')
    uf = ttk.Frame(t3)
    uf.grid(row=9, column=1, sticky='w')
    ttk.Checkbutton(uf, text='크기를 GB 로 (KB · MB · Bytes → GB)', variable=to_gb).pack(side='left')
    ttk.Checkbutton(uf, text='네트워크 속도를 Gbps 로 (KBps · Mbps → Gbps)', variable=to_gbps).pack(side='left', padx=12)
    opt_label(12, '포함할 VM (기본값)')
    ttk.Entry(t3, textvariable=vm_inc, width=40).grid(row=12, column=1, sticky='w')
    hint(13, '탭 1 의 포함할 VM 칸이 빈 케이스에 쓴다. VM 이름에 이 글자가 들어 있는 VM 만 넣는다(쉼표로 여러 개). 비우면 호스트의 VM 전부')
    opt_label(10, '종료 기준 VM (기본값)')
    ttk.Entry(t3, textvariable=end_refs).grid(row=10, column=1, sticky='we')
    hint(11, '잡 종료 시각을 비운 케이스에서 이 VM 들의 잡 종료(TAT 로그)와 CPU 사용률로 종료 시각을 정한다. 여러 대면 가장 늦게 끝난 VM 기준. '
             '이름 · 패턴을 ; 로 구분 (예: *app01;*app02). 비우면 케이스의 모든 VM. 케이스 정보에 종료 기준 VM 열이 있으면 그 값이 먼저다')

    def get_opts():
        return {'title': v['title'].get(), 'core': core.get(), 'html': html_var.get(), 'palette': pal_var.get(), 'width': width_var.get(),
                'roles': {m: [r, o] for m, (r, o) in state['roles'].items()}, 'include_un': include_un.get(),
                'selection': v['selection'].get(), 'to_gb': to_gb.get(), 'to_gbps': to_gbps.get(),
                'vm_include': vm_inc.get(), 'end_refs': end_refs.get()}

    def apply_opts(d):
        v['title'].set(str(d.get('title', '')))
        core.set(bool(d.get('core', False)))
        html_var.set(bool(d.get('html', True)))
        pal_var.set(d.get('palette') if d.get('palette') in [n for n, _ in P.PALETTES] else P.PALETTES[P.DEFAULT_PALETTE][0])
        width_var.set(str(d.get('width', '16.5')))
        try:
            set_roles({m: (x[0], int(x[1])) for m, x in (d.get('roles') or {}).items() if x and x[0] in R.ROLES})
        except (TypeError, ValueError, IndexError):
            set_roles({})
        include_un.set(bool(d.get('include_un', True)))
        v['selection'].set(str(d.get('selection', '')))
        to_gb.set(bool(d.get('to_gb', False)))
        to_gbps.set(bool(d.get('to_gbps', False)))
        vm_inc.set(str(d.get('vm_include', '')))
        end_refs.set(str(d.get('end_refs', '')))

    pb_opt = profile_bar(tab3, 'case_options', '이 탭의 값 전부. 바뀌면 바로 저장',
                         get_opts, apply_opts, lambda: {'end_refs': str(saved.get('end_ref_vms', ''))})
    t3.pack(fill='both', expand=True)
    for var_ in (v['title'], v['selection'], core, html_var, pal_var, width_var, include_un, to_gb, to_gbps, vm_inc, end_refs):
        var_.trace_add('write', lambda *_: pb_opt['save']())

    # ── 탭 4: 비교 리포트 (만들 때만 쓴다. 생성 탭에서 '비교 리포트' 를 체크하지 않으면 이 탭의 값은 보지 않는다)
    cmp_title = tk.StringVar()
    cmp_base = tk.StringVar(value=NO_BASE)
    cmp_phase = tk.BooleanVar(value=False)
    cmp_pcase = tk.StringVar()
    cmp_word = tk.BooleanVar(value=True)
    cmp_html = tk.BooleanVar(value=True)
    cmp_add = tk.StringVar(value=CMP.TAT)
    cmp_pal = tk.StringVar(value=P.PALETTES[P.DEFAULT_PALETTE][0])
    hold = {'cmp': False}
    VAL_NAMES = {v_: k for k, v_ in CMP.VALUES.items()}
    CHART_NAMES = {v_: k for k, v_ in CMP.CHARTS.items()}
    PAL_NAMES = [n for n, _ in P.PALETTES]

    def cmp_save_preset():
        pb_cmp['save']()

    def apply_preset(d):
        state['cmp_metrics'] = [dict(CMP.normalize(x), on=bool(x.get('on', True))) for x in d.get('metrics', []) if isinstance(x, dict) and x.get('metric')]
        cmp_word.set(bool(d.get('word', True)))
        cmp_html.set(bool(d.get('html', True)))
        cmp_refresh_metrics()

    pb_cmp = profile_bar(tabc, 'compare_presets', '지표 목록과 형식을 묶음으로 저장',
                         lambda: {'metrics': state['cmp_metrics'], 'word': cmp_word.get(), 'html': cmp_html.get()}, apply_preset,
                         lambda: {'metrics': [dict(CMP.default_item(CMP.TAT), on=True), dict(CMP.default_item(CMP.PHASE), on=True)], 'word': True, 'html': True})
    crow = ttk.Frame(tabc)
    crow.pack(fill='x', pady=(0, 6))
    ttk.Label(crow, text='제목').pack(side='left')
    ttk.Entry(crow, textvariable=cmp_title, width=26).pack(side='left', padx=(6, 14))
    ttk.Label(crow, text='리포트 기준선').pack(side='left')
    cmp_base_cb = ttk.Combobox(crow, textvariable=cmp_base, values=[NO_BASE], state='readonly', width=24)
    cmp_base_cb.pack(side='left', padx=(6, 14))
    ttk.Label(crow, text='출력').pack(side='left')
    ttk.Checkbutton(crow, text='Word', variable=cmp_word).pack(side='left', padx=(6, 0))
    ttk.Checkbutton(crow, text='HTML (비교하는 케이스를 담은 HTML)', variable=cmp_html).pack(side='left', padx=(8, 0))
    crow2 = ttk.Frame(tabc)
    crow2.pack(fill='x', pady=(0, 8))
    ttk.Checkbutton(crow2, text='시계열 그래프 배경에 phase 구간 표시 · 기준 케이스', variable=cmp_phase).pack(side='left')
    cmp_pcase_cb = ttk.Combobox(crow2, textvariable=cmp_pcase, values=[], state='readonly', width=24)
    cmp_pcase_cb.pack(side='left', padx=(6, 14))
    ttk.Label(crow2, text='리포트 기준선을 고르면 TAT 막대에 점선과 Degradation 을 적는다. (기준선 없음) 이면 적지 않는다',
              style='Hint.TLabel').pack(side='left')
    cpw = ttk.Frame(tabc)
    cpw.pack(fill='both', expand=True)
    cpw.columnconfigure(0, weight=5, uniform='cmp')
    cpw.columnconfigure(1, weight=6, uniform='cmp')
    cpw.rowconfigure(0, weight=1)
    cl = ttk.Frame(cpw)
    cl.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
    cr = ttk.Frame(cpw)
    cr.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
    # 왼쪽: 비교할 케이스
    lb = ttk.Frame(cl)
    lb.pack(fill='x', pady=(0, 4))
    lb2 = ttk.Frame(cl)
    lb2.pack(fill='x', pady=(0, 4))
    lb3 = ttk.Frame(cl)
    lb3.pack(fill='x', pady=(0, 6))
    ttk.Label(lb, text='비교할 테스트 케이스', style='Head.TLabel').pack(side='left', padx=(0, 10))
    ctree = ttk.Treeview(cl, columns=('sel', 'ord', 'id', 'name', 'pal'), show='headings', selectmode='browse', height=8)
    for key, text, w_, anchor in (('sel', '선택', 46, 'center'), ('ord', '순서', 46, 'center'), ('id', '케이스명', 170, 'w'),
                                  ('name', '표시 이름', 150, 'w'), ('pal', '색 계열', 130, 'w')):
        ctree.heading(key, text=text, anchor='w' if anchor == 'w' else 'center')
        ctree.column(key, width=w_, anchor=anchor, stretch=key in ('id', 'name'))
    ctree.tag_configure('on', background=C['case'])
    ctree.tag_configure('off', foreground=C['off'])
    ctree.pack(fill='both', expand=True)
    ttk.Label(cl, text='선택 칸을 누르면 체크가 바뀐다. 순서 = 막대그래프 가로축 순서(위로 · 아래로). 표시 이름은 두 번 눌러 고친다.\n'
                       '색: 표에서 케이스를 고르고 위의 색 계열 목록에서 바꾼다. VM 은 그 계열의 색을 왼쪽부터 순서대로 받는다.',
              style='Hint.TLabel', justify='left').pack(anchor='w', pady=(4, 2))
    cmp_cv = tk.Canvas(cl, height=120, bg=C['card'], highlightthickness=0)       # 선택한 케이스의 실제 색
    cmp_cv.pack(fill='x')
    # 오른쪽: 지표
    rb = ttk.Frame(cr)
    rb.pack(fill='x', pady=(0, 4))
    rb2 = ttk.Frame(cr)
    rb2.pack(fill='x', pady=(0, 6))
    ttk.Label(rb, text='넣을 지표', style='Head.TLabel').pack(side='left', padx=(0, 10))
    cmp_add_cb = ttk.Combobox(rb, textvariable=cmp_add, values=[CMP.SUMMARY] + list(CMP.JOB_ITEMS), width=30)
    cmp_add_cb.pack(side='left')
    mtree = ttk.Treeview(cr, columns=('on', 'ord', 'metric', 'value', 'chart', 'vms'), show='headings', selectmode='browse', height=8)
    for key, text, w_, anchor in (('on', '넣기', 46, 'center'), ('ord', '순서', 46, 'center'), ('metric', '지표', 220, 'w'),
                                  ('value', '값', 100, 'w'), ('chart', '그래프 형식', 100, 'w'), ('vms', '포함할 VM', 130, 'w')):
        mtree.heading(key, text=text, anchor='w' if anchor == 'w' else 'center')
        mtree.column(key, width=w_, anchor=anchor, stretch=key == 'metric')
    mtree.tag_configure('off', foreground=C['off'])
    mtree.pack(fill='both', expand=True)
    ttk.Label(cr, text='넣기 칸을 누르면 체크가 바뀐다. 순서 = 리포트에 싣는 순서. 값 · 그래프 형식 · 포함할 VM 은 두 번 눌러 고친다.\n'
                       '막대: 케이스(와 VM)마다 막대. 누적 막대: phase 소요 시간. 꺾은선: 시계열(가로축 elapsed time, 케이스 · VM 마다 선 1개).\n'
                       '포함할 VM: 이름에 그 글자가 든 VM 만 그 그래프에 넣는다(쉼표로 여러 개). 비우면 VM 전부.',
              style='Hint.TLabel', justify='left').pack(anchor='w', pady=(4, 0))

    def cmp_key():
        return case_key()

    def cmp_save_state():
        if hold['cmp'] or not cmp_key():
            return
        allst = dict(settings_mod.load().get('compare_state', {}))
        allst[cmp_key()] = {'title': cmp_title.get(), 'baseline': name_of_choice(cmp_base.get()), 'phase_bg': cmp_phase.get(),
                            'phase_case': cmp_pcase.get(), 'rows': state['cmp_rows']}
        settings_mod.save({'compare_state': allst})

    def cmp_selected():
        return [r for r in state['cmp_rows'] if r['sel']]

    def cmp_draw_colors():
        cmp_cv.delete('all')
        for k, r in enumerate(cmp_selected()[:5]):
            y = 4 + k * 23
            cmp_cv.create_text(4, y + 9, text=(r['name'] or r['id'])[:26], anchor='w', font=(FONT, 9), fill=C['ink'])
            for i, col in enumerate(P.palette_colors(P.palette_index(r['palette']))[:8]):
                cmp_cv.create_rectangle(200 + i * 28, y, 200 + i * 28 + 23, y + 17, fill=col, outline=C['mut'])
        if len(cmp_selected()) > 5:
            cmp_cv.create_text(440, 100, text=f'외 {len(cmp_selected()) - 5}건', anchor='w', font=(FONT, 9), fill=C['mut'])

    def cmp_refresh_cases(select=None):
        ctree.delete(*ctree.get_children())
        n = 0
        for r in state['cmp_rows']:
            n += bool(r['sel'])
            ctree.insert('', 'end', iid=r['id'], tags=('on' if r['sel'] else 'off',),
                         values=('☑' if r['sel'] else '☐', n if r['sel'] else '', ref_text(r['id']), r['name'], r['palette']))
        if select and ctree.exists(select):
            ctree.selection_set(select)
        ids = [r['id'] for r in state['cmp_rows'] if not SM.is_prior(r['id'])]
        cmp_pcase_cb.config(values=ids)
        if cmp_pcase.get() not in ids:
            cmp_pcase.set(next((r['id'] for r in cmp_selected()), ids[0] if ids else ''))
        cmp_draw_colors()

    def cmp_sync():
        """케이스 정보 파일을 읽은 뒤: 이 파일에 저장된 비교 설정을 불러오고 케이스 목록에 맞춘다."""
        hold['cmp'] = True
        try:
            old = settings_mod.load().get('compare_state', {}).get(cmp_key(), {})
            have = [c_['id'] for c_ in state['cases']] + [p_['key'] for p_ in state['priors']]
            prev_rows = state['cmp_rows'] if state['cmp_rows'] and state.get('cmp_for') == cmp_key() else old.get('rows', [])
            rows = [dict(r) for r in prev_rows if isinstance(r, dict) and r.get('id') in set(have)]
            known = {r['id'] for r in rows}
            for k_ in have:
                if k_ not in known:
                    rows.append({'id': k_, 'sel': False, 'name': '', 'palette': ''})
            state['cmp_for'] = cmp_key()
            auto = [n for i, n in enumerate(PAL_NAMES) if i not in P.NO_AUTO]
            for i, r in enumerate(rows):
                r['sel'] = bool(r.get('sel'))
                r['name'] = str(r.get('name') or '')
                if r.get('palette') not in PAL_NAMES:
                    r['palette'] = auto[i % len(auto)]
            state['cmp_rows'] = rows
            cmp_title.set(str(old.get('title', '')))
            b = base_by_name(str(old.get('baseline', '')))
            cmp_base_cb.config(values=base_choices())
            cmp_base.set(base_label(b) if b else NO_BASE)
            cmp_phase.set(bool(old.get('phase_bg', False)))
            cmp_pcase.set(str(old.get('phase_case', '')))
            cmp_refresh_cases()
        finally:
            hold['cmp'] = False

    def cmp_toggle(cid):
        r = next(x for x in state['cmp_rows'] if x['id'] == cid)
        r['sel'] = not r['sel']
        cmp_save_state()
        cmp_refresh_cases(cid)

    def cmp_case_done(iid, col, val):
        key = ctree['columns'][int(col[1:]) - 1]
        r = next(x for x in state['cmp_rows'] if x['id'] == iid)
        if key == 'name':
            r['name'] = val.strip()
        elif key == 'pal' and val in PAL_NAMES:
            r['palette'] = val
        cmp_save_state()
        cmp_refresh_cases(iid)

    def cmp_case_move(d):
        sel = ctree.selection()
        if not sel:
            return
        i = next(k for k, x in enumerate(state['cmp_rows']) if x['id'] == sel[0])
        j = i + d
        if 0 <= j < len(state['cmp_rows']):
            state['cmp_rows'][i], state['cmp_rows'][j] = state['cmp_rows'][j], state['cmp_rows'][i]
            cmp_save_state()
            cmp_refresh_cases(sel[0])

    def cmp_auto_colors():
        auto = [n for i, n in enumerate(PAL_NAMES) if i not in P.NO_AUTO]
        for i, r in enumerate(cmp_selected()):
            r['palette'] = auto[i % len(auto)]
        cmp_save_state()
        cmp_refresh_cases()

    def ctree_click(e):
        if ctree.identify_region(e.x, e.y) == 'cell' and ctree.identify_column(e.x) == '#1':
            iid = ctree.identify_row(e.y)
            if iid:
                cmp_toggle(iid)
                return 'break'
        return None

    def ctree_double(e):
        iid, col = ctree.identify_row(e.y), ctree.identify_column(e.x)
        if iid and col:
            key = ctree['columns'][int(col[1:]) - 1]
            if key == 'name':
                state['edit'] = edit_cell(ctree, iid, col, None, cmp_case_done)
            elif key == 'pal':
                state['edit'] = edit_cell(ctree, iid, col, PAL_NAMES, cmp_case_done)

    def cmp_refresh_metrics(select=None):
        mtree.delete(*mtree.get_children())
        n = 0
        for i, it in enumerate(state['cmp_metrics']):
            n += bool(it.get('on', True))
            mtree.insert('', 'end', iid=str(i), tags=('' if it.get('on', True) else 'off',),
                         values=('☑' if it.get('on', True) else '☐', n if it.get('on', True) else '', it['metric'],
                                 (f'항목 {len(it.get("items") or [])}개 · ' + '·'.join(CMP.STATS[x] for x in it.get('stats') or [])) if it['metric'] == CMP.SUMMARY else
                                 CMP.VALUES[it['value']] if it['metric'] not in CMP.JOB_ITEMS else ('잡 평균' if it['metric'] == CMP.TAT else '단계 self 시간' if it['metric'] == CMP.PHASE else '세트 평균'),
                                 CMP.CHARTS[it['chart']], it.get('vms', '')))
        if select is not None and mtree.exists(str(select)):
            mtree.selection_set(str(select))

    def cmp_metric_add(name=None):
        name = (name if name is not None else cmp_add.get()).strip()
        if not name:
            return
        state['cmp_metrics'].append(dict(CMP.default_item(name), on=True))
        cmp_save_preset()
        cmp_refresh_metrics(len(state['cmp_metrics']) - 1)
        if name == CMP.SUMMARY and not state.get('no_dialog'):
            summary_dialog(len(state['cmp_metrics']) - 1)

    def cmp_metric_del():
        sel = mtree.selection()
        if sel:
            state['cmp_metrics'].pop(int(sel[0]))
            cmp_save_preset()
            cmp_refresh_metrics()

    def cmp_metric_move(d):
        sel = mtree.selection()
        if not sel:
            return
        i = int(sel[0])
        j = i + d
        if 0 <= j < len(state['cmp_metrics']):
            state['cmp_metrics'][i], state['cmp_metrics'][j] = state['cmp_metrics'][j], state['cmp_metrics'][i]
            cmp_save_preset()
            cmp_refresh_metrics(j)

    def cmp_metric_done(iid, col, val):
        key = mtree['columns'][int(col[1:]) - 1]
        it = state['cmp_metrics'][int(iid)]
        if key == 'value' and val in VAL_NAMES:
            it['value'] = VAL_NAMES[val]
            it['chart'] = 'line' if it['value'] == 'series' else ('bar' if it['chart'] == 'line' else it['chart'])
        elif key == 'chart' and val in CHART_NAMES:
            it['chart'] = CHART_NAMES[val]
            it['value'] = 'series' if it['chart'] == 'line' else ('mean' if it['value'] == 'series' else it['value'])
        elif key == 'vms':
            it['vms'] = val.strip()
        state['cmp_metrics'][int(iid)] = dict(CMP.normalize(it), on=it.get('on', True))
        cmp_save_preset()
        cmp_refresh_metrics(iid)

    def mtree_click(e):
        if mtree.identify_region(e.x, e.y) == 'cell' and mtree.identify_column(e.x) == '#1':
            iid = mtree.identify_row(e.y)
            if iid:
                it = state['cmp_metrics'][int(iid)]
                it['on'] = not it.get('on', True)
                cmp_save_preset()
                cmp_refresh_metrics(iid)
                return 'break'
        return None

    def mtree_double(e):
        iid, col = mtree.identify_row(e.y), mtree.identify_column(e.x)
        if not iid or not col:
            return
        key = mtree['columns'][int(col[1:]) - 1]
        it = state['cmp_metrics'][int(iid)]
        if it['metric'] == CMP.SUMMARY:
            summary_dialog(int(iid))
            return
        job_item = it['metric'] in CMP.JOB_ITEMS
        if key == 'value' and not job_item:
            state['edit'] = edit_cell(mtree, iid, col, list(CMP.VALUES.values()), cmp_metric_done)
        elif key == 'chart' and not job_item:
            state['edit'] = edit_cell(mtree, iid, col, [CMP.CHARTS['bar'], CMP.CHARTS['line']], cmp_metric_done)
        elif key == 'vms' and it['metric'] != CMP.PHASE:
            state['edit'] = edit_cell(mtree, iid, col, None, cmp_metric_done)

    def summary_dialog(idx, auto_ok=False):
        """요약 표 설정 창: 넣을 항목(TAT, phase, 지표), 통계, 행 · 열 방향, Load · Compute, 기준 대비 차이, 포함할 VM."""
        it = CMP.normalize(state['cmp_metrics'][idx])
        win = tk.Toplevel(root)
        win.title('요약 표 설정')
        win.geometry('760x620')
        win.transient(root)
        win.configure(bg=C['card'])
        f = ttk.Frame(win, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='넣을 항목', style='Head.TLabel').pack(anchor='w')
        ttk.Label(f, text='TAT 는 TAT 로그, phase 는 phase 로그, 그 밖은 지표(잡 실행 구간 통계). 지표 목록은 [지표 목록 읽기] 를 누르면 나온다',
                  style='Hint.TLabel').pack(anchor='w')
        names = [CMP.TAT, CMP.PHASE] + [m for m in dict.fromkeys(state['metric_names'] + [x['metric'] for x in state['cmp_metrics']])
                                        if m not in (CMP.SUMMARY, CMP.TAT, CMP.PHASE, CMP.CONC)]
        names += [m for m in it['items'] if m not in names]
        lf = ttk.Frame(f)
        lf.pack(fill='both', expand=True, pady=(4, 8))
        lst = tk.Listbox(lf, selectmode='multiple', exportselection=False, height=12, font=(FONT, 10))
        sb = ttk.Scrollbar(lf, orient='vertical', command=lst.yview)
        lst.configure(yscrollcommand=sb.set)
        lst.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        for k, n_ in enumerate(names):
            lst.insert('end', n_)
            if n_ in it['items']:
                lst.selection_set(k)
        o = ttk.Frame(f)
        o.pack(fill='x')
        sv = {k: tk.BooleanVar(value=k in it['stats']) for k in CMP.STATS}
        ttk.Label(o, text='통계').grid(row=0, column=0, sticky='w', pady=3)
        so = ttk.Frame(o)
        so.grid(row=0, column=1, sticky='w')
        for k, t_ in CMP.STATS.items():
            ttk.Checkbutton(so, text=t_, variable=sv[k]).pack(side='left', padx=(0, 10))
        orient = tk.StringVar(value=it['orient'])
        ttk.Label(o, text='행 · 열').grid(row=1, column=0, sticky='w', pady=3)
        oo = ttk.Frame(o)
        oo.grid(row=1, column=1, sticky='w')
        ttk.Radiobutton(oo, text='행 = 케이스 · VM, 열 = 항목 · 통계', variable=orient, value='case_rows').pack(side='left', padx=(0, 14))
        ttk.Radiobutton(oo, text='행 = 항목 · 통계, 열 = 케이스 · VM (항목이 많을 때)', variable=orient, value='item_rows').pack(side='left')
        split = tk.BooleanVar(value=bool(it.get('phase_split')))
        dif = tk.BooleanVar(value=bool(it.get('diff')))
        vmv = tk.StringVar(value=it.get('vms', ''))
        ttk.Checkbutton(o, text='phase 를 Load 와 Compute 로 나눠서도 넣기', variable=split).grid(row=2, column=1, sticky='w', pady=3)
        ttk.Checkbutton(o, text='첫 케이스 대비 평균 차이 (%) 넣기', variable=dif).grid(row=3, column=1, sticky='w', pady=3)
        ttk.Label(o, text='포함할 VM').grid(row=4, column=0, sticky='w', pady=3)
        ttk.Entry(o, textvariable=vmv, width=30).grid(row=4, column=1, sticky='w')

        def ok():
            items = [names[k] for k in lst.curselection()]
            stats = [k for k in CMP.STATS if sv[k].get()]
            if not items or not stats:
                messagebox.showwarning('요약 표 설정', '항목과 통계를 하나 이상 고르십시오.', parent=win)
                return
            state['cmp_metrics'][idx] = dict(CMP.normalize(dict(it, items=items, stats=stats, orient=orient.get(), phase_split=split.get(),
                                                                diff=dif.get(), vms=vmv.get().strip())), on=state['cmp_metrics'][idx].get('on', True))
            cmp_save_preset()
            cmp_refresh_metrics(idx)
            win.destroy()
        b = ttk.Frame(f)
        b.pack(fill='x', pady=(10, 0))
        ttk.Button(b, text='확인', style='Blue.TButton', command=ok).pack(side='right')
        ttk.Button(b, text='취소', command=win.destroy).pack(side='right', padx=6)
        return {'win': win, 'list': lst, 'stats': sv, 'orient': orient, 'split': split, 'diff': dif, 'vms': vmv, 'ok': ok, 'names': names}

    def cmp_load_names():
        if not os.path.isfile(v['cases'].get()) or not os.path.isdir(v['data'].get()):
            messagebox.showwarning('입력', '케이스 정보 파일과 데이터 폴더를 먼저 고르십시오.')
            return
        names_btn.config(state='disabled')
        nb.select(tab4)
        log.insert('end', '── 지표 목록 읽기 시작 (비교 리포트)\n')
        threading.Thread(target=list_worker, args=(v['cases'].get(), v['data'].get(), None, to_gb.get(), to_gbps.get(), 'cmp_metrics'),
                         daemon=True).start()

    def cmp_spec():
        b = base_by_name(name_of_choice(cmp_base.get()))
        return {'title': cmp_title.get().strip(),
                'cases': [{'id': r['id'], 'name': r['name'], 'palette': r['palette'], 'display': ref_text(r['id'])} for r in cmp_selected()],
                'metrics': [{k: v_ for k, v_ in it.items() if k != 'on'} for it in state['cmp_metrics'] if it.get('on', True)],
                'baseline': ({'name': b['name'], 'kind': b.get('kind', ''), 'cond': b.get('cond', ''), 'note': b.get('note', ''),
                              'value': str(b.get('tat', '')).replace(',', '') or 0} if b else None),
                'phase_bg': cmp_phase.get(), 'phase_case': cmp_pcase.get(), 'word': cmp_word.get(), 'html': cmp_html.get()}

    ctree.bind('<Button-1>', ctree_click)
    ctree.bind('<Double-1>', ctree_double)
    mtree.bind('<Button-1>', mtree_click)
    mtree.bind('<Double-1>', mtree_double)
    ttk.Button(lb2, text='위로', width=7, command=lambda: cmp_case_move(-1)).pack(side='left')
    ttk.Button(lb2, text='아래로', width=7, command=lambda: cmp_case_move(1)).pack(side='left', padx=(4, 0))
    ttk.Button(lb2, text='색 계열 자동 지정', command=cmp_auto_colors).pack(side='left', padx=(4, 0))
    # 색 고르기: 탭 3 의 그래프 색 계열과 같은 방식(목록 + 실제 색). 표에서 고른 케이스에 적용한다
    ttk.Label(lb3, text='고른 케이스의 색 계열').pack(side='left', padx=(0, 6))
    cmp_pal_cb = ttk.Combobox(lb3, textvariable=cmp_pal, values=PAL_NAMES, state='readonly', width=16)
    cmp_pal_cb.pack(side='left')
    cmp_pal_cv = tk.Canvas(lb3, width=8 * 30 + 4, height=22, bg=C['card'], highlightthickness=0)
    cmp_pal_cv.pack(side='left', padx=8)

    def cmp_pal_draw(*_):
        cmp_pal_cv.delete('all')
        if cmp_pal.get() in PAL_NAMES:
            for i, col in enumerate(P.palette_colors(P.palette_index(cmp_pal.get()))[:8]):
                cmp_pal_cv.create_rectangle(2 + i * 30, 2, 2 + i * 30 + 25, 20, fill=col, outline=C['mut'])

    def cmp_pal_pick(_e=None):
        sel = ctree.selection()
        cmp_pal_draw()
        if sel and cmp_pal.get() in PAL_NAMES:
            cmp_case_done(sel[0], '#5', cmp_pal.get())

    def cmp_row_selected(_e=None):
        sel = ctree.selection()
        if sel:
            r = next((x for x in state['cmp_rows'] if x['id'] == sel[0]), None)
            if r and cmp_pal.get() != r['palette']:
                cmp_pal.set(r['palette'])
            cmp_pal_draw()

    cmp_pal_cb.bind('<<ComboboxSelected>>', cmp_pal_pick)
    ctree.bind('<<TreeviewSelect>>', cmp_row_selected)
    cmp_pal_draw()
    ttk.Button(rb, text='+ 지표 추가', style='Green.TButton', command=cmp_metric_add).pack(side='left', padx=(6, 0))
    names_btn = ttk.Button(rb, text='지표 목록 읽기', command=cmp_load_names)
    names_btn.pack(side='left', padx=(6, 0))
    ttk.Button(rb2, text='삭제', width=7, command=cmp_metric_del).pack(side='left')
    ttk.Button(rb2, text='위로', width=7, command=lambda: cmp_metric_move(-1)).pack(side='left', padx=(4, 0))
    ttk.Button(rb2, text='아래로', width=7, command=lambda: cmp_metric_move(1)).pack(side='left', padx=(4, 0))
    ttk.Label(rb2, text='[지표 목록 읽기] 를 누르면 데이터의 지표 이름이 위 목록에 나온다',
              style='Hint.TLabel').pack(side='left', padx=10)
    for var_ in (cmp_title, cmp_base, cmp_phase, cmp_pcase):
        var_.trace_add('write', lambda *_: cmp_save_state())
    for var_ in (cmp_word, cmp_html):
        var_.trace_add('write', lambda *_: cmp_save_preset())

    # ── 탭 5: 생성
    gbar = ttk.Frame(tab4)
    gbar.pack(fill='x', pady=(0, 4))
    ttk.Label(gbar, text='만들 것', style='Head.TLabel').pack(side='left', padx=(0, 10))
    ttk.Checkbutton(gbar, text='케이스 리포트 (탭 1 에서 생성을 체크한 케이스)', variable=gen_case).pack(side='left')
    ttk.Checkbutton(gbar, text='비교 리포트 (탭 4 에서 고른 케이스와 지표)', variable=gen_cmp).pack(side='left', padx=(18, 0))
    run_btn = ttk.Button(gbar, text='리포트 생성', style='Red.TButton')
    run_btn.pack(side='left', padx=(24, 0))
    open_btn = ttk.Button(gbar, text='결과 폴더 열기', state='disabled')
    open_btn.pack(side='left', padx=8)
    ttk.Label(tab4, text='체크한 것만 만든다. 비교 리포트를 체크하지 않으면 탭 4 는 설정하지 않아도 된다. '
                         '[리포트 생성] 을 누르면 체크한 것에 필요한 설정을 검사하고, 빠진 것이 있으면 탭별로 알려 준 뒤 만들지 않는다.',
              style='Hint.TLabel').pack(anchor='w', pady=(0, 6))
    log = scrolledtext.ScrolledText(tab4, height=18, font=(FONT, 9), relief='solid', borderwidth=1)
    log.pack(fill='both', expand=True)
    for var_, key_ in ((gen_case, 'gen_case'), (gen_cmp, 'gen_cmp')):
        var_.trace_add('write', lambda *_a, v_=var_, k_=key_: settings_mod.save({k_: v_.get()}))

    def set_roles(r):
        state['roles'] = r
        role_sum.set(R.summary(r))
        un_chk.config(state='normal' if r else 'disabled')
        pb_opt['save']()

    def list_worker(cases, data, analysis, gb=False, gbps=False, kind='metrics'):
        from .build import list_metrics
        try:
            q.put((kind, list_metrics(cases, data, log=lambda m: q.put(('log', m)), analysis=analysis, to_gb=gb, to_gbps=gbps)))
        except SystemExit as e:
            q.put(('merr', str(e)))
        except Exception as e:  # 화면에 원인을 그대로 보인다
            q.put(('merr', f'{type(e).__name__}: {e}'))

    def open_roles():
        if not os.path.isfile(v['cases'].get()) or not os.path.isdir(v['data'].get()):
            messagebox.showwarning('입력', '케이스 정보 파일과 데이터 폴더를 먼저 고르십시오.')
            return
        role_btn.config(state='disabled')
        nb.select(tab4)
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

    def check():
        """[리포트 생성] 을 눌렀을 때의 검사. 체크한 것(케이스 리포트 · 비교 리포트)에 필요한 설정만 본다.
        반환: (run_csv 에 넘길 값 또는 None, 빠진 설정 목록 ['[탭 이름] 내용'], 알림 목록(막지 않음))."""
        miss, warn = [], []
        do_case, do_cmp = gen_case.get(), gen_cmp.get()
        if not do_case and not do_cmp:
            miss.append('[탭 5 생성] 케이스 리포트와 비교 리포트 가운데 하나 이상을 체크하십시오.')
        if not os.path.isfile(v['cases'].get()):
            miss.append('[입력] 케이스 정보 파일(CSV 또는 엑셀)을 고르십시오.')
        if not os.path.isdir(v['data'].get()):
            miss.append('[입력] 데이터 폴더를 고르십시오.')
        if os.path.isfile(v['cases'].get()) and not state['cases'] and not load_cases(show_error=False):
            miss.append('[탭 1 테스트 케이스] 케이스 정보 파일을 읽지 못했습니다. [다시 불러오기] 를 눌러 오류를 확인하십시오.')
        width = None
        try:
            width = float(width_var.get())
        except ValueError:
            miss.append('[탭 3 케이스 리포트 옵션] 그래프 폭은 숫자(cm)로 적으십시오. 예: 16.5')
        only, bases = [], {}
        if do_case and state['cases']:
            only = [c_['id'] for c_ in state['cases'] if state['rows'][c_['id']]['gen']]
            if not only:
                miss.append('[탭 1 테스트 케이스] 생성할 케이스를 하나 이상 체크하십시오.')
            for cid in only:
                name = state['rows'][cid].get('base', '')
                b = base_by_name(name)
                if name and not b:
                    miss.append(f'[탭 1 테스트 케이스] {cid}: 기준선 "{name}" 이 지금 기준선 프로파일({pb_base["var"].get()})에 없습니다. 기준선을 다시 고르십시오.')
                elif b and not str(b.get('tat', '')).strip():
                    miss.append(f'[탭 2 기준선] 기준선 {b["name"]} 의 TAT 가 비어 있습니다. ({cid} 에서 사용)')
                elif not b:
                    warn.append(f'{cid}: 기준선 없음 (Degradation 을 적지 않음)')
        refs = {}
        for cid in only:
            k = state['rows'][cid].get('ref', '')
            if not k:
                continue
            if (SM.is_prior(k) and not any(p_['key'] == k for p_ in state['priors'])) or (not SM.is_prior(k) and k not in state['rows']):
                miss.append(f'[탭 1 테스트 케이스] {cid}: 비교 기준 케이스 {ref_text(k)} 를 찾을 수 없습니다. 이전 결과 폴더를 확인하거나 다시 고르십시오.')
            else:
                refs[cid] = k
        for c_ in state['cases']:
            b = base_by_name(state['rows'][c_['id']].get('base', ''))
            if b and str(b.get('tat', '')).strip():
                bases[c_['id']] = {'name': b['name'], 'kind': b.get('kind', ''), 'cond': b.get('cond', ''), 'note': b.get('note', ''),
                                   'value': float(str(b['tat']).replace(',', ''))}
        spec = None
        if do_cmp:
            spec = cmp_spec()
            if not state['cases']:
                miss.append('[탭 4 비교 리포트] 케이스 정보 파일을 먼저 불러오십시오.')
            miss += ['[탭 4 비교 리포트] ' + m for m in CMP.validate(spec, ([c_['id'] for c_ in state['cases']] + [p_['key'] for p_ in state['priors']])
                                                                    if state['cases'] else None)]
            if not spec['title']:
                spec['title'] = os.path.splitext(os.path.basename(v['cases'].get()))[0] + ' 비교'
            if spec['phase_bg'] and spec['phase_case'] not in [c['id'] for c in spec['cases']]:
                warn.append(f'비교 리포트: phase 배경의 기준 케이스 {spec["phase_case"] or "(없음)"} 가 비교할 케이스에 없어 배경을 그리지 않음')
                spec['phase_bg'] = False
        if miss:
            return None, miss, warn
        kw = dict(cases_csv=v['cases'].get(), data_dir=v['data'].get(), out_dir=v['out'].get() or None,
                  title=v['title'].get() or None, analysis=CORE_PROFILE if core.get() else None, split_dir=v['split'].get() or None,
                  roles=dict(state['roles']) or None, include_unassigned=include_un.get(),
                  palette=pal_var.get(), chart_width_cm=width, selection=v['selection'].get() or None,
                  to_gb=to_gb.get(), to_gbps=to_gbps.get(), end_ref_vms=end_refs.get().strip(),
                  only=only, case_desc={cid: r['label'] for cid, r in state['rows'].items()}, case_baselines=bases,
                  case_vms={c_['id']: state['rows'][c_['id']]['vms'] for c_ in state['cases']      # 화면에서 고친 케이스만 (그대로면 파일의 VM 열 규칙)
                            if state['rows'][c_['id']]['vms'] and state['rows'][c_['id']]['vms'] != (c_.get('vms') or '')},
                  vm_filter=vm_inc.get().strip() or None, make_docs=do_case, make_html=html_var.get(), compare=spec,
                  case_refs=refs, prior_dirs=[x.strip() for x in v['prev'].get().split(';') if x.strip()])
        return kw, [], warn

    def run_args():
        """(run_csv 에 넘길 값, 빠진 설정 글). 창 없이 시험할 때 쓴다."""
        kw, miss, _ = check()
        return kw, '\n'.join(miss)

    def start():
        kw, miss, warn = check()
        nb.select(tab4)
        if kw is None:
            text = '빠진 설정이 있어 만들지 않았습니다.\n\n' + '\n'.join(miss)
            log.insert('end', '── ' + text.replace('\n\n', '\n') + '\n── 설정을 고친 뒤 [리포트 생성] 을 다시 누르십시오\n')
            log.see('end')
            messagebox.showwarning('빠진 설정', text + '\n\n설정을 고친 뒤 [리포트 생성] 을 다시 누르십시오.')
            return
        run_btn.config(state='disabled')
        what = ([f'케이스 리포트 {len(kw["only"])}건' + (' + HTML' if kw['make_html'] else '')] if kw['make_docs'] else []) \
            + ([f'비교 리포트 (케이스 {len(kw["compare"]["cases"])}건 · 지표 {len(kw["compare"]["metrics"])}개)'] if kw['compare'] else [])
        log.insert('end', f'── 시작: {kw["cases_csv"]}\n── 만들 것: {" · ".join(what)}\n'
                          f'── 기준선 프로파일 {pb_base["var"].get()} · 옵션 프로파일 {pb_opt["var"].get()}'
                          + (f' · 비교 리포트 양식 {pb_cmp["var"].get()}' if kw['compare'] else '') + '\n'
                          + ''.join(f'── 알림: {w}\n' for w in warn))
        threading.Thread(target=worker, args=(kw,), daemon=True).start()

    def poll():
        while not q.empty():
            kind, val = q.get()
            if kind == 'log':
                log.insert('end', val + '\n')
            elif kind == 'done':
                last['dir'] = val.get('outdir') or (os.path.dirname(val['outputs'][0]) if val.get('outputs') else '')
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
            elif kind == 'cmp_metrics':
                names_btn.config(state='normal')
                state['metric_names'] = list(val or [])
                cmp_add_cb.config(values=[CMP.SUMMARY] + list(CMP.JOB_ITEMS) + state['metric_names'])
                log.insert('end', f'── 지표 {len(state["metric_names"])}개를 읽었습니다. 탭 4 의 넣을 지표 목록에서 고를 수 있습니다\n')
                nb.select(tabc)
            elif kind == 'merr':
                role_btn.config(state='normal')
                names_btn.config(state='normal')
                log.insert('end', f'── 지표 목록 읽기 실패: {val}\n')
                messagebox.showerror('지표 목록', val)
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

    hooks.update(refresh_cases=refresh_cases, cmp_sync=cmp_sync)
    run_btn.config(command=start)
    open_btn.config(command=open_dir)
    role_btn.config(command=open_roles)
    pb_base['load_current']()                 # 저장된 묶음 값을 화면에 넣는다
    pb_opt['load_current']()
    pb_cmp['load_current']()
    if os.path.isfile(v['cases'].get()):
        load_cases(show_error=False)
    root.after(200, poll)
    return {'v': v, 'log': log, 'core': core, 'include_un': include_un, 'role_sum': role_sum, 'state': state, 'start': start,
            'open_roles': open_roles, 'set_roles': set_roles, 'run_btn': run_btn, 'role_btn': role_btn, 'un_chk': un_chk,
            'pal_var': pal_var, 'width_var': width_var, 'to_gb': to_gb, 'to_gbps': to_gbps, 'end_refs': end_refs, 'vm_inc': vm_inc,
            'html_var': html_var, 'gen_case': gen_case, 'gen_cmp': gen_cmp,
            'nb': nb, 'tabs': (tab1, tab2, tab3, tabc, tab4), 'tree': tree, 'btree': btree, 'ctree': ctree, 'mtree': mtree, 'bulk_var': bulk_var,
            'load_cases': load_cases, 'set_gen': set_gen, 'apply_base': apply_base, 'apply_vms': apply_vms, 'vmf_var': vmf_var, 'cell_done': cell_done,
            'base_add': base_add, 'base_done': base_done, 'base_del': base_del, 'run_args': run_args, 'check': check,
            'refresh_cases': refresh_cases, 'count_var': count_var, 'apply_ref': apply_ref, 'ref_bulk': ref_bulk, 'ref_choices': ref_choices, 'pb_base': pb_base, 'pb_opt': pb_opt, 'pb_cmp': pb_cmp,
            'cmp': {'pal': cmp_pal, 'pal_pick': cmp_pal_pick, 'title': cmp_title, 'base': cmp_base, 'phase': cmp_phase, 'pcase': cmp_pcase, 'word': cmp_word, 'html': cmp_html,
                    'toggle': cmp_toggle, 'case_done': cmp_case_done, 'case_move': cmp_case_move, 'auto_colors': cmp_auto_colors,
                    'metric_add': cmp_metric_add, 'metric_del': cmp_metric_del, 'metric_move': cmp_metric_move,
                    'metric_done': cmp_metric_done, 'spec': cmp_spec, 'sync': cmp_sync, 'summary_dialog': summary_dialog}}


def main():
    root = tk.Tk()
    build(root)
    root.mainloop()
