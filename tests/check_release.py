"""자동 시험: 견본 데이터로 리포트를 만들어 DEVELOPER.md 3절의 설계 규칙을 검사한다.
사용: python tests/check_release.py   (패키지 폴더 어디서 실행해도 된다. 결과는 임시 폴더에 만들고 지운다)"""
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FAIL = []


def check(name, ok, detail=''):
    print(('통과  ' if ok else '실패  ') + name + (f'  ({detail})' if detail and not ok else ''))
    if not ok:
        FAIL.append(name)


def body_items(doc):
    """문서 본문을 순서대로: ('p', 문단) · ('t', 표)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    out = []
    for el in doc.element.body.iterchildren():
        if el.tag.endswith('}p'):
            out.append(('p', Paragraph(el, doc)))
        elif el.tag.endswith('}tbl'):
            out.append(('t', Table(el, doc)))
    return out


def check_docx(path, terms):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    d = docx.Document(path)
    name = os.path.basename(path)
    items = body_items(d)
    h1 = [p.text for k, p in items if k == 'p' and p.style.name == 'Heading 1']
    body = [re.sub(r'^\d+\. ', '', h) for h in h1 if not h.startswith('Appendix')]
    want = ['케이스 식별', '요약', 'TAT', 'phase 소요 시간', '동시 실행 잡']
    seen = [h for h in body if h in want]
    check(f'{name}: 절 순서', seen == [w for w in want if w in seen] and body[:2] == want[:2], str(body))
    nums = [int(h.split('.')[0]) for h in h1 if not h.startswith('Appendix')]
    check(f'{name}: 절 번호 연속', nums == list(range(1, len(nums) + 1)))
    appx = [h.split('. ', 1)[1] for h in h1 if h.startswith('Appendix')]
    tail = ['데이터 출처', '지표별 표본 수', '데이터 가용성']
    check(f'{name}: Appendix 구성', appx[-3:] == tail and [h[9] for h in h1 if h.startswith('Appendix')] == list('ABCDEF'[:len(appx)]), str(appx))
    has_jobs = any(h in seen for h in want[2:])
    check(f'{name}: 잡 결과 표는 Appendix', ('잡 결과 표' in appx) == has_jobs)
    # 본문 TAT · phase · 동시 실행 잡 절에는 표가 없어야 한다
    cur, bad = '', 0
    for k, x in items:
        if k == 'p' and x.style.name == 'Heading 1':
            cur = re.sub(r'^\d+\. ', '', x.text)
        elif k == 't' and cur in want[2:]:
            deg = cur == 'TAT' and x.rows[0].cells[-1].text == 'Degradation (%)'      # 기준선 대비 표만 본문에 둔다
            bad += not deg
    check(f'{name}: TAT · phase · 동시 실행 잡 본문에 잡 결과 표 없음 (기준선 대비 표 제외)', bad == 0)
    if 'TAT' in seen:
        check(f'{name}: 기준선을 주면 TAT 절에 Degradation 표', any(t.rows[0].cells[-1].text == 'Degradation (%)' for t in d.tables))
    # 표 제목: 모든 표 바로 위, 왼쪽 정렬, '표. ' 로 시작
    bad = []
    for i, (k, x) in enumerate(items):
        if k == 't':
            prev = items[i - 1][1] if i and items[i - 1][0] == 'p' else None
            if prev is None or not prev.text.startswith('표. ') or prev.alignment not in (None, WD_ALIGN_PARAGRAPH.LEFT):
                bad.append(prev.text[:30] if prev is not None else '제목 없음')
    check(f'{name}: 표 제목은 표 위 · 왼쪽', not bad, str(bad[:3]))
    # 그림 제목: 그림 문단 바로 아래
    bad = 0
    pars = [x for k, x in items if k == 'p']
    for i, p in enumerate(pars):
        if p._element.xpath('.//w:drawing'):
            nxt = pars[i + 1].text if i + 1 < len(pars) else ''
            bad += not nxt.startswith('그림. ')
        if p.text.startswith('그림. ') and not (i and pars[i - 1]._element.xpath('.//w:drawing')):
            bad += 1
    check(f'{name}: 그림 제목은 그림 아래', bad == 0)
    cells = [c.text for t in d.tables for r in t.rows for c in r.cells]
    text = '\n'.join([p.text for p in d.paragraphs] + cells)
    check(f'{name}: 문서에 | 없음', '|' not in text)
    check(f'{name}: 값 없는 칸은 빈 칸(- 없음)', not any(c.strip() == '-' for c in cells))
    merged = len(d.element.body.xpath('.//w:gridSpan | .//w:vMerge'))
    check(f'{name}: 병합 칸 없음', merged == 0)
    check(f'{name}: 원 데이터 표기 없음', '원 데이터' not in text)
    hit = sorted({t.get('show', t['ban']) for t in terms if re.search(t['ban'], text)})
    check(f'{name}: 통제 용어 0건', not hit, str(hit))
    s = d.sections[0]
    w, h = round(s.page_width / 360000, 1), round(s.page_height / 360000, 1)
    check(f'{name}: A4 세로 용지', (w, h) == (21.0, 29.7), f'{w} x {h}')
    # 지표 소제목: 지표 절의 그림마다 같은 이름의 제목 2
    h2 = {p.text for p in d.paragraphs if p.style.name in ('Heading 2', 'Heading 3')}
    figs = [p.text[4:] for p in d.paragraphs if p.text.startswith('그림. ')]
    miss = [f for f in figs if not any(f == x or f.startswith(x + ' (') for x in h2)]
    check(f'{name}: 그림마다 소제목', not miss, str(miss[:3]))
    return h1


def unit_checks(tmp):
    import pandas as pd
    from perfreport import jobs, phase, runwin, units, report_case, docx_base

    # phase: 열이 알파벳순이고 VM 이름이 '…Phase-Hostname' 열에 있는 파일(로그 수집 시스템에서 내보낸 형식)
    stages = phase.STAGE_ORDER
    cols = sorted(f'{s}_{k}' for s in stages for k in ('self', 'total'))
    p = os.path.join(tmp, 'phase_export.csv')
    with open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['text', 'hostname', 'JobStart_KST', 'Job-Phase-Hostname', 'TotalElapsed_Sec'] + cols)
        acc = {s: (i + 1) * 100 for i, s in enumerate(stages)}
        w.writerow(['x', 'sender01', '2026-01-05T10-00-02', 'esx01-vm01', acc[stages[-1]]]
                   + [100 if c.endswith('_self') else acc[c[:-6]] for c in cols])
    js = phase.read_jobs(p)
    check('phase: 열이 알파벳순이어도 실행 순서', js and js[0]['order'] == stages, str(js[0]['order'][:4]) if js else '읽지 못함')
    check('phase: VM 은 …Phase-Hostname 열', js and js[0]['host'] == 'esx01-vm01', js[0]['host'] if js else '')

    from perfreport.build import vm_filter_patterns as vf
    check('VM 필터: 낱말은 포함 검색, 빈 값은 전부', [vf(x) for x in ('vm01', 'vm01, vm02', '', 'app-*')] == ['*vm01*', '*vm01*;*vm02*', '', 'app-*'])

    # 잡 구분: TAT 측정 잡과 같은 종류의 다른 실행은 동시 실행 잡이 아니다
    t0 = pd.Timestamp('2026-01-05 10:00:00')
    rows = []
    def add(host, code, start, sec, n=4):
        for j in range(n):
            s = start + pd.Timedelta(seconds=j)
            rows.append({'host': host, 'code': code, 'info': f'{code}_{j + 1}_of_{n}', 'start': s,
                         'end': s + pd.Timedelta(seconds=sec), 'elapsed': float(sec), 'row': {}})
    add('vm01', 'sim', t0, 12000)
    add('vm01', 'sim', t0 + pd.Timedelta(seconds=12300), 9000)       # 같은 종류의 다음 실행
    for k in range(4):
        add('vm02', 'load', t0 + pd.Timedelta(seconds=5 + 3000 * k), 2900)
    c = jobs.classify({'id': 'T', 'start': t0, 'end': t0 + pd.Timedelta(seconds=12600)}, rows)
    check('잡: TAT 측정 잡 판정', len(c['mains']) == 1 and c['mains'][0]['code'] == 'sim' and abs(c['mains'][0]['tat'] - 12000) < 1)
    check('잡: 같은 종류의 다른 실행은 동시 실행 잡에서 제외', [g['code'] for g in c['conc']] == ['load'] and c['mains'][0]['other_sets'] == 1)

    # 종료 시각: 기준 VM 의 TAT 원장 종료가 1순위, 없으면 CPU 10분 유휴
    recs = []
    for k in range(-5, 200):
        t = t0 + pd.Timedelta(minutes=k)
        for vm, until in (('vm01', 100), ('vm02', 150)):
            recs.append({'entity': vm, 't': t, 'metric': 'CPU|Usage (%)', 'value': 70.0 if 0 <= k < until else 1.0})
    df = pd.DataFrame(recs)
    end, why = runwin.detect_end(df, t0, refs=['vm01'])
    check('종료 시각: 기준 VM 의 CPU 유휴 시작', end is not None and abs((end - t0).total_seconds() - 6000) <= 300, str(end))
    tj = [{'host': 'vm01', 'code': 'sim', 'start': t0, 'end': t0 + pd.Timedelta(minutes=101), 'elapsed': 6060.0}]
    end, why = runwin.detect_end(df, t0, refs=['vm01'], tat_jobs=tj)
    check('종료 시각: TAT 원장 종료가 1순위', end == t0 + pd.Timedelta(minutes=101), str(end))

    # THP Coverage: 케이스 · VM 단위로 판단
    recs = []
    for case, has in (('A', True), ('B', False)):
        for m, v in (('AnonHugePages_MB', 50.0), ('AnonPages_MB', 100.0)) + ((('THP_Coverage_Pct', 40.0),) if has else ()):
            recs.append({'case': case, 'entity': 'vm01', 't': t0, 'metric': m, 'value': v, 'unit': ''})
    d = units.derive_thp(pd.DataFrame(recs))
    got = d[d['metric'] == units.THP_COVERAGE]
    check('THP Coverage: coverage 지표가 없는 케이스에만 계산', list(got['case']) == ['B'] and abs(got['value'].iloc[0] - 50.0) < 1e-6)

    # 용지: A4 세로 → A4 가로 → A3 가로
    sizes = []
    for wcm in (16.5, 25, 31.5):
        doc = docx_base.new_document()
        report_case._page_for(doc, wcm)
        s = doc.sections[0]
        sizes.append((round(s.page_width / 360000, 1), round(s.page_height / 360000, 1)))
    check('용지: A4 세로 · A4 가로 · A3 가로', sizes == [(21.0, 29.7), (29.7, 21.0), (42.0, 29.7)], str(sizes))


def main():
    tmp = tempfile.mkdtemp(prefix='perfreport_check_')
    env = dict(os.environ, HOME=tmp, USERPROFILE=tmp, APPDATA=tmp, MPLBACKEND='Agg')      # 색인 · 설정 파일을 사용자 폴더에 남기지 않는다
    try:
        out, split = os.path.join(tmp, 'out'), os.path.join(tmp, 'split')
        r = subprocess.run([sys.executable, '-m', 'perfreport', 'run', 'samples/sample_cases.csv', 'samples/sample_data',
                            '--out', out, '--split', split, '--workers', '1', '--tat-baseline', '13800', '--tat-baseline-note', 'sample'],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        log = r.stdout + r.stderr
        check('견본 실행 종료 코드 0', r.returncode == 0, log[-400:])
        check('실행 로그에 오류 없음', 'Traceback' not in log)
        for tag in ('잡 TAT 원장 CSV', 'phase 소요 시간 CSV', '[phase]', '[잡]'):
            check(f'실행 로그: {tag}', tag in log)
        terms = json.load(open(os.path.join(ROOT, 'rules', 'terms.json'), encoding='utf-8'))['terms']
        docs = sorted(glob.glob(os.path.join(out, '*.docx')))
        check('케이스마다 문서 1개', len(docs) == 3, str(len(docs)))
        heads = {os.path.basename(p): check_docx(p, terms) for p in docs}
        b = [re.sub(r'^\d+\. ', '', h) for h in heads.get('CASE-B_performance_report.docx', [])]
        check('CASE-B: TAT · phase · 동시 실행 잡 절 있음', b[2:5] == ['TAT', 'phase 소요 시간', '동시 실행 잡'], str(b[:6]))
        htmls = sorted(glob.glob(os.path.join(out, '*.html')))
        check('그룹마다 HTML 1개', len(htmls) == 2, str(len(htmls)))
        for h in htmls:
            s = open(h, encoding='utf-8').read()
            name = os.path.basename(h)
            ext = re.findall(r'<(?:script|link|img|iframe)\b[^>]*?(?:src|href)\s*=\s*["\'](https?://[^"\']+)', s)
            check(f'{name}: 외부 요청 없음', not ext, str(ext[:2]))
            check(f'{name}: 잡 결과 그래프 줄', all(k in s for k in ('optTat', 'optConc', 'optSets', 'phaseBand')))
        tun = open(os.path.join(out, 'tuning.html'), encoding='utf-8').read() if os.path.exists(os.path.join(out, 'tuning.html')) else ''
        check('tuning.html: phase · 잡 데이터 포함', '"phases"' in tun and '"jobs"' in tun and 'transient' in tun)
        names = sorted(os.listdir(os.path.join(split, 'CASE-B'))) if os.path.isdir(os.path.join(split, 'CASE-B')) else []
        pat = re.compile(r'^CASE-B-(ops|tat|phase)(-\d+)?-\d{8}\.csv$|^CASE-B-etc-\d+-\d{8}\.\w+$|^(case\.txt|manifest\.csv)$')
        check('TC 정리 파일 이름', bool(names) and all(pat.match(n) for n in names) and any('-tat-' in n for n in names) and any('-phase-' in n for n in names), str(names))
        r2 = subprocess.run([sys.executable, '-m', 'perfreport', 'run', 'samples/sample_cases.csv', 'samples/sample_data',
                             '--out', os.path.join(tmp, 'out_only'), '--workers', '1', '--only', 'CASE-B'],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        only_docs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(tmp, 'out_only', '*.docx')))
        check('--only: 고른 케이스만 생성', r2.returncode == 0 and only_docs == ['CASE-B_performance_report.docx'], str(only_docs))
        if only_docs:
            import docx
            d2 = docx.Document(os.path.join(tmp, 'out_only', only_docs[0]))
            check('기준선이 없으면 Degradation 표 없음', not any(t.rows[0].cells[-1].text == 'Degradation (%)' for t in d2.tables))
        unit_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print('전부 통과' if not FAIL else f'실패 {len(FAIL)}건: ' + ' / '.join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
