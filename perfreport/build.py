"""작업 파일(job YAML) 하나 → docx + html + 빌드 기록.

작업 파일 형식은 samples/job_fc_2vm.yaml 참고.
"""
import datetime as dt
import glob
import json
import os
import re
import sys

import pandas as pd
import yaml

from . import analyze, charts, ingest, report_case, report_docx, report_html, runwin
from . import palette as P

# 프로그램 폴더 (profiles/ · rules/ 기본 위치). exe 로 묶였으면 exe 가 있는 폴더.
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve(p, base):
    if os.path.isabs(p):
        return p
    for root in (base, APP_DIR):
        q = os.path.join(root, p)
        if os.path.exists(q):
            return q
    return os.path.join(base, p)


def load_profiles(dirpath):
    out = {}
    for f in sorted(glob.glob(os.path.join(dirpath, '*.yaml'))):
        out[os.path.splitext(os.path.basename(f))[0]] = yaml.safe_load(open(f, encoding='utf-8'))
    return out


def _safe(name):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('_')[:60] or 'metric'


def _scan_terms(docx_path, terms):
    from docx import Document
    d = Document(docx_path)
    txt = '\n'.join([p.text for p in d.paragraphs] + [c.text for t in d.tables for r in t.rows for c in r.cells])
    return [f'"{m.group(0)}" → {t["use"]}' for t in terms for m in [re.search(t['ban'], txt)] if m]


def run(job_path, log=print):
    """작업 파일(YAML) 방식."""
    base = os.path.dirname(os.path.abspath(job_path))
    job = yaml.safe_load(open(job_path, encoding='utf-8'))
    job.setdefault('name', os.path.splitext(os.path.basename(job_path))[0])
    return run_job(job, base, log)


# 케이스 정보 CSV 의 열 이름. 왼쪽이 기본 이름이고 오른쪽은 같은 뜻으로 받는 이름.
CASE_COLUMNS = {
    'id': ['케이스명', '케이스', 'TC', 'case', 'name'],
    'label': ['설명', 'description', 'desc'],
    'host': ['호스트 이름', '호스트', '호스트명', 'host', 'hostname'],
    'start': ['잡 시작 시각', '시작 시각', '시작', 'start', 'job_start'],
    'end': ['잡 종료 시각', '종료 시각', '종료', 'end', 'job_end'],
    'vms': ['VM', 'VM 이름', '대상 VM', 'vms', 'vm'],          # 선택: VM 이름 또는 패턴 (; 로 구분)
    'group': ['테스트 그룹', '테스트그룹', '그룹', 'group', 'test group'],   # 선택: 그룹마다 HTML 1개
}


SHEET_NAMES = ['케이스', '테스트케이스', '테스트 케이스', 'cases', 'Cases']


def _read_table(path):
    if path.lower().endswith(('.xlsx', '.xlsm')):          # 엑셀 시트: '케이스' 시트가 있으면 그 시트, 없으면 첫 시트
        import openpyxl  # noqa: F401  (pandas 가 엑셀을 읽을 때 쓴다 · 실행 파일 묶음에 포함되게 명시)
        xl = pd.ExcelFile(path)
        sheet = next((s for s in SHEET_NAMES if s in xl.sheet_names), xl.sheet_names[0])
        t = xl.parse(sheet, dtype=str, keep_default_na=False)
        t = t.loc[:, [c for c in t.columns if not str(c).startswith('Unnamed')]]
        strip = lambda v: str(v).strip()
        return t.map(strip) if hasattr(pd.DataFrame, 'map') else t.applymap(strip)
    for enc in ('utf-8-sig', 'cp949'):          # 엑셀에서 저장한 CSV 는 둘 중 하나
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f'케이스 정보 파일의 문자 인코딩을 읽지 못함: {path}')


def _parse_when(v):
    v = str(v).strip()
    m = re.match(r'^(.*?)\s*(오전|오후)\s*(\d{1,2})(:\d{2}(?::\d{2})?)$', v)   # 엑셀 한글 표기: 2026-09-14 오후 3:13:24
    if m:
        h = int(m.group(3)) % 12 + (12 if m.group(2) == '오후' else 0)
        v = f'{m.group(1)} {h:02d}{m.group(4)}'
    v = re.sub(r'^(\d{4}-\d{2}-\d{2})_(\d)', r'\1 \2', v)                     # 2026-08-11_19:00:02
    return pd.to_datetime(v.replace('/', '-').replace('.', '-', 2) if re.match(r'^\d{4}[./]', v) else v, errors='coerce')


def cases_from_csv(path):
    t = _read_table(path)
    norm = {c.strip().lower(): c for c in t.columns}
    col = {}
    for key, names in CASE_COLUMNS.items():
        col[key] = next((norm[n.lower()] for n in names if n.lower() in norm), None)
    miss = [CASE_COLUMNS[k][0] for k in ('id', 'host', 'start', 'end') if not col[k]]
    if miss:
        raise SystemExit(f'케이스 정보 파일에 열이 없음: {", ".join(miss)} (있는 열: {", ".join(t.columns)})')
    cases, errs = [], []
    for i, r in t.iterrows():
        cid = r[col['id']].strip()
        if not cid:
            continue
        raw_end = str(r[col['end']]).strip()
        auto = raw_end in ('', '자동', 'auto', 'AUTO', '-')      # 비우면 CPU 사용률로 종료 시각을 감지한다
        st, en = _parse_when(r[col['start']]), (None if auto else _parse_when(raw_end))
        if pd.isna(st) or (not auto and pd.isna(en)):
            errs.append(f'{i + 2}행 {cid}: 시각을 읽지 못함 ("{r[col["start"]]}", "{raw_end}")')
        elif not auto and st >= en:
            errs.append(f'{i + 2}행 {cid}: 시작 시각이 종료 시각보다 늦음')
        cases.append({'id': cid, 'label': r[col['label']].strip() if col['label'] else '',
                      'host': r[col['host']].strip(), 'start': st, 'end': en, 'end_auto': auto,
                      'vms': r[col['vms']].strip() if col['vms'] else '',
                      'group': r[col['group']].strip() if col['group'] else ''})
    if errs:
        raise SystemExit('케이스 정보 파일 오류\n  ' + '\n  '.join(errs))
    ids = [c['id'] for c in cases]
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        raise SystemExit(f'케이스명이 중복됨: {", ".join(dup)}')
    return cases


DATA_EXT = ('.csv', '.log', '.txt')


INDEX_PATH = os.path.join(os.path.expanduser('~'), '.perfreport', 'file_index.json')


def file_index(srcs, profiles, log=print):
    """데이터 파일마다 들어 있는 VM · 호스트 · 시각 범위. 파일 경로 · 크기 · 수정 시각이 같으면 저장해 둔 값을 쓴다.
    데이터 폴더에 파일이 쌓여도 케이스마다 필요한 파일만 읽게 하는 데 쓴다."""
    try:
        cache = json.load(open(INDEX_PATH, encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    out, new = {}, 0
    for s in srcs:
        st = os.stat(s['path'])
        key = f'{os.path.abspath(s["path"])}|{st.st_size}|{int(st.st_mtime)}|{s["profile"]}'
        if key not in cache:
            df = ingest.load_source(s, profiles[s['profile']])
            cache[key] = {'entities': sorted(df['entity'].astype(str).unique().tolist()),
                          'hosts': sorted(df['host'].astype(str).unique().tolist()),
                          'tmin': str(df['t'].min()) if len(df) else '', 'tmax': str(df['t'].max()) if len(df) else ''}
            new += 1
        out[s['path']] = cache[key]
    if new:
        os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
        json.dump(cache, open(INDEX_PATH, 'w', encoding='utf-8'), ensure_ascii=False)
        log(f'[색인] 새로 읽은 파일 {new}개 · 저장된 색인 사용 {len(srcs) - new}개')
    return out


def _file_matches(ix, case):
    """케이스의 실행 구간과 시각이 겹치고, 케이스의 VM · 호스트가 들어 있는 파일인지."""
    import fnmatch
    if not ix['tmin']:
        return False
    m = pd.Timedelta(seconds=case.get('margin_s', 0) or 0)
    if pd.Timestamp(ix['tmax']) < pd.Timestamp(case['start']) - m or pd.Timestamp(ix['tmin']) > pd.Timestamp(case['end']) + m:
        return False
    ents = [e.lower() for e in ix['entities']]
    if case.get('vms'):
        pats = [p.strip().lower() for p in re.split(r'[;,]', case['vms']) if p.strip()]
        return any(fnmatch.fnmatchcase(e, p) for e in ents for p in pats)
    if case.get('host'):
        hosts = [h.strip().lower() for h in re.split(r'[;,]', case['host']) if h.strip()]
        return any(h in [x.lower() for x in ix['hosts']] or any(e.startswith(h) for e in ents) for h in hosts)
    return True


def run_csv(cases_csv, data_dir, out_dir=None, title=None, baseline=None, analysis=None, log=print,
            split_dir=None, report=True, margin_s=None):
    """케이스 정보 CSV + 데이터 폴더 방식. 데이터 폴더의 모든 파일을 읽어 케이스마다 호스트·잡 실행 구간으로 잘라 쓴다."""
    cases = cases_from_csv(cases_csv)
    profiles = load_profiles(os.path.join(APP_DIR, 'profiles', 'inputs'))
    files = []
    for root, _, names in os.walk(data_dir):
        for n in sorted(names):
            if n.lower().endswith(DATA_EXT) and os.path.abspath(os.path.join(root, n)) != os.path.abspath(cases_csv):
                files.append(os.path.join(root, n))
    srcs = []
    for f in files:
        prof = ingest.detect_profile(f, profiles)
        if prof:
            srcs.append({'profile': prof, 'path': f})
            log(f'[데이터] {os.path.relpath(f, data_dir)} → {profiles[prof].get("source_system", prof)}')
        else:
            log(f'[건너뜀] {os.path.relpath(f, data_dir)}: 아는 형식이 아님')
    if not srcs:
        raise SystemExit(f'데이터 폴더에서 읽을 수 있는 파일이 없음: {data_dir}')
    index = file_index(srcs, profiles, log)
    margin_s = runwin.MARGIN_S if margin_s is None else margin_s
    for c in cases:
        c['margin_s'] = margin_s
        if c.get('end_auto'):    # 종료 시각 자동 감지: 잡 시작 후 72시간 또는 같은 호스트 다음 케이스 시작 전까지에서 찾는다
            nxt = [o['start'] for o in cases if o is not c and o.get('host') == c.get('host') and o['start'] > c['start']]
            c['end'] = min([c['start'] + pd.Timedelta(hours=runwin.AUTO_MAX_H)] + nxt)
            probe = dict(c, margin_s=0)
            probe['sources'] = [dict(s) for s in srcs if _file_matches(index[s['path']], probe)]
            if not probe['sources']:
                raise SystemExit(f'{c["id"]}: 잡 시작 이후 데이터 파일이 없어 종료 시각을 감지하지 못함')
            pdf, _, _ = ingest.load_case(probe, profiles)
            end, why = runwin.detect_end(pdf, c['start'])
            if end is None:
                raise SystemExit(f'{c["id"]}: 종료 시각 자동 감지 실패 ({why}). 케이스 정보에 잡 종료 시각을 적는다.')
            c['end'], c['end_reason'] = end, why
            log(f'[종료 감지] {c["id"]}: {end} ({why})')
        c['sources'] = [dict(s) for s in srcs if _file_matches(index[s['path']], c)]
        log(f'[선택] {c["id"]}: 데이터 파일 {len(c["sources"])}/{len(srcs)}개')
        if not c['sources']:
            raise SystemExit(f'{c["id"]}: 데이터 없음: 호스트 이름({c.get("host", "-")})'
                             + (f'·VM({c["vms"]})' if c.get('vms') else '')
                             + f'과 실행 구간({c["start"]} ~ {c["end"]})에 해당하는 데이터 파일이 없다.')
    checks = {}
    for c in cases:              # 수집 끊김 점검 (실행 구간 안): 리포트 · TC 정리 모두 실행
        cdf, _, _ = ingest.load_case(c, profiles)
        c['collection_issues'], checks[c['id']] = runwin.check_collection(cdf, c)
        c['collection_check'] = checks[c['id']]
        log(f'[수집 점검] {c["id"]}: ' + checks[c['id']].replace('\n', ' / '))
        if split_dir:            # TC 별 정리: 원본에서 이 TC 의 VM · 구간(앞뒤 여유 포함) 행만 잘라 저장
            runwin.split_case(c, c['sources'], profiles, set(cdf['entity']), split_dir, data_dir, log)
    if not report:
        return {'cases': [c['id'] for c in cases], 'checks': checks,
                'windows': {c['id']: [str(c['start']), str(c['end']), bool(c.get('end_auto'))] for c in cases}}
    name = os.path.splitext(os.path.basename(cases_csv))[0]
    job = {'name': name, 'title': title or f'성능 비교 리포트 ({", ".join(c["id"] for c in cases[:4])}{" 외" if len(cases) > 4 else ""})',
           'cases': cases, 'baseline': baseline or cases[0]['id'],
           'analysis': analysis or 'profiles/analysis/default.yaml',
           'output': out_dir or os.path.join(os.path.dirname(os.path.abspath(cases_csv)), 'out_' + name)}
    return run_job(job, os.path.dirname(os.path.abspath(cases_csv)), log)


def run_job(job, base, log=print):
    profiles = load_profiles(_resolve(job.get('input_profiles', 'profiles/inputs'), base))
    ana = yaml.safe_load(open(_resolve(job.get('analysis', 'profiles/analysis/default.yaml'), base), encoding='utf-8'))
    terms = json.load(open(_resolve('rules/terms.json', base), encoding='utf-8'))['terms']
    outdir = os.path.join(base, job.get('output', 'out'))   # 절대 경로면 join 이 그대로 둔다
    os.makedirs(os.path.join(outdir, 'charts'), exist_ok=True)

    cases = job['cases']
    frames, sources, case_meta, case_start = [], [], [], {}
    for c in cases:
        for s in c['sources']:
            s['path'] = _resolve(s['path'], base)
            if s['profile'] not in profiles:
                raise SystemExit(f'입력 프로파일 없음: {s["profile"]} (TC {c["id"]})')
        df, start, end = ingest.load_case(c, profiles)
        if df.empty:
            raise SystemExit(f'{c["id"]}: 데이터 없음: 호스트 이름({c.get("host", "-")})'
                             + (f'·VM({c["vms"]})' if c.get('vms') else '')
                             + f'과 실행 구간({c.get("start", "-")} ~ {c.get("end", "-")})에 해당하는 기록이 데이터 파일에 없다. '
                             'VM 이름이 호스트 이름으로 시작하지 않으면 케이스 정보 CSV 의 VM 열에 VM 이름이나 패턴(예: app-*;db01)을 적는다.')
        log(f'[읽기] {c["id"]}: {len(df):,}개 값 · {start} ~ {end}' + (f' · 호스트 {c["host"]}' if c.get('host') else ''))
        frames.append(df)
        case_start[c['id']] = start
        ents = df[['entity', 'host', 'role']].drop_duplicates().sort_values('role')
        case_meta.append({'id': c['id'], 'label': c.get('label', ''), 'start': start, 'end': end, 'host': c.get('host', ''),
                          'entities': ', '.join(r.entity if r.entity.endswith(r.role) else f'{r.entity} ({r.role})'
                                                for r in ents.itertuples())})
        for s in c['sources']:
            n = int(((df['_src'] == s['path']) if '_src' in df else pd.Series(False)).sum())
            if n == 0:
                continue                               # 이 케이스에 쓰이지 않은 파일은 출처 표에 넣지 않는다
            sources.append({'case': c['id'], 'system': profiles[s['profile']].get('source_system', s['profile']),
                            'file': os.path.basename(s['path']), 'n': n})
    df = pd.concat(frames, ignore_index=True)

    # 지표 선택: 분석 프로파일의 metrics 목록 순서. 'all' 이면 데이터에 있는 전부.
    want = ana.get('metrics', 'all')
    present = list(dict.fromkeys(df['metric']))
    metrics = present if want == 'all' else [m for m in want if m in set(present)]
    missing = [] if want == 'all' else [m for m in want if m not in set(present)]
    for m in missing:
        log(f'[주의] 분석 프로파일의 지표가 데이터에 없음: {m}')
    df = df[df['metric'].isin(metrics)]

    case_order = [c['id'] for c in cases]
    role_order = ana.get('role_order') or []
    role_order = [r for r in role_order if r in set(df['role'])] + sorted(set(df['role']) - set(role_order))
    baseline = job.get('baseline', case_order[0])
    stat = ana.get('compare_stat', 'mean')

    stats = analyze.stats_table(df)
    cmp = analyze.compare(stats, baseline, stat)
    cmp['baseline_case'] = baseline

    # 선 색 지정 (선택). HTML 의 '색 설정 내보내기' 파일을 colors_file 로 주거나 colors 에 직접 적는다.
    colors = {}
    if job.get('colors_file'):
        colors.update(json.load(open(_resolve(job['colors_file'], base), encoding='utf-8')))
    colors.update(job.get('colors') or {})

    # 케이스별 문서: 케이스 1건 = docx 1개, 수집 지표 전부를 분류별 그래프로
    date = dt.date.today().isoformat()
    docs, n_charts = [], 0
    meta = {c['id']: c for c in case_meta}
    for c in cases:
        cm = meta[c['id']]
        cm['group'] = c.get('group', '')
        dpath = os.path.join(outdir, f'{_safe(c["id"])}_performance_report.docx')
        _, k = report_case.build_case(dpath, {'id': c['id'], 'label': c.get('label', ''), 'host': c.get('host', ''),
                                              'start': cm['start'], 'end': cm['end'], 'group': c.get('group', ''),
                                              'end_auto': c.get('end_auto'), 'end_reason': c.get('end_reason', ''),
                                              'margin_s': c.get('margin_s', 0),
                                              'collection_issues': c.get('collection_issues', []),
                                              'collection_check': c.get('collection_check', '')},
                                      df[df['case'] == c['id']], os.path.join(outdir, 'charts', _safe(c['id'])), date, log)
        docs.append(dpath)
        n_charts += k
        log(f'[문서] {c["id"]}: 그래프 {k}장')
    term_hits = []
    for dpath in docs:
        term_hits += [f'{os.path.basename(dpath)}: {h}' for h in _scan_terms(dpath, terms)]
    for h in term_hits:
        log(f'[검사] 통제 용어 검출: {h}')

    # 반응형 HTML: 테스트 그룹마다 1개 (그룹 열이 없으면 1개)
    groups = list(dict.fromkeys(c.get('group') or '' for c in cases))
    htmls = []
    for g in groups:
        ids = [c['id'] for c in cases if (c.get('group') or '') == g]
        sub = df[df['case'].isin(ids)]
        gbase = baseline if baseline in ids else ids[0]
        gcmp = analyze.compare(analyze.stats_table(sub), gbase, stat)
        gmetrics = [m for m in metrics if m in set(sub['metric'])]
        ctx = {'title': (f'{g}: ' if g else '') + job.get('title', '성능 비교 리포트'), 'date': date, 'baseline': gbase,
               'cases': [m for m in case_meta if m['id'] in ids],
               'metrics': [{'name': m, 'unit': sub[sub['metric'] == m]['unit'].iloc[0]} for m in gmetrics],
               'role_order': role_order, 'case_start': case_start, 'colors': colors}
        fname = re.sub(r'[\\/:*?"<>|]+', '_', g).strip() if g else _safe(job.get('name', 'report'))
        htmls.append(report_html.build(os.path.join(outdir, f'{fname}.html'), ctx, sub, gcmp))
        if g:
            log(f'[HTML] 그룹 {g}: 케이스 {len(ids)}건')
    html_path = htmls[0]
    rec = {'job': job.get('name'), 'built_at': dt.datetime.now().isoformat(timespec='seconds'),
           'cases': case_order, 'baseline': baseline, 'metrics': len(metrics), 'values': int(len(df)),
           'charts': n_charts, 'term_violations': term_hits, 'outputs': docs + htmls,
           'windows': {c['id']: [str(meta[c['id']]['start']), str(meta[c['id']]['end']), bool(c.get('end_auto'))] for c in cases},
           'checks': {c['id']: c.get('collection_check', '') for c in cases},
           'groups': {g or '-': [c['id'] for c in cases if (c.get('group') or '') == g] for g in groups}}
    json.dump(rec, open(os.path.join(outdir, 'BUILD.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    # 수집 끊김 점검 결과: 케이스 정보 엑셀의 비고 열에 옮겨 적을 수 있게 표로도 남긴다
    pd.DataFrame([{'케이스명': k, '수집 점검': v} for k, v in rec['checks'].items()]) \
        .to_csv(os.path.join(outdir, 'collection_check.csv'), index=False, encoding='utf-8-sig')
    log('[완료] ' + '\n[완료] '.join(rec['outputs']))
    return rec
