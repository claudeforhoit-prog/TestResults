"""창 화면: 케이스 정보 CSV 와 데이터 폴더를 고르고 [리포트 생성] 을 누르면 docx · html 을 만든다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


def main():
    root = tk.Tk()
    root.title('성능 리포트 생성기')
    root.geometry('820x560')
    v = {k: tk.StringVar() for k in ('cases', 'data', 'out', 'split', 'title', 'baseline')}
    core = tk.BooleanVar(value=False)
    q = queue.Queue()
    last = {}

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
        '지정하면 원본에서 케이스마다 VM·실행 구간(앞뒤 10분)만 잘라 <폴더>/<케이스명>/ 에 저장')
    row(7, '제목 (선택)', 'title')
    row(8, '기준 케이스 (선택)', 'baseline', hint='비우면 케이스 정보의 첫 번째 케이스')
    tk.Checkbutton(frm, text='VM 핵심 지표만 넣기 (CPU · 메모리 등 8종)', variable=core).grid(row=10, column=1, sticky='w')

    btns = tk.Frame(root, padx=12)
    btns.pack(fill='x')
    run_btn = tk.Button(btns, text='리포트 생성', width=14)
    run_btn.pack(side='left')
    open_btn = tk.Button(btns, text='결과 폴더 열기', width=14, state='disabled')
    open_btn.pack(side='left', padx=6)

    log = scrolledtext.ScrolledText(root, height=18, font=('Malgun Gothic', 9))
    log.pack(fill='both', expand=True, padx=12, pady=10)

    def worker(args):
        from .build import run_csv
        try:
            rec = run_csv(*args[:6], log=lambda m: q.put(('log', m)), split_dir=args[6])
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
        args = (v['cases'].get(), v['data'].get(), v['out'].get() or None, v['title'].get() or None,
                v['baseline'].get() or None, 'profiles/analysis/vm_core.yaml' if core.get() else None,
                v['split'].get() or None)
        run_btn.config(state='disabled')
        log.insert('end', f'── 시작: {args[0]}\n')
        threading.Thread(target=worker, args=(args,), daemon=True).start()

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
    root.after(200, poll)
    root.mainloop()
