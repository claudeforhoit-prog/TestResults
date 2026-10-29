"""docx 용 정적 차트 (matplotlib). 지표 1개 = 그림 1장, VM 역할별로 위아래 패널을 나눈다.

이미지 안에는 관측값·축·범례만 그린다. 제목과 설명은 문서 본문 텍스트로 쓴다.
판형 8.5in × 패널 수에 비례 · 200dpi (워드 50% 축소 삽입에서도 글자가 읽히는 크기).
"""
import os
import platform

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.lines
import matplotlib.ticker
from matplotlib import font_manager

from . import palette as P

_FONT_CANDIDATES = ['Malgun Gothic', 'AppleSDGothicNeo', 'Apple SD Gothic Neo', 'NanumGothic', 'Noto Sans CJK KR']


def _set_font():
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in _FONT_CANDIDATES:
        if name in have:
            plt.rcParams['font.family'] = name
            break
    if platform.system() == 'Darwin' and plt.rcParams['font.family'] == ['sans-serif']:
        p = '/System/Library/Fonts/AppleSDGothicNeo.ttc'
        if os.path.exists(p):
            font_manager.fontManager.addfont(p)
            plt.rcParams['font.family'] = font_manager.FontProperties(fname=p).get_name()
    plt.rcParams['axes.unicode_minus'] = False


_set_font()


NICE_BINS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600]


def bin_seconds(df, max_points=300):
    """그래프 1개 선의 점이 max_points 를 넘지 않게 하는 평균 구간 길이(초). 원 데이터 간격보다 작으면 원 데이터 그대로(0)."""
    span = df['sec'].max() if len(df) else 0
    per = df.groupby(['case', 'entity'])['sec'].count().max() if len(df) else 0
    if not per or per <= max_points:
        return 0
    need = span / max_points
    return next((b for b in NICE_BINS if b >= need), NICE_BINS[-1])


CASES_PER_FIGURE = 4


def _natural(t):
    import re
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', str(t))]


def vm_styles(df, colors=None):
    """VM 별 (색, 선 모양). 리포트 안의 VM 전체를 호스트 → VM 이름 순서로 늘어놓고 고정 팔레트를 배정한다.
    8대를 넘으면 선 모양을 바꿔 조합한다. HTML 뷰어와 같은 규칙이다."""
    colors = colors or {}
    pairs = sorted(df[['host', 'entity']].drop_duplicates().itertuples(index=False), key=lambda r: (_natural(r[0]), _natural(r[1])))
    out = {}
    for i, (host, e) in enumerate(pairs):
        ls = ['-', (0, (6, 3)), (0, (1.5, 2)), (0, (6, 2, 1.5, 2))][(i // 8) % 4]
        out[(host, e)] = (colors.get('vm:' + e, P.TC_COLORS_LIGHT[i % 8]), ls)
    return out


def metric_chart(df, metric, case_order, role_order, path, bin_s=0, colors=None):
    """지표 1개의 그림 파일 목록 [(경로, 케이스 목록)]. 케이스 하나 = 패널 하나, 선 = 그 케이스 호스트의 VM.
    케이스가 4개를 넘으면 4개씩 나눠 여러 장으로 저장한다."""
    d = df[df['metric'] == metric]
    styles = vm_styles(df, colors)
    if bin_s:
        d = d.assign(sec=(d['sec'] // bin_s) * bin_s + bin_s / 2)
        d = d.groupby(['case', 'host', 'entity', 'sec'], as_index=False)['value'].mean().assign(unit=d['unit'].iloc[0])
    cases = [c for c in case_order if c in set(d['case'])] or ['-']
    unit = d['unit'].iloc[0] if len(d) else ''
    nfig = -(-len(cases) // CASES_PER_FIGURE)
    size = -(-len(cases) // nfig)
    base, ext = os.path.splitext(path)
    out = []
    for k in range(nfig):
        part = cases[k * size:(k + 1) * size]
        p = path if nfig == 1 else f'{base}_{k + 1}{ext}'
        _figure(d, part, unit, styles, p)
        out.append((p, part))
    return out


def _figure(d, cases, unit, styles, path):
    n = len(cases)
    fig, axes = plt.subplots(n, 1, figsize=(8.5, 0.6 + 2.5 * n), dpi=200, sharex=True, squeeze=False)
    for i, case in enumerate(cases):
        ax = axes[i][0]
        g = d[d['case'] == case]
        for (host, e), s in sorted(g.groupby(['host', 'entity']), key=lambda x: _natural(x[0][1])):
            s = s.sort_values('sec')
            col, ls = styles.get((host, e), ('#777777', '-'))
            ax.plot(s['sec'], s['value'], color=col, linestyle=ls, linewidth=1.2, label=e)
        hosts = ', '.join(sorted(g['host'].unique()))
        ax.set_ylabel(f'{case}\n{hosts}' + (f'\n({unit})' if unit else ''), color=P.TEXT_SECONDARY, fontsize=8.5)
        ax.grid(True, color=P.GRID, linewidth=0.6)
        for side in ('top', 'right'):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=P.TEXT_SECONDARY, labelsize=8)
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}' if abs(v) >= 1000 else f'{v:g}'))
        nl = len(ax.get_lines())
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=7.5,
                  ncol=1 if nl <= 10 else 2, handlelength=2.2)
    axes[-1][0].set_xlabel('경과 시간 (s, 잡 시작 기준)', color=P.TEXT_SECONDARY, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor='white', bbox_inches='tight')
    plt.close(fig)
    return path
