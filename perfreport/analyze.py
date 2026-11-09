"""통계와 기준 케이스 대비 비교. 모든 수치는 여기서만 계산한다 (LLM 은 계산하지 않는다)."""
import pandas as pd

STATS = ['n', 'mean', 'p95', 'max', 'min']
STAT_LABEL = {'n': '표본 수', 'mean': '평균', 'p95': 'p95', 'max': '최댓값', 'min': '최솟값'}


def stats_table(df):
    if 'in_run' in df:
        df = df[df['in_run']]                       # 통계는 잡 실행 구간만 (앞뒤 여유 구간 제외)
    keys = ['metric', 'unit', 'role', 'case', 'host', 'entity']
    g = df.groupby(keys)['value']
    out = g.agg(n='count', mean='mean', max='max', min='min')
    out['p95'] = g.quantile(0.95)                   # 그룹마다 함수를 부르지 않는 묶음 계산 (결과는 같다)
    return out.reset_index()


def compare(stats, baseline, stat='mean'):
    """같은 지표·역할에서 기준 케이스 대비 차이. 기준값이 0 이면 비율을 계산하지 않는다."""
    base = stats[stats['case'] == baseline][['metric', 'role', stat]].rename(columns={stat: 'base'})
    m = stats.merge(base, on=['metric', 'role'], how='left')
    m['diff'] = m[stat] - m['base']
    m['diff_pct'] = m.apply(lambda r: (r['diff'] / r['base'] * 100) if r['base'] not in (0, None) and pd.notna(r['base']) else None, axis=1)
    return m


def fmt(v, digits=2):
    if v is None or pd.isna(v):
        return '-'
    if abs(v) >= 1000:
        return f'{v:,.0f}'
    if v != 0 and abs(v) < 1:
        digits = 3 if abs(v) >= 0.01 else 4      # 작은 값은 자릿수를 늘려 0.00 으로 뭉개지지 않게 한다
    return f'{v:.{digits}f}'


def template_sentences(cmp, metric, baseline, stat='mean'):
    """LLM 없이 쓰는 해석 문장 (명사형 종결). 수치는 cmp 의 값만 쓴다."""
    rows = cmp[(cmp['metric'] == metric)].sort_values(['role', 'case'])
    out = []
    for role, g in rows.groupby('role', sort=False):
        unit = g['unit'].iloc[0]
        b = g[g['case'] == baseline]
        if b.empty:
            continue
        bv = b[stat].iloc[0]
        for _, r in g[g['case'] != baseline].iterrows():
            word = '증가' if r['diff'] > 0 else ('감소' if r['diff'] < 0 else '변화 없음')
            if word == '변화 없음':
                change = word
            elif unit == '%':
                change = f'{fmt(abs(r["diff"]))}%p {word}'      # 백분율 지표는 %p 차이로 쓴다
            elif r['diff_pct'] is None or pd.isna(r['diff_pct']):
                change = f'{fmt(abs(r["diff"]))} {unit} {word}'.replace('  ', ' ')
            else:
                change = f'{fmt(abs(r["diff_pct"]), 1)}% {word}'
            sep = '' if unit == '%' else ' '
            out.append(f'{role}: {STAT_LABEL[stat]} {fmt(bv)} → {fmt(r[stat])}{sep}{unit}'.rstrip()
                       + f' ({baseline} 대비 {r["case"]} {change}).')
    return out
