"""비교 리포트: 고른 케이스들을 지표마다 그래프 1장으로 비교한다 (주간 보고 형식).

지표마다 값과 그래프 형식을 정한다.
  - 막대: 케이스마다 막대(대상 VM 이 여러 대면 VM 마다 막대). 값 = 평균 · 최대 · 평균과 최대. 가로축 = 케이스 순서
  - 누적 막대: phase 소요 시간. 케이스마다 막대 1개에 단계를 실행 순서로 쌓는다
  - 꺾은선: 시계열. 가로축 = 경과 시간, 케이스 · VM 마다 선 1개
색: 케이스마다 색 계열(palette.py) 하나, 그 케이스의 VM 은 계열의 색을 순서대로 받는다.
대상 VM: 지표마다 '포함할 VM'(이름에 그 글자가 든 VM, 쉼표로 여러 개, 빈 값 = 전부).
기준선: 리포트마다 하나(없을 수 있다). TAT 막대에 점선과 Degradation.
"""
import os
import re
import textwrap

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm

from . import analyze, docx_base, jobs as jobs_mod, palette as P, report_case as RC, report_docx as D

TAT, PHASE, CONC = 'TAT', 'phase 소요 시간', '동시 실행 잡 elapsed'
JOB_ITEMS = (TAT, PHASE, CONC)
VALUES = {'mean': '평균', 'max': '최대', 'mean_max': '평균과 최대', 'series': '시계열'}
CHARTS = {'bar': '막대', 'stack': '누적 막대', 'line': '꺾은선'}
plt, mticker = RC.plt, RC.matplotlib.ticker


def file_title(title):
    """제목을 파일 이름으로: 파일 이름에 쓸 수 없는 글자만 바꾼다."""
    return re.sub(r'[\\/:*?"<>|]+', '_', str(title)).strip(' .') or 'compare'


def default_item(metric):
    """지표를 처음 넣을 때의 값과 그래프 형식."""
    if metric == PHASE:
        return {'metric': metric, 'value': 'mean', 'chart': 'stack', 'vms': ''}
    return {'metric': metric, 'value': 'mean', 'chart': 'bar', 'vms': ''}


def normalize(item):
    """값과 그래프 형식의 짝을 맞춘다: phase 는 누적 막대, 꺾은선은 시계열, 시계열은 꺾은선. 잡 결과 항목은 시계열이 없다."""
    it = dict(default_item(item.get('metric', '')), **item)
    if it['metric'] == PHASE:
        it['chart'], it['value'] = 'stack', 'mean'
    elif it['metric'] in (TAT, CONC):
        it['chart'], it['value'] = 'bar', 'mean'
    else:
        if it['chart'] == 'stack':
            it['chart'] = 'bar'
        if it['chart'] == 'line' or it['value'] == 'series':
            it['chart'], it['value'] = 'line', 'series'
    return it


def vm_tokens(text):
    return [t.strip().lower() for t in re.split(r'[;,]', str(text or '')) if t.strip()]


def vm_match(name, tokens):
    """포함할 VM: 낱말이 이름에 들어 있으면 맞다. 낱말이 없으면 전부."""
    n = str(name).lower()
    return not tokens or any(t.strip('*') in n for t in tokens)


def validate(spec, known_cases=None):
    """빠진 설정 목록(글). 비어 있으면 만들 수 있다."""
    out = []
    cs = spec.get('cases') or []
    if len(cs) < 2:
        out.append(f'비교할 케이스를 2건 이상 고르십시오. (지금 {len(cs)}건)')
    if known_cases is not None:
        gone = [c['id'] for c in cs if c['id'] not in set(known_cases)]
        if gone:
            out.append('케이스 정보 파일에 없는 케이스: ' + ', '.join(gone))
    if not spec.get('metrics'):
        out.append('넣을 지표를 하나 이상 고르십시오.')
    if not (spec.get('word') or spec.get('html')):
        out.append('출력(Word, HTML)을 하나 이상 체크하십시오.')
    b = spec.get('baseline')
    if b is not None:
        try:
            if float(b.get('value') or 0) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            out.append(f'리포트 기준선 {b.get("name", "")} 의 TAT 가 비어 있습니다.')
    return out


def _fmt(v):
    return f'{v:,.0f}' if abs(v) >= 1000 else f'{v:,.2f}' if abs(v) < 100 else f'{v:,.1f}'


def _axes(ax, ylabel):
    ax.grid(axis='y', color=P.GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=P.TEXT_SECONDARY, labelsize=13)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:,.0f}' if abs(v) >= 1000 else f'{v:g}'))
    ax.set_ylabel(ylabel, fontsize=14, color=P.TEXT_SECONDARY)


def _save(fig, path):
    fig.savefig(path, facecolor='white', bbox_inches='tight', pil_kwargs={'compress_level': RC.PNG_LEVEL})
    plt.close(fig)
    return path


def _short_names(names):
    """막대 아래 이름: 한 케이스 안에서 모두 같은 앞부분(호스트 이름 등)은 뺀다. 예: esx01-vm01, esx01-vm02 → vm01, vm02."""
    names = [str(x) for x in names]
    if len(names) < 2:
        return names
    pre = os.path.commonprefix(names)
    cut = max(pre.rfind('-'), pre.rfind('_'), pre.rfind('.')) + 1 or len(re.sub(r'[A-Za-z]+$', '', pre.rstrip('0123456789')))   # 구분자가 없으면 끝의 글자+숫자 묶음은 남긴다
    short = [x[cut:] for x in names]
    return short if all(short) else names


def bar_chart(path, groups, ylabel, base=None, pair=False):
    """groups: [(케이스 표시 이름, [(막대 아래 이름, 값, 색, 막대 위 덧글, 둘째 값)])]. pair 면 막대마다 평균(진한 색)과 최대(빗금)를 나란히.
    base: (값, 이름) 이면 가로 점선."""
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=RC.CHART_DPI)
    nmax = max(len(g[1]) for g in groups) or 1
    slot = 0.8 / nmax
    bw = slot * (0.42 if pair else 0.8)
    top = max([v for _, bars in groups for b in bars for v in (b[1], b[4] if pair and b[4] is not None else b[1])] + [base[0] if base else 0]) or 1.0
    xt, xl = [], []
    # 가로축 글자 겹침 방지: 막대 아래 이름(VM)은 한 칸에 들어가지 않으면 기울이고, 케이스 이름은 칸 폭에 맞춰 줄을 바꾼다
    short = [_short_names([b[0] for b in bars]) for _, bars in groups]
    slot_in = 11.5 / max(1, len(groups)) * slot                         # 막대 한 칸의 폭(인치, 그림 폭 14 in 기준)
    longest = max([len(x) for subs in short for x in subs] or [0])
    tilt = nmax > 1 and longest * 0.092 > slot_in * 0.95                # 10.5 pt 글자 1자 ≈ 0.092 in
    bw_in = 11.5 / max(1, len(groups)) * bw                             # 막대 하나의 폭(인치)
    vkw = ({'rotation': 90, 'fontsize': 10} if bw_in < 0.42 else {'fontsize': 10.5})      # 막대가 좁으면 막대 위 값을 세로로 적는다
    wrap = max(8, int(11.5 / max(1, len(groups)) / 0.115))              # 케이스 이름 한 줄 글자 수 (13 pt 1자 ≈ 0.115 in)
    for gi, (gname, bars) in enumerate(groups):
        n = len(bars)
        subs = short[gi]
        for bi, (sub, val, color, note, val2) in enumerate(bars):
            x = gi + (bi - (n - 1) / 2) * slot
            if pair:
                ax.bar(x - bw / 2, val, width=bw, color=color, zorder=3)
                ax.bar(x + bw / 2, val2, width=bw, color=color, alpha=0.45, hatch='//', edgecolor='white', zorder=3)
                ax.text(x - bw / 2, val + top * 0.012, _fmt(val), ha='center', va='bottom', color=P.TEXT_PRIMARY, **vkw)
                ax.text(x + bw / 2, val2 + top * 0.012, _fmt(val2), ha='center', va='bottom', color=P.TEXT_PRIMARY, **vkw)
            else:
                ax.bar(x, val, width=bw, color=color, zorder=3)
                ax.text(x, val + top * 0.012, _fmt(val) + (f'\n{note}' if note else ''), ha='center', va='bottom', fontsize=11.5,
                        color=P.TEXT_PRIMARY, linespacing=1.25)
            if sub and nmax > 1:
                if tilt:
                    ax.text(x + bw * 0.25, -top * 0.015, subs[bi][:18], ha='right', va='top', fontsize=10.5, color=P.TEXT_SECONDARY,
                            rotation=35, rotation_mode='anchor')
                else:
                    ax.text(x, -top * 0.02, subs[bi], ha='center', va='top', fontsize=10.5, color=P.TEXT_SECONDARY)
        xt.append(gi)
        xl.append(textwrap.fill(gname, wrap))
    if base:
        ax.axhline(base[0], color=jobs_mod.COLORS['base'], linewidth=1.6, linestyle=(0, (5, 4)), zorder=2)
        ax.annotate(f'기준선 {base[1]}\n{base[0]:,.2f}', xy=(1.01, base[0]), xycoords=('axes fraction', 'data'), ha='left', va='center',
                    fontsize=11.5, color=P.TEXT_SECONDARY, annotation_clip=False)      # 그림 오른쪽 바깥: 막대 · 값과 겹치지 않는다
    ax.set_xticks(xt)
    ax.set_xticklabels(xl, fontsize=13)
    ax.tick_params(axis='x', pad=((16 + min(longest, 18) * 3.7) if tilt else 22) if nmax > 1 else 6, length=0)     # 기울인 이름 아래에 케이스 이름
    ax.set_xlim(-0.6, len(groups) - 0.4)
    ax.set_ylim(0, top * (1.2 if not pair else (1.2 if bw_in < 0.42 else 1.13)))
    _axes(ax, ylabel)
    if pair:
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(facecolor='#777777', label='Mean'), Patch(facecolor='#777777', alpha=0.45, hatch='//', edgecolor='white', label='Max')],
                  loc='upper left', bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=12)
    return _save(fig, path)


def line_chart(path, series, ylabel, bands=None):
    """series: [(선 이름, 색, sec 값들, value 값들)]. bands: (구간들, load 끝, 전체) 면 배경에 phase 구간."""
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=RC.CHART_DPI)
    if bands:
        RC._bands(ax, *bands)
    for name, color, xs, ys in series:
        ax.plot(xs, ys, color=color, linewidth=2.0, label=name)
    ax.grid(True, color=P.GRID, linewidth=0.8)
    _axes(ax, ylabel)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
    ax.set_xlabel('Elapsed time (s)', fontsize=14, color=P.TEXT_SECONDARY)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.12), ncol=min(4, max(1, len(series))), frameon=False, fontsize=12)
    fig.tight_layout()
    return _save(fig, path)


def build(spec, cases, meta, df, outdir, date, log=print, width_cm=None, extra_stats=None):
    """비교 문서(docx)를 만든다. cases: 케이스 dict(jobs · phase · label 포함), meta: {케이스명: {start, end, ...}}, df: 전체 값 표.
    extra_stats: 이전 결과 케이스의 통계 표(summary.as_case). 이전 결과 케이스의 시계열 행은 in_run=False 라 통계에 들어가지 않는다.
    반환: (저장한 경로 또는 None, 그래프 수, 문서 글자)."""
    from .build import versioned_path
    by_id = {c['id']: c for c in cases}
    sel = [dict(c, case=by_id[c['id']]) for c in spec['cases'] if c['id'] in by_id]
    auto = [cols for k, (_, cols) in enumerate(P.PALETTES) if k not in P.NO_AUTO]      # 색 계열을 정하지 않은 케이스: 자동 배정용 계열을 순서대로
    for i, c in enumerate(sel):
        c['name'] = (c.get('name') or '').strip() or c['case'].get('display') or c['id']
        c['colors'] = P.palette_colors(P.palette_index(c['palette'])) if c.get('palette') else auto[i % len(auto)]   # 색 계열 이름 또는 1부터 센 번호
    base = spec.get('baseline')
    bval = float(base['value']) if base and float(base.get('value') or 0) > 0 else None
    bname = (base or {}).get('name') or '기준선'
    title = (spec.get('title') or '').strip() or '비교 리포트'
    chart_dir = os.path.join(outdir, 'charts', '_compare_' + RC.safe_name(title))
    os.makedirs(chart_dir, exist_ok=True)
    width = min(max(float(width_cm or RC.DEFAULT_WIDTH_CM), 8.0), RC.MAX_WIDTH_CM)
    doc = docx_base.new_document()
    RC._page_for(doc, width)
    D._style(doc)
    units = df.drop_duplicates('metric').set_index('metric')['unit'].to_dict()
    run = df[df['in_run']] if 'in_run' in df else df
    st = analyze.stats_table(run)
    if extra_stats is not None and len(extra_stats):
        st = analyze.pd.concat([st, extra_stats], ignore_index=True)
    n = {'k': 0}

    def table(t, header, rows):
        D._tcaption(doc, f'표. {t}')
        D._table(doc, header, rows)

    def picture(p, caption, notes=None):
        n['k'] += 1
        doc.add_picture(p, width=Cm(width))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        D._caption(doc, caption)
        if notes:
            D._notes(doc, notes, small=True)

    def ents_of(c, toks):
        e = sorted(set(df.loc[df['case'] == c['id'], 'entity'].astype(str)), key=RC._natural)
        return e, [x for x in e if vm_match(x, toks)]

    doc.add_heading(title, level=0)
    doc.add_paragraph(f'작성일: {date}')
    doc.add_heading('1. 비교 대상', level=1)
    table('비교 대상 케이스', ['표시 이름', '케이스명', '설명', '호스트', '잡 시작', '잡 종료', '실행 시간 (s)'],
          [[c['name'], c['case'].get('show_id') or c['id'], (c['case'].get('label') or '') + (' (이전 결과)' if c['case'].get('prior') else ''), c['case'].get('host') or '', f'{meta[c["id"]]["start"]:%Y-%m-%d %H:%M:%S}',
            f'{meta[c["id"]]["end"]:%Y-%m-%d %H:%M:%S}', f'{(meta[c["id"]]["end"] - meta[c["id"]]["start"]).total_seconds():,.0f}'] for c in sel])
    D._notes(doc, ['그래프 가로축의 케이스 순서와 색: 위 표의 순서, 케이스마다 색 계열 1개(같은 케이스의 VM 은 계열 안의 색으로 구분)',
                   '통계 기준: 잡 실행 구간(시작~종료 시각)의 수집값',
                   (f'기준선: {bname} ({base.get("kind") or "구분 없음"}) TAT {bval:,.2f} s'
                    + (f', 조건 {base["cond"]}' if base.get('cond') else '') if bval else '기준선: 지정 없음')])

    doc.add_heading('2. 지표별 비교', level=1)
    for k, raw in enumerate(spec['metrics'], 1):
        it = normalize(raw)
        m, toks = it['metric'], vm_tokens(it.get('vms'))
        who = f'포함할 VM: {it["vms"]}' if toks else '포함할 VM: 전부'
        png = os.path.join(chart_dir, f'{k:02d}.png')
        if m == TAT:
            groups, rows = [], []
            for c in sel:
                mains = [x for x in ((c['case'].get('jobs') or {}).get('mains') or []) if not toks or any(vm_match(h, toks) for h in x['hosts'])]
                bars = []
                for i, x in enumerate(mains):
                    deg = jobs_mod.degradation(x['tat'], bval)
                    host = ', '.join(x['hosts']) or x['label']
                    bars.append((host, x['tat'], c['colors'][i % len(c['colors'])], f'{deg:+.2f}%' if deg is not None else '', None))
                    rows.append([c['name'], c['case'].get('label') or '', host, f'{x["tat"]:,.2f}']
                                + ([bname, f'{bval:,.2f}', f'{deg:+.2f}'] if bval else []))
                if bars:
                    groups.append((c['name'], bars))
            D._sub(doc, 'TAT', 2)
            if not groups:
                D._notes(doc, ['고른 케이스에 TAT 측정 잡이 없음 (잡 TAT 원장 필요)', who])
                continue
            picture(bar_chart(png, groups, 'TAT (s)', (bval, bname) if bval else None), '그림. 케이스별 TAT',
                    ['막대: TAT 측정 잡의 잡 elapsed 평균' + (' · 점선: 기준선 · 막대 위 %: Degradation' if bval else ''), who])
            table('케이스별 TAT' + (' 와 기준선 대비 Degradation' if bval else ''),
                  ['케이스', '설명', 'VM', 'TAT (s)'] + (['기준선', '기준선 TAT (s)', 'Degradation (%)'] if bval else []), rows)
            if bval:
                D._notes(doc, ['Degradation = (TAT − 기준선 TAT) ÷ 기준선 TAT × 100, 양수는 기준선 대비 TAT 증가'])
        elif m == PHASE:
            items = [(c['name'], c['case']['phase']) for c in sel if c['case'].get('phase')]
            D._sub(doc, 'phase 소요 시간', 2)
            if not items:
                D._notes(doc, ['고른 케이스에 phase 기록이 없음 (phase 소요 시간 CSV 필요)'])
                continue
            picture(RC._phase_stack(png, items), '그림. 케이스별 phase self 시간 누적', ['막대 구성: 실행 순서대로 아래부터 누적 · 값: 잡 평균'])
            names = []
            for _, ph in items:
                names += [s for s, _ in ph['stages'] if s not in names]
            table('케이스별 phase self 시간 (s)', ['단계 (phase)'] + [nm for nm, _ in items],
                  [[s] + [f'{dict(ph["stages"]).get(s, 0):,.0f}' if s in dict(ph['stages']) else '' for _, ph in items] for s in names]
                  + [['전체'] + [f'{ph["total"]:,.0f}' for _, ph in items]])
        elif m == CONC:
            groups, rows = [], []
            for c in sel:
                conc = [g for g in ((c['case'].get('jobs') or {}).get('conc') or []) if not toks or vm_match(g.get('host') or '', toks)]
                bars = [(g['label'], g['mean'], c['colors'][i % len(c['colors'])], f'세트 {g["sets_in_window"]}회', None) for i, g in enumerate(conc)]
                rows += [[c['name'], g['label'], g['sets'], g['sets_in_window'], f'{g["mean"]:,.2f}', f'{g["min"]:,.2f}', f'{g["max"]:,.2f}'] for g in conc]
                if bars:
                    groups.append((c['name'], bars))
            D._sub(doc, '동시 실행 잡 elapsed', 2)
            if not groups:
                D._notes(doc, ['고른 케이스에 동시 실행 잡이 없음', who])
                continue
            picture(bar_chart(png, groups, 'Job elapsed (s)'), '그림. 케이스별 동시 실행 잡 평균 elapsed', ['막대: 잡 elapsed 평균 · 막대 위: 기준 구간 중 시작한 세트 수', who])
            table('케이스별 동시 실행 잡', ['케이스', '잡 종류', '총 세트', '기준 구간 중 시작한 세트', '잡 elapsed 평균 (s)', '최소 (s)', '최대 (s)'], rows)
        else:
            unit = units.get(m) or ''
            ylabel = m if (not unit or f'({unit})' in m) else f'{m} ({unit})'
            D._sub(doc, f'{m} · {VALUES[it["value"]]}', 2)
            if m not in set(df['metric']):
                D._notes(doc, ['고른 케이스의 데이터에 이 지표가 없음'])
                continue
            rows, miss = [], []
            if it['chart'] == 'line':
                series = []
                for c in sel:
                    allv, ents = ents_of(c, toks)
                    d = df[(df['case'] == c['id']) & (df['metric'] == m) & df['entity'].isin(ents)]
                    if d.empty:
                        miss.append(c['name'])
                        continue
                    b = RC._bin(d)
                    if b:
                        d = d.assign(sec=(d['sec'] // b) * b + b / 2).groupby(['entity', 'sec'], as_index=False)['value'].mean()
                    for e in ents:
                        s = d[d['entity'] == e].sort_values('sec')
                        if len(s):
                            series.append((f'{c["name"]} · {e}', c['colors'][allv.index(e) % len(c['colors'])], list(s['sec']), list(s['value'])))
                    for e in ents:
                        x = st[(st['case'] == c['id']) & (st['entity'] == e) & (st['metric'] == m)] if 'case' in st else st.iloc[0:0]
                        if len(x):
                            rows.append([c['name'], e, analyze.fmt(x['mean'].iloc[0]), analyze.fmt(x['p95'].iloc[0]), analyze.fmt(x['max'].iloc[0]), unit])
                if not series:
                    D._notes(doc, ['포함할 VM 에 맞는 VM 의 데이터가 없음', who])
                    continue
                bands = None
                pc = by_id.get(spec.get('phase_case') or '')
                if spec.get('phase_bg') and pc and pc.get('phase'):
                    bands = (pc['phase']['bands'], pc['phase'].get('load'), pc['phase']['total'])
                picture(line_chart(png, series, ylabel, bands), f'그림. {m} 시계열',
                        ['가로축: 각 케이스의 잡 시작 시각을 0초로 둔 경과 시간 · 선: 케이스 · VM 마다 1개',
                         (f'배경: 기준 케이스 {pc["id"]} 의 phase 구간' if bands else ''), who,
                         ('데이터 없는 케이스: ' + ', '.join(miss) if miss else '')])
                table(f'{m} 통계 (잡 실행 구간)', ['케이스', 'VM', '평균', 'p95', '최댓값', '단위'], rows)
            else:
                groups = []
                pair = it['value'] == 'mean_max'
                col = 'max' if it['value'] == 'max' else 'mean'
                for c in sel:
                    allv, ents = ents_of(c, toks)
                    bars = []
                    for e in ents:
                        x = st[(st['case'] == c['id']) & (st['entity'] == e) & (st['metric'] == m)] if 'case' in st else st.iloc[0:0]
                        if not len(x):
                            continue
                        bars.append((e, float(x[col].iloc[0]), c['colors'][allv.index(e) % len(c['colors'])], '', float(x['max'].iloc[0]) if pair else None))
                        rows.append([c['name'], e, analyze.fmt(x['mean'].iloc[0]), analyze.fmt(x['max'].iloc[0]), unit])
                    if bars:
                        groups.append((c['name'], bars))
                    else:
                        miss.append(c['name'])
                if not groups:
                    D._notes(doc, ['포함할 VM 에 맞는 VM 의 데이터가 없음', who])
                    continue
                picture(bar_chart(png, groups, ylabel, pair=pair), f'그림. 케이스별 {m} {VALUES[it["value"]]}',
                        [('막대: VM 마다 평균(진한 색)과 최대(빗금)' if pair else f'막대: VM 마다 {VALUES[it["value"]]}'), who,
                         ('데이터 없는 케이스: ' + ', '.join(miss) if miss else '')])
                table(f'케이스별 {m}', ['케이스', 'VM', '평균', '최댓값', '단위'], rows)

    path = versioned_path(os.path.join(outdir, file_title(title) + '.docx'))
    docx_base.confluence_safe(doc)
    text = docx_base.text_of(doc)
    return RC._save(doc, path, log), n['k'], text
