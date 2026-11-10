"""반응형 HTML 리포트: 파일 하나에 데이터와 plotly.js 를 넣어 인터넷 없이 열린다."""
import json
import os
import re

import numpy as np

from . import metrics
from . import palette as palette_mod
from . import jobs as jobs_mod
from . import phase as phase_mod
from . import roles as roles_mod

HERE = os.path.dirname(os.path.abspath(__file__))


def _plotly_js():
    import plotly
    return open(os.path.join(os.path.dirname(plotly.__file__), 'package_data', 'plotly.min.js'), encoding='utf-8').read()


def _num(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return float(f'{v:.4g}') if abs(v) < 1e6 else round(float(v))


def _nums(a):
    """_num 의 열 단위 판: 유효숫자 4자리(100만 이상은 정수), 빈 값은 None. 값마다 함수를 부르지 않는다."""
    a = np.asarray(a, dtype=float)
    out = np.char.mod('%.4g', a).astype(float)
    big = np.abs(a) >= 1e6
    out[big] = np.round(a[big])
    lst = out.tolist()
    for i in np.flatnonzero(np.isnan(a)):
        lst[i] = None
    return lst


def _natural(t):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', str(t))]


def _vms(df):
    """VM 목록: 호스트·VM 이름 순. 각 VM 이 들어 있는 TC 를 함께 넣는다."""
    g = df.groupby(['host', 'entity', 'role'])['case'].unique().reset_index()
    rows = [{'host': r.host, 'entity': r.entity, 'role': r.role, 'cases': sorted(r.case)} for r in g.itertuples()]
    return sorted(rows, key=lambda v: (_natural(v['host']), _natural(v['entity'])))


def build(path, ctx, df, stats):
    series = []
    for (case, role, entity, host, metric), g in df.groupby(['case', 'role', 'entity', 'host', 'metric']):
        g = g.sort_values('sec')
        t0 = ctx['case_start'][case]
        series.append({'case': case, 'role': role, 'entity': entity, 'host': host, 'metric': metric,
                       't0': int(t0.value // 1_000_000),   # 현지 시각을 그대로 ms 로 (브라우저 시간대 변환 없음)
                       'x': np.round(g['sec'].to_numpy(dtype=float), 1).tolist(), 'y': _nums(g['value'])})
    units = {m['name']: m['unit'] for m in ctx['metrics']}
    mroles = ctx.get('metric_roles') or {}
    ph_names, ph_tot = [], {}
    for ph in (ctx.get('phases') or {}).values():
        for n, v in ph['stages']:
            if n not in ph_names:
                ph_names.append(n)
            ph_tot[n] = ph_tot.get(n, 0.0) + v
    phase_cols = phase_mod.colors(ph_names, ph_tot)
    data = {
        'title': ctx['title'], 'date': ctx['date'], 'baseline': ctx['baseline'],
        'cases': [{'id': c['id'], 'label': c.get('label', ''),
                   'host': c.get('host') or ', '.join(sorted(df[df['case'] == c['id']]['host'].unique()))} for c in ctx['cases']],
        'roles': ctx['role_order'],
        'vms': _vms(df),
        'colors': ctx.get('colors', {}),
        # 지표 목록: 역할 지정 지표(역할 순서 → 역할 안 순서) 다음에 역할 미지정 지표(분류 순서)
        'metrics': [{'name': n, 'unit': units[n], 'category': metrics.category(n),
                     'role': mroles.get(n, ('', 0))[0], 'order': mroles.get(n, ('', 0))[1]}
                    for n in roles_mod.display_order([m['name'] for m in ctx['metrics']], mroles)],
        'roleNames': metrics.ROLES,
        'palettes': [{'name': n, 'c': c, 'auto': i not in palette_mod.NO_AUTO} for i, (n, c) in enumerate(palette_mod.PALETTES)],
        'series': series,
        # phase 소요 시간 (phase.py): 케이스별 단계 self 평균 · 구간 · Load. 색은 문서의 누적 막대그래프와 같다
        'phases': ctx.get('phases') or {},
        'jobs': ctx.get('jobs') or {},          # 잡 TAT 원장 (jobs.py): 케이스별 TAT 측정 잡 · 동시 실행 잡
        'tatBase': ctx.get('tat_base'),         # TAT 비교 기준값과 설명 (없으면 null)
        'jobColors': jobs_mod.COLORS,           # 잡 결과 그래프 색: 이전 리포트와 같은 색
        'phaseColors': phase_cols,
        'phaseText': {n: ('#2a2a2a' if c in phase_mod.REST else '#ffffff') for n, c in phase_cols.items()},
        'stats': [{k: (_num(r[k]) if k in ('mean', 'p95', 'max', 'min', 'diff', 'diff_pct') else r[k])
                   for k in ('metric', 'role', 'case', 'entity', 'host', 'n', 'mean', 'p95', 'max', 'min', 'diff', 'diff_pct')}
                  | {'n': int(r['n'])} for r in stats.to_dict('records')],
    }
    html = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    # 문자열 치환 순서: 데이터 → 제목 → plotly (plotly.js 본문에 자리표시자 문자열이 우연히 있어도 건드리지 않게 마지막에)
    html = html.replace('__DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/'))
    html = html.replace('__TITLE__', ctx['title'])
    html = html.replace('__PLOTLY__', _plotly_js().replace('</script', '<\\/script'))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    return path
