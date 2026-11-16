"""케이스 요약 파일: 리포트를 만들 때 결과 폴더의 case_summary\\<케이스명>.json 에 남기고, 다음 리포트에서 '이전 결과' 로 읽는다.

담는 것: 케이스 정보(이름, 설명, 호스트, 실행 구간), 지표 통계(잡 실행 구간의 평균 · p95 · 최대 · 최소 · 표본 수),
지표 시계열(구간 평균으로 줄인 값, VM 마다 최대 약 300점), 잡 TAT 원장 정리(TAT 측정 잡 · 동시 실행 잡), phase 요약.
이전 결과 케이스는 비교 기준 케이스(케이스 리포트)와 비교 리포트의 케이스로 쓴다. 원 데이터 파일은 다시 읽지 않는다.
"""
import datetime as dt
import glob
import json
import os
import re

import pandas as pd

VERSION = 1
DIRNAME = 'case_summary'
PREFIX = 'prior:'
MAX_POINTS = 300


def _safe(s):
    return re.sub(r'[\\/:*?"<>|]+', '_', str(s)).strip(' .') or 'case'


def write(outdir, case, meta, df, st, built_at=None):
    """케이스 1건의 요약 파일을 쓴다. df: 이 케이스의 값 표(case 열 = 케이스명), st: analyze.stats_table 결과(모든 케이스)."""
    cid = case['id']
    d = df[df['case'] == cid]
    s = st[st['case'] == cid]
    series = {}
    for (m, e), g in d.groupby(['metric', 'entity']):
        g = g.sort_values('sec')
        span = float(g['sec'].max() - g['sec'].min()) if len(g) > 1 else 0.0
        step = max(1.0, span / MAX_POINTS)
        if len(g) > MAX_POINTS:
            g = g.assign(sec=(g['sec'] // step) * step + step / 2).groupby('sec', as_index=False)['value'].mean()
        series.setdefault(m, {})[str(e)] = [[round(float(a), 1), round(float(b), 6)] for a, b in zip(g['sec'], g['value'])]
    data = {'version': VERSION, 'id': cid, 'label': case.get('label', ''), 'host': case.get('host', ''), 'group': case.get('group', ''),
            'start': str(meta['start']), 'end': str(meta['end']), 'built_at': built_at or dt.datetime.now().isoformat(timespec='seconds'),
            'units': {m: u for m, u in d.drop_duplicates('metric').set_index('metric')['unit'].items()},
            'stats': [{k: (r[k] if k in ('metric', 'entity', 'unit', 'role', 'host') else float(r[k])) for k in
                       ('metric', 'entity', 'unit', 'role', 'host', 'n', 'mean', 'p95', 'max', 'min')} for _, r in s.iterrows()],
            'series': series, 'jobs': case.get('jobs'), 'phase': case.get('phase')}
    path = os.path.join(outdir, DIRNAME, _safe(cid) + '.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)
    return path


def scan(folders):
    """이전 결과 폴더(여러 개)에서 케이스 요약 파일을 찾는다. 폴더 자체, 그 아래 case_summary, 한 단계 아래 폴더의 case_summary 를 본다.
    반환: [{'key', 'id', 'label', 'host', 'built_at', 'path', 'display'}] (같은 파일은 한 번만, 결과 날짜가 늦은 것부터)."""
    found, seen = [], set()
    for folder in folders or []:
        folder = str(folder).strip()
        if not folder or not os.path.isdir(folder):
            continue
        pats = [os.path.join(folder, '*.json'), os.path.join(folder, DIRNAME, '*.json'), os.path.join(folder, '*', DIRNAME, '*.json')]
        for p in sorted({x for pat in pats for x in glob.glob(pat)}):
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            try:
                with open(ap, encoding='utf-8') as f:
                    head = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(head, dict) or head.get('version') != VERSION or not head.get('id') or 'stats' not in head:
                continue
            seen.add(ap)
            day = str(head.get('built_at', ''))[5:10]
            found.append({'key': PREFIX + ap, 'id': head['id'], 'label': head.get('label', ''), 'host': head.get('host', ''),
                          'built_at': head.get('built_at', ''), 'path': ap,
                          'display': f'{head["id"]} ({day} 결과)' if day else f'{head["id"]} (이전 결과)'})
    found.sort(key=lambda x: x['built_at'], reverse=True)
    return found


def is_prior(key):
    return str(key or '').startswith(PREFIX)


def load(key):
    """요약 파일을 읽는다. key: 'prior:<경로>' 또는 경로."""
    path = key[len(PREFIX):] if is_prior(key) else key
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def as_case(key, sm, display=None):
    """요약을 케이스처럼 쓸 수 있는 dict 로: (케이스 dict, meta, 값 표(시계열, in_run=False), 통계 표)."""
    case = {'id': key, 'show_id': sm['id'], 'label': sm.get('label', ''), 'host': sm.get('host', ''), 'group': sm.get('group', ''),
            'jobs': sm.get('jobs'), 'phase': sm.get('phase'), 'prior': True, 'display': display or sm['id']}
    meta = {'id': key, 'label': sm.get('label', ''), 'start': pd.Timestamp(sm['start']), 'end': pd.Timestamp(sm['end']), 'host': sm.get('host', '')}
    units = sm.get('units', {})
    rows = [(key, e, m, units.get(m, ''), a, b) for m, by in (sm.get('series') or {}).items() for e, pts in by.items() for a, b in pts]
    df = pd.DataFrame(rows, columns=['case', 'entity', 'metric', 'unit', 'sec', 'value'])
    df['in_run'] = False            # 통계는 요약 파일의 값을 쓴다 (줄인 시계열로 다시 계산하지 않는다)
    df['role'] = ''
    df['host'] = sm.get('host', '')
    st = pd.DataFrame(sm.get('stats') or [])
    if len(st):
        st['case'] = key
    return case, meta, df, st
