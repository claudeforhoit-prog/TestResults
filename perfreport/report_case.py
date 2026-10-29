"""케이스별 성능 리포트(docx): 케이스 1건 = 문서 1개. 그 케이스의 실행 구간에 수집된 지표를 전부 그래프로 싣는다.

구성
  §1 케이스 식별
  §2 요약 표 (VM 별 주요 지표 평균 · 최대: 데이터에 있는 지표만)
  §3~ 분류별 절 (CPU · Memory · Guest · Disk · Network · 기타: 데이터에 있는 분류만)
       지표마다 그래프 1장(선 = VM, 최대 8개, 넘으면 나눔) + 한 줄 설명, 절 끝에 통계 표
  Appendix A 데이터 출처 (분류별 수집 시스템 · 시간대)
  Appendix B 데이터 가용성 (VM · 수집 시스템별 실행 구간 안 표본 수 · 최대 공백): 문서 끝

그림 안에는 축 · 범례 · 데이터만 둔다(축 라벨 영문). 제목과 설명은 본문 텍스트로 쓴다. 모든 선은 실선이고 색으로 구별한다.
"""
import os
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches

from . import analyze, charts, metrics, report_docx
from . import palette as P

LINES_PER_CHART = 8
SUMMARY_PRIORITY = [   # 요약 표에 넣는 지표: 기존 성능 리포트의 서버별 요약 표 순서 (데이터에 있는 것만)
    'CPU|Usage (%)', 'Super Metrics|Resource-Utilization (%)', 'CPU|Co-stop (%)', 'CPU|Ready (%)',
    'CPU|Peak vCPU Co-Stop within collection cycle (%)', 'CPU|Peak vCPU Ready within collection cycle (%)',
    'Memory|Usage (%)', 'Guest|Used Memory (GB)', 'Guest|Free Memory (GB)', 'Guest|Remaining Swap Space (GB)',
    'Virtual Disk:Aggregate of all instances|Read Latency (ms)',
    'Virtual Disk:Aggregate of all instances|Write Latency (ms)']


def _natural(t):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', str(t))]


def safe_name(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', str(s)).strip('_')[:60] or 'case'


def _chart(d, ents, metric, unit, path, bin_s, colors=None, run=None):
    """VM 선 그래프 1장. d: 한 케이스·한 지표의 행."""
    if bin_s:
        d = d.assign(sec=(d['sec'] // bin_s) * bin_s + bin_s / 2).groupby(['entity', 'sec'], as_index=False)['value'].mean()
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=200)
    for i, e in enumerate(ents):
        s = d[d['entity'] == e].sort_values('sec')
        ax.plot(s['sec'], s['value'], color=(colors or {}).get(e, P.TC_COLORS_LIGHT[i % 8]), linewidth=2.0, label=e)
    if run:                                     # 잡 시작(0초)과 종료 시각: 앞뒤 여유 구간과 구분하는 세로 점선
        for x in run:
            ax.axvline(x, color='#77766f', linewidth=1.2, linestyle=(0, (4, 3)))
    ax.grid(True, color=P.GRID, linewidth=0.8)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=P.TEXT_SECONDARY, labelsize=13)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}' if abs(v) >= 1000 else f'{v:g}'))
    ax.set_xlabel('Elapsed time (s)', fontsize=14, color=P.TEXT_SECONDARY)
    ax.set_ylabel(f'{metric}' + (f'' if f'({unit})' in metric or not unit else f' ({unit})'), fontsize=14, color=P.TEXT_SECONDARY)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.12), ncol=min(4, len(ents)), frameon=False, fontsize=13)
    fig.tight_layout()
    fig.savefig(path, facecolor='white', bbox_inches='tight')
    plt.close(fig)
    return path


def _bin(d):
    per = d.groupby('entity')['sec'].count().max() if len(d) else 0
    if not per or per <= 400:
        return 0
    return next((b for b in charts.NICE_BINS if b >= d['sec'].max() / 400), 3600)


def _gaps(t):
    t = t.sort_values()
    return float(t.diff().dt.total_seconds().max() or 0) if len(t) > 1 else 0.0


def build_case(path, case, df, chart_dir, date, log=print):
    """case: {id, label, host, start, end}. df: 이 케이스의 행만 (case 열 = case id)."""
    os.makedirs(chart_dir, exist_ok=True)
    doc = Document()
    report_docx._style(doc)
    cid = case['id']
    ents = sorted(df['entity'].unique(), key=_natural)
    present = list(dict.fromkeys(df['metric']))
    groups = metrics.ordered(present)
    units = df.drop_duplicates('metric').set_index('metric')['unit'].to_dict()
    systems = df.drop_duplicates('metric').set_index('metric')['source_system'].to_dict()
    st = analyze.stats_table(df)
    vm_color = {e: P.TC_COLORS_LIGHT[i % 8] for i, e in enumerate(ents)}   # 문서 안에서 VM 색 고정

    doc.add_heading(f'{cid} 성능 리포트', level=0)
    doc.add_paragraph(f'작성일: {date}')

    # §1 케이스 식별
    doc.add_heading('1. 케이스 식별', level=1)
    report_docx._caption(doc, '표. 케이스 정보')
    dur = (case['end'] - case['start']).total_seconds()
    end_txt = f'{case["end"]:%Y-%m-%d %H:%M:%S}' + (f' (자동 감지: {case.get("end_reason", "")})' if case.get('end_auto') else '')
    mg = int(case.get('margin_s') or 0)
    report_docx._table(doc, ['항목', '값'], [
        ['케이스명', cid], ['테스트 그룹', case.get('group') or '-'], ['설명', case.get('label') or '-'], ['호스트', case.get('host') or ', '.join(sorted(df['host'].unique()))],
        ['잡 시작 시각', f'{case["start"]:%Y-%m-%d %H:%M:%S}'], ['잡 종료 시각', end_txt],
        ['실행 시간', f'{dur:,.0f} s'],
        ['수집 점검', case.get('collection_check', '').split('] ', 1)[-1].split('\n')[0] or '-'],
        ['그래프 표시 구간', f'잡 시작 {mg // 60}분 전 ~ 종료 {mg // 60}분 후' if mg else '잡 실행 구간'], ['VM', ', '.join(ents)],
        ['수집 지표', ' · '.join(f'{c} {len(ms)}종' for c, ms in groups)],
    ])
    doc.add_paragraph('그래프의 가로축은 잡 시작 시각을 0초로 둔 경과 시간(Elapsed time). 선 하나는 VM 하나이고 모두 실선이며 색으로 구별. '
                      + ('세로 점선은 잡 시작과 종료 시각이고, 그 바깥은 앞뒤 여유 구간. ' if mg else '')
                      + '표의 통계는 잡 실행 구간(시작~종료 시각)의 원 데이터 기준.')

    # §2 요약 표
    doc.add_heading('2. 요약', level=1)
    keys = [m for m in SUMMARY_PRIORITY if m in present] or metrics.flat(present)[:6]
    report_docx._caption(doc, '표. VM 별 주요 지표 (평균 / 최대 · 남은 자원 지표는 평균 / 최소)')
    rows = []
    for e in ents:
        r = [e]
        for m in keys:
            x = st[(st['entity'] == e) & (st['metric'] == m)]
            edge = 'min' if metrics.is_remaining(m) else 'max'
            r.append(f'{analyze.fmt(x["mean"].iloc[0])} / {analyze.fmt(x[edge].iloc[0])}' if len(x) else '-')
        rows.append(r)
    report_docx._table(doc, ['VM'] + [f'{m} ({"평균/최소" if metrics.is_remaining(m) else "평균/최대"})' for m in keys], rows)
    doc.add_paragraph('여유 메모리 · 잔여 swap 처럼 남은 자원을 재는 지표는 최솟값을 적는다. 값이 0 인 지표는 0 으로, 수집되지 않은 지표는 - 로 적는다.')

    # §3~ 분류별 절
    n = 0
    for sec_no, (cat, ms) in enumerate(groups, start=3):
        doc.add_heading(f'{sec_no}. {cat}', level=1)
        doc.add_paragraph(f'데이터: {", ".join(sorted({systems[m] for m in ms}))}')
        for m in ms:
            d = df[df['metric'] == m]
            b = _bin(d)
            have = [e for e in ents if e in set(d['entity'])]            # 이 지표에 데이터가 있는 VM 만 선·범례에 넣는다
            parts = [have[i:i + LINES_PER_CHART] for i in range(0, len(have), LINES_PER_CHART)]
            for k, part in enumerate(parts):
                n += 1
                who = f' ({part[0]} ~ {part[-1]})' if len(parts) > 1 else ''
                report_docx._caption(doc, f'그림. {m}{who}')
                png = _chart(d[d['entity'].isin(part)], part, m, units.get(m, ''), os.path.join(chart_dir, f'{n:03d}.png'), b, vm_color,
                             (0, dur) if mg else None)
                doc.add_picture(png, width=Inches(6.5))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            missing = [e for e in ents if e not in have]
            doc.add_paragraph(f'{m}: 단위 {units.get(m) or "없음"} · 수집 시스템 {systems[m]}'
                              + (f' · 데이터 없는 VM: {", ".join(missing)}' if missing else '')
                              + (f' · 그래프는 {b}초 구간 평균' if b else ' · 그래프는 원 데이터'))
        report_docx._caption(doc, f'표. {cat} 지표 통계')
        rows = []
        for m in ms:
            for e in ents:
                x = st[(st['entity'] == e) & (st['metric'] == m)]
                if len(x):
                    x = x.iloc[0]
                    rows.append([m, e, int(x['n']), analyze.fmt(x['mean']), analyze.fmt(x['p95']), analyze.fmt(x['max']),
                                 units.get(m) or '-'])
        report_docx._table(doc, ['지표', 'VM', '표본 수', '평균', 'p95', '최댓값', '단위'], rows)

    # Appendix A 데이터 출처
    doc.add_heading('Appendix A. 데이터 출처', level=1)
    rows = []
    for cat, ms in groups:
        for sysname in sorted({systems[m] for m in ms}):
            k = sum(1 for m in ms if systems[m] == sysname)
            rows.append([cat, f'{k}종', sysname, '데이터 파일의 시각 그대로 (변환 없음)'])
    report_docx._table(doc, ['분류', '지표 수', '수집 시스템', '시간대'], rows)

    # Appendix B 데이터 가용성
    doc.add_heading('Appendix B. 데이터 가용성', level=1)
    rows = []
    run_df = df[df['in_run']] if 'in_run' in df else df
    for (e, sysname), g in run_df.groupby(['entity', 'source_system']):
        t = g.drop_duplicates('t')['t']
        rows.append([e, sysname, f'{len(t):,}', f'{t.min():%H:%M:%S}', f'{t.max():%H:%M:%S}', f'{_gaps(t):,.0f}'])
    rows.sort(key=lambda r: (_natural(r[0]), r[1]))
    report_docx._table(doc, ['VM', '수집 시스템', '실행 구간 안 표본 시각 수', '첫 표본', '마지막 표본', '최대 공백 (s)'], rows)
    iss = case.get('collection_issues') or []
    report_docx._caption(doc, '표. 수집 끊김 점검 (실행 구간 안, 정상 표본 간격의 2.5배 또는 간격+60초를 넘는 공백)')
    if iss:
        report_docx._table(doc, ['VM', '수집 시스템', '종류', '시작', '끝', '길이 (분)', '정상 간격 (s)'],
                           [[x['vm'], x['system'], x['kind'], f'{x["from"]:%Y-%m-%d %H:%M:%S}', f'{x["to"]:%Y-%m-%d %H:%M:%S}',
                             f'{x["sec"] / 60:.1f}', f'{x["interval"]:g}' if x['interval'] else '-'] for x in iss])
    else:
        doc.add_paragraph('수집 끊김 없음.')

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    doc.save(path)
    return path, n
