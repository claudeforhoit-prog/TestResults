"""반응형 HTML 리포트: 파일 하나에 데이터와 plotly.js 를 넣어 인터넷 없이 열린다."""
import json
import os
import re

import numpy as np

from . import metrics

HERE = os.path.dirname(os.path.abspath(__file__))


def _plotly_js():
    import plotly
    return open(os.path.join(os.path.dirname(plotly.__file__), 'package_data', 'plotly.min.js'), encoding='utf-8').read()


def _num(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return float(f'{v:.4g}') if abs(v) < 1e6 else round(float(v))


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
                       'x': [round(float(v), 1) for v in g['sec']], 'y': [_num(v) for v in g['value']]})
    units = {m['name']: m['unit'] for m in ctx['metrics']}
    data = {
        'title': ctx['title'], 'date': ctx['date'], 'baseline': ctx['baseline'],
        'cases': [{'id': c['id'], 'label': c.get('label', ''),
                   'host': c.get('host') or ', '.join(sorted(df[df['case'] == c['id']]['host'].unique()))} for c in ctx['cases']],
        'roles': ctx['role_order'],
        'vms': _vms(df),
        'colors': ctx.get('colors', {}),
        'metrics': [{'name': n, 'unit': units[n], 'category': c} for c, ns in metrics.ordered([m['name'] for m in ctx['metrics']]) for n in ns],
        'series': series,
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
