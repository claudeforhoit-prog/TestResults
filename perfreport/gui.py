"""창 화면: 케이스 정보 파일과 데이터 폴더를 고르고, 탭 1 에서 만들 케이스 · 설명 · 기준선을 정한 뒤 [리포트 생성] 을 누르면 docx · html 을 만든다."""
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


COLORS = dict(bg='#f3f5f8', card='#ffffff', line='#d5dbe3', head='#e8edf4', ink='#1f2937', mut='#6b7280',
              blue='#2a6fd6', blue_d='#1f5bb5', green='#1f9d6b', green_d='#17845a', purple='#7a4fbf', purple_d='#6540a3',
              red='#c0392b', red_d='#a93226', case='#edf3fc', sel='#cfe0fb', off='#9aa3af', dark='#2f3a48',
              tab='#e3e8ef', tab_h='#d5dce6', field='#aeb7c2')
NO_BASE = '(기준선 없음)'
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


def build(root):
    """기본 창을 구성한다. 반환: 입력 값과 동작 함수(창 없이 시험할 때 쓴다).
    탭 1 테스트 케이스(생성 선택 · 설명 수정 · 기준선), 탭 2 기준선(사전 정의), 탭 3 케이스 리포트 옵션, 탭 4 생성."""
    from .build import cases_from_csv
    C = COLORS
    root.title('성능 리포트 생성기')
    root.geometry('1280x860')
    root.minsize(1100, 720)
    apply_style(root)
    saved = settings_mod.load()
    v = {k: tk.StringVar(value=str(saved.get('path_' + k, ''))) for k in ('cases', 'data', 'out', 'split')}
    v.update({k: tk.StringVar() for k in ('title', 'selection')})
    core = tk.BooleanVar(value=False)
    include_un = tk.BooleanVar(value=True)
    role_sum = tk.StringVar(value=R.summary({}))
    pal_var = tk.StringVar(value=P.PALETTES[P.DEFAULT_PALETTE][0])
    width_var = tk.StringVar(value='16.5')
    to_gb = tk.BooleanVar(value=False)      # 단위 변환: 기본은 원래 단위 그대로
    to_gbps = tk.BooleanVar(value=False)
    end_refs = tk.StringVar(value=str(saved.get('end_ref_vms', '')))      # 종료 기준 VM: 바꿀 때까지 저장
    bulk_var = tk.StringVar(value=NO_BASE)
    vmf_var = tk.StringVar()                # VM 필터 일괄 적용 입력 칸
    count_var = tk.StringVar(value='케이스 정보 파일을 고르면 케이스가 표로 나온다')
    q = queue.Queue()
    last = {}
    state = {'roles': {}, 'cases': [], 'rows': {}, 'edit': None,
             'baselines': [dict(b) for b in saved.get('baselines', []) if isinstance(b, dict) and b.get('name')]}
    # 옛 판의 'TAT 비교 기준' 값이 있으면 기준선 한 줄로 옮긴다 (한 번만)
    if not state['baselines'] and str(saved.get('tat_baseline', '')).strip():
        state['baselines'].append({'name': str(saved.get('tat_baseline_note') or '기준선').strip(), 'kind': 'BM', 'cond': '',
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

    def save_paths(*_):
        settings_mod.save({'path_' + k: v[k].get() for k in ('cases', 'data', 'out', 'split')})
    for k in ('cases', 'data', 'out', 'split'):
        v[k].trace_add('write', save_paths)

    nb = ttk.Notebook(root)
    nb.pack(fill='both', expand=True, padx=8, pady=(4, 8))
    tab1, tab2, tab3, tab4 = (ttk.Frame(nb, padding=10) for _ in range(4))
    for t_, name in ((tab1, '1. 테스트 케이스'), (tab2, '2. 기준선'), (tab3, '3. 케이스 리포트 옵션'), (tab4, '4. 생성')):
        nb.add(t_, text=name)

    # ── 기준선 (탭 2 에서 정의, 탭 1 에서 케이스마다 선택)
    def base_by_name(name):
        return next((b for b in state['baselines'] if b['name'] == name), None)

    def base_choices():
        return [NO_BASE] + [base_label(b) for b in state['baselines']]

    def name_of_choice(text):
        return '' if text == NO_BASE else text.split(' · ')[0]

    def save_baselines():
        settings_mod.save({'baselines': state['baselines']})
        bulk_cb.config(values=base_choices())
        refresh_cases()

    # ── 탭 1: 테스트 케이스
    bar = ttk.Frame(tab1)
    bar.pack(fill='x', pady=(0, 6))
    bar2 = ttk.Frame(tab1)
    bar2.pack(fill='x', pady=(0, 8))
    COLS = (('gen', '생성', 46, 'center'), ('id', '케이스명', 140, 'w'), ('label', '설명 (수정 가능)', 180, 'w'), ('group', '테스트 그룹', 90, 'w'),
            ('host', '호스트', 80, 'w'), ('start', '잡 시작', 135, 'w'), ('end', '잡 종료', 135, 'w'), ('vms', 'VM 필터 (수정 가능)', 150, 'w'),
            ('end_refs', '종료 기준 VM', 100, 'w'), ('base', '기준선 (TAT)', 170, 'w'))
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
    ttk.Label(tab1, text='생성 칸을 누르면 체크가 바뀐다. 설명 · VM 필터 · 기준선 칸은 두 번 눌러 고친다. 체크한 케이스만 리포트를 만든다. '
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
                r['vms'], c_.get('end_refs') or '', btxt))
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
                                       'base': str(o.get('base', '')), 'vms': str(o.get('vms', c_.get('vms') or ''))}
        refresh_cases()
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

    def apply_vms(all_rows):
        ids = [c_['id'] for c_ in state['cases']] if all_rows else list(tree.selection())
        if not ids:
            messagebox.showinfo('VM 필터 일괄 적용', '표에서 적용할 케이스를 먼저 고르십시오. (Shift · Ctrl 로 여러 줄 선택)')
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
    ttk.Label(bar2, text='VM 필터 일괄 적용').pack(side='left', padx=(0, 6))
    ttk.Entry(bar2, textvariable=vmf_var, width=30).pack(side='left')
    ttk.Button(bar2, text='전체 케이스에 적용', style='Purple.TButton', command=lambda: apply_vms(True)).pack(side='left', padx=(6, 0))
    ttk.Button(bar2, text='선택한 케이스에 적용', style='Purple.TButton', command=lambda: apply_vms(False)).pack(side='left', padx=(4, 0))
    ttk.Label(bar2, text='리포트에 넣을 VM 의 이름 필터. 예: vm01 또는 vm01,vm02 (이름에 그 글자가 든 VM). 비우면 호스트의 VM 전부',
              style='Hint.TLabel').pack(side='left', padx=10)

    # ── 탭 2: 기준선 사전 정의
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
    ttk.Label(bbar, text='기준선 사전 정의', style='Head.TLabel').pack(side='left', padx=(0, 12))
    ttk.Button(bbar, text='+ 기준선 추가', style='Green.TButton', command=base_add).pack(side='left')
    ttk.Button(bbar, text='삭제', command=base_del).pack(side='left', padx=(8, 0))
    ttk.Button(bbar, text='위로', command=lambda: base_move(-1)).pack(side='left', padx=(4, 0))
    ttk.Button(bbar, text='아래로', command=lambda: base_move(1)).pack(side='left', padx=(4, 0))

    # ── 탭 3: 케이스 리포트 옵션
    tab3.columnconfigure(1, weight=1)

    def opt_label(r, text):
        ttk.Label(tab3, text=text, width=20).grid(row=r, column=0, sticky='w', pady=5)

    def hint(r, text):
        ttk.Label(tab3, text=text, style='Hint.TLabel', wraplength=980, justify='left').grid(row=r, column=1, sticky='w')

    opt_label(0, 'HTML 제목 (선택)')
    ttk.Entry(tab3, textvariable=v['title']).grid(row=0, column=1, sticky='we')
    hint(1, '비우면 테스트 그룹마다 그 그룹의 케이스 이름으로 만든다')
    ttk.Checkbutton(tab3, text='VM 핵심 지표만 넣기 (CPU · 메모리 등 8종)', variable=core).grid(row=2, column=1, sticky='w', pady=3)
    opt_label(3, '그래프 색 계열')
    pf = ttk.Frame(tab3)
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
    wf = ttk.Frame(tab3)
    wf.grid(row=4, column=1, sticky='w')
    ttk.Spinbox(wf, from_=10, to=31.5, increment=0.5, textvariable=width_var, width=6).pack(side='left')
    ttk.Label(wf, text='기본 16.5 (A4 세로). 넓히면 A4 가로, 27.7 을 넘으면 A3 가로. 컨플루언스로 가져올 문서는 31.5 (최대)',
              style='Hint.TLabel').pack(side='left', padx=8)
    opt_label(5, '지표 역할 (선택)')
    rf = ttk.Frame(tab3)
    rf.grid(row=5, column=1, sticky='we')
    role_btn = ttk.Button(rf, text='지표 역할 지정…')
    role_btn.pack(side='left')
    ttk.Label(rf, textvariable=role_sum, wraplength=820, justify='left').pack(side='left', padx=8)
    un_chk = ttk.Checkbutton(tab3, text='역할을 지정하지 않은 지표도 넣기 (문서는 Appendix A)', variable=include_un, state='disabled')
    un_chk.grid(row=6, column=1, sticky='w')
    opt_label(7, 'HTML 선택 파일 (선택)')
    sf = ttk.Frame(tab3)
    sf.grid(row=7, column=1, sticky='we')
    sf.columnconfigure(0, weight=1)
    ttk.Entry(sf, textvariable=v['selection']).grid(row=0, column=0, sticky='we')
    ttk.Button(sf, text='찾기', width=6, command=lambda: v['selection'].set(
        filedialog.askopenfilename(title='HTML 선택 파일', filetypes=[('HTML 선택 파일 (JSON)', '*.json'), ('모든 파일', '*.*')]) or v['selection'].get())
    ).grid(row=0, column=1, padx=(6, 0))
    hint(8, "HTML 의 '선택 내보내기' 로 저장한 파일. 지정하면 그 케이스 · VM · 지표만으로 만든다")
    opt_label(9, '단위 변환 (선택)')
    uf = ttk.Frame(tab3)
    uf.grid(row=9, column=1, sticky='w')
    ttk.Checkbutton(uf, text='크기를 GB 로 (KB · MB · Bytes → GB)', variable=to_gb).pack(side='left')
    ttk.Checkbutton(uf, text='네트워크 속도를 Gbps 로 (KBps · Mbps → Gbps)', variable=to_gbps).pack(side='left', padx=12)
    opt_label(10, '종료 기준 VM (선택)')
    ttk.Entry(tab3, textvariable=end_refs).grid(row=10, column=1, sticky='we')
    hint(11, '잡 종료 시각을 비운 케이스에서 이 VM 들의 잡 종료(TAT 로그)와 CPU 사용률로 종료 시각을 정한다. 여러 대면 가장 늦게 끝난 VM 기준. '
             '이름 · 패턴을 ; 로 구분 (예: *app01;*app02). 비우면 케이스의 모든 VM. 케이스 정보에 종료 기준 VM 열이 있으면 그 값이 먼저다')

    # ── 탭 4: 생성
    gbar = ttk.Frame(tab4)
    gbar.pack(fill='x', pady=(0, 8))
    run_btn = ttk.Button(gbar, text='리포트 생성', style='Red.TButton')
    run_btn.pack(side='left')
    open_btn = ttk.Button(gbar, text='결과 폴더 열기', state='disabled')
    open_btn.pack(side='left', padx=8)
    ttk.Label(gbar, text='탭 1 에서 생성을 체크한 케이스의 문서와 HTML 을 만든다', style='Hint.TLabel').pack(side='left', padx=8)
    log = scrolledtext.ScrolledText(tab4, height=18, font=(FONT, 9), relief='solid', borderwidth=1)
    log.pack(fill='both', expand=True)

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

    def run_args():
        """[리포트 생성] 이 run_csv 에 넘기는 값. 입력이 모자라면 (None, 안내 글)."""
        if not os.path.isfile(v['cases'].get()):
            return None, '케이스 정보 파일(CSV 또는 엑셀)을 고르십시오.'
        if not os.path.isdir(v['data'].get()):
            return None, '데이터 폴더를 고르십시오.'
        if not state['cases'] and not load_cases(show_error=False):
            return None, '케이스 정보 파일을 읽지 못했습니다. 탭 1 에서 [다시 불러오기] 를 눌러 오류를 확인하십시오.'
        try:
            width = float(width_var.get())
        except ValueError:
            return None, '그래프 폭은 숫자(cm)로 적으십시오. 예: 16.5'
        only = [c_['id'] for c_ in state['cases'] if state['rows'][c_['id']]['gen']]
        if not only:
            return None, '탭 1 에서 생성할 케이스를 하나 이상 체크하십시오.'
        bases = {}
        for c_ in state['cases']:
            b = base_by_name(state['rows'][c_['id']].get('base', ''))
            if b and str(b.get('tat', '')).strip():
                bases[c_['id']] = {'name': b['name'], 'kind': b.get('kind', ''), 'cond': b.get('cond', ''), 'note': b.get('note', ''),
                                   'value': float(str(b['tat']).replace(',', ''))}
        return dict(cases_csv=v['cases'].get(), data_dir=v['data'].get(), out_dir=v['out'].get() or None,
                    title=v['title'].get() or None, analysis=CORE_PROFILE if core.get() else None, split_dir=v['split'].get() or None,
                    roles=dict(state['roles']) or None, include_unassigned=include_un.get(),
                    palette=pal_var.get(), chart_width_cm=width, selection=v['selection'].get() or None,
                    to_gb=to_gb.get(), to_gbps=to_gbps.get(), end_ref_vms=end_refs.get().strip(),
                    only=only, case_desc={cid: r['label'] for cid, r in state['rows'].items()}, case_baselines=bases,
                    case_vms={c_['id']: state['rows'][c_['id']]['vms'] for c_ in state['cases']      # 화면에서 고친 케이스만 (그대로면 파일의 VM 열 규칙)
                              if state['rows'][c_['id']]['vms'] != (c_.get('vms') or '')}), ''

    def start():
        kw, why = run_args()
        if kw is None:
            messagebox.showwarning('입력', why)
            return
        settings_mod.save({'end_ref_vms': end_refs.get().strip()})
        run_btn.config(state='disabled')
        nb.select(tab4)
        log.insert('end', f'── 시작: {kw["cases_csv"]} · 케이스 {len(kw["only"])}건 · 기준선 지정 {len([c for c in kw["only"] if c in kw["case_baselines"]])}건\n'
                          f'── 지표 역할: {R.summary(state["roles"])}\n')
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
    refresh_bases()
    if os.path.isfile(v['cases'].get()):
        load_cases(show_error=False)
    root.after(200, poll)
    return {'v': v, 'log': log, 'core': core, 'include_un': include_un, 'role_sum': role_sum, 'state': state, 'start': start,
            'open_roles': open_roles, 'set_roles': set_roles, 'run_btn': run_btn, 'role_btn': role_btn, 'un_chk': un_chk,
            'pal_var': pal_var, 'width_var': width_var, 'to_gb': to_gb, 'to_gbps': to_gbps, 'end_refs': end_refs,
            'nb': nb, 'tabs': (tab1, tab2, tab3, tab4), 'tree': tree, 'btree': btree, 'bulk_var': bulk_var,
            'load_cases': load_cases, 'set_gen': set_gen, 'apply_base': apply_base, 'apply_vms': apply_vms, 'vmf_var': vmf_var, 'cell_done': cell_done,
            'base_add': base_add, 'base_done': base_done, 'base_del': base_del, 'run_args': run_args,
            'refresh_cases': refresh_cases, 'count_var': count_var}


def main():
    root = tk.Tk()
    build(root)
    root.mainloop()
