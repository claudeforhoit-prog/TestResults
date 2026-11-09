"""케이스별 성능 리포트(docx): 케이스 1건 = 문서 1개. 그 케이스의 실행 구간에 수집된 지표를 전부 그래프로 싣는다.

구성 (기본 양식: 지표 역할 지정이 없을 때)
  §1 케이스 식별
  §2 요약 표 (VM 별 주요 지표 평균 · 최대: 데이터에 있는 지표만)
  §3~ 분류별 절 (CPU · Memory · Guest · Disk · Network · 기타: 데이터에 있는 분류만)
       지표마다 그래프 1장(선 = VM, 최대 8개, 넘으면 나눔) + 한 줄 설명, 절 끝에 통계 표
  Appendix A 데이터 출처 (분류별 수집 시스템 · 시간대)
  Appendix B 데이터 가용성 (VM · 수집 시스템별 실행 구간 안 표본 수 · 최대 공백): 문서 끝

구성 (지표 역할 지정이 있을 때)
  §2 요약 표는 핵심 성능 지표, §3~ 는 역할별 절(핵심 성능 지표 → 원인 분석 지표 → 세부 분석 지표 → 상충 지표 →
  측정 조건 확인 항목, 지정된 역할만). 역할 미지정 지표는 옵션에 따라 Appendix A(분류별)에 수록하거나 뺀다.
  그 뒤 데이터 출처, 데이터 가용성.

그림 안에는 축 · 범례 · 데이터만 둔다(축 라벨 영문). 제목과 설명은 본문 텍스트로 쓴다. 모든 선은 실선이고 색으로 구별한다.
"""
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from docx.shared import Cm

from . import analyze, charts, docx_base, metrics, report_docx
from . import palette as P

LINES_PER_CHART = 8
CHART_DPI = 150          # 그래프 해상도: 14 x 7.5 in → 약 2,100 x 1,100 픽셀 (본문 폭 16.5 cm 에서 약 320 ppi)
PNG_LEVEL = 3            # PNG 압축 수준: 기본값 6 보다 빠르고 파일은 조금 크다
DEFAULT_WIDTH_CM = 16.5  # 문서 그래프 폭 기본값 = 세로 페이지 본문 폭 (6.5 in)
MAX_WIDTH_CM = 25.0      # 가로 방향 페이지에서 좌우 여백을 줄여 넣을 수 있는 최대 폭
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
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=CHART_DPI)
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
    fig.savefig(path, facecolor='white', bbox_inches='tight', pil_kwargs={'compress_level': PNG_LEVEL})
    plt.close(fig)
    return path


def _worker_init():
    """병렬 처리용 프로세스 준비.
    - 콘솔 없는 실행 파일(--windowed)에서는 sys.stdout · sys.stderr 가 None 이라 출력하는 순간 멈춘다: 빈 출력으로 바꾼다
      (PyInstaller 문서 'Common Issues and Pitfalls' 의 권장 방법).
    - 우선순위를 낮춰 그래프를 그리는 동안에도 창 화면이 멈추지 않게 한다."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    try:
        if sys.platform.startswith('win'):
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)    # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:
        pass


class ChartPool:
    """그래프를 여러 프로세스로 나눠 그린다(표준 multiprocessing 의 spawn 방식).
    프로세스를 만들지 못하거나, 시간 안에 답이 없거나, 도중에 깨지면 남은 그래프는 이 프로세스에서 순서대로 그린다.
    환경 변수 PERFREPORT_REQUIRE_POOL=1 이면 병렬 처리를 못 쓸 때 멈춘다(빌드 시험용)."""

    def __init__(self, workers, log=print, timeout=180):
        self.ex, self.broken, self.timeout, self.log = None, False, timeout, log
        if workers <= 1:
            return
        for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ.setdefault(k, '1')        # 프로세스마다 계산 스레드를 1개로 묶어 CPU 를 서로 빼앗지 않게 한다
        kw = {'max_workers': workers, 'mp_context': multiprocessing.get_context('spawn'), 'initializer': _worker_init}
        if sys.version_info >= (3, 11):
            kw['max_tasks_per_child'] = 200      # 오래 쓰면 늘어나는 메모리를 비우려고 프로세스를 바꿔 쓴다
        try:
            self.ex = ProcessPoolExecutor(**kw)
        except Exception as e:
            self._stop(f'프로세스를 만들지 못함: {type(e).__name__}: {e}')

    @property
    def active(self):
        return self.ex is not None and not self.broken

    def submit(self, args):
        if not self.active:
            return None
        try:
            return self.ex.submit(_chart, *args)
        except Exception as e:
            self._stop(f'그래프를 맡기지 못함: {type(e).__name__}')
            return None

    def result(self, fut, args):
        if fut is not None and self.active:
            try:
                return fut.result(timeout=self.timeout)
            except Exception as e:               # 시간 초과, 프로세스 고장 등
                self._stop(f'{type(e).__name__}: {e}'[:200])
        return _chart(*args)

    def _stop(self, why):
        if self.broken:
            return
        self.broken = True
        self.log(f'[속도] 병렬 처리를 멈추고 남은 그래프는 순서대로 그린다 ({why})')
        if self.ex is not None:
            try:
                self.ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        if os.environ.get('PERFREPORT_REQUIRE_POOL') == '1':
            raise SystemExit(f'병렬 처리를 쓰지 못함: {why}')

    def close(self):
        if self.ex is not None and not self.broken:
            self.ex.shutdown(wait=True)


def _page_for(doc, width_cm):
    """그래프 폭이 기본값(16.5 cm, 세로 페이지: 기존 리포트와 같음)보다 넓으면 가로 방향 페이지로 바꾸고,
    그래도 본문 폭이 좁으면 좌우 여백을 줄인다."""
    sec = doc.sections[0]
    if width_cm <= DEFAULT_WIDTH_CM + 0.01:
        return
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    if width_cm > (sec.page_width - sec.left_margin - sec.right_margin) / 360000:
        sec.left_margin = sec.right_margin = Cm(max(1.0, (sec.page_width / 360000 - width_cm) / 2))


def _save(doc, path, log):
    """문서 저장. 같은 이름의 파일이 Word 에서 열려 있거나 보호(DRM 등)되어 덮어쓸 수 없으면 새 이름으로 저장한다."""
    try:
        doc.save(path)
        return path
    except OSError as e:
        base, ext = os.path.splitext(path)
        alt = f'{base}_{time.strftime("%H%M%S")}{ext}'
        doc.save(alt)
        log(f'[문서] {os.path.basename(path)} 를 덮어쓰지 못해(열려 있거나 보호된 파일) 새 이름으로 저장: '
            f'{os.path.basename(alt)} ({type(e).__name__})')
        return alt


def _bin(d):
    per = d.groupby('entity')['sec'].count().max() if len(d) else 0
    if not per or per <= 400:
        return 0
    return next((b for b in charts.NICE_BINS if b >= d['sec'].max() / 400), 3600)


def _gaps(t):
    t = t.sort_values()
    return float(t.diff().dt.total_seconds().max() or 0) if len(t) > 1 else 0.0


def build_case(path, case, df, chart_dir, date, log=print, roles=None, include_unassigned=True,
               palette=None, chart_width_cm=None, pool=None):
    """case: {id, label, host, start, end}. df: 이 케이스의 행만 (case 열 = case id).
    roles: {지표: (역할, 순서)}. 이 케이스 데이터에 있는 지표로 역할이 하나라도 있으면 역할 순서로 절을 나눈다.
    include_unassigned: 역할 미지정 지표를 Appendix A 에 넣을지. 역할이 없으면 기본 양식(분류별 절).
    palette: 그래프 색상 팔레트 번호(0부터, 기본 0 = 파랑 계열). VM 은 팔레트 색을 VM 순서대로 받는다.
    chart_width_cm: 문서 안 그래프 폭. 세로 페이지 본문 폭보다 넓으면 가로 방향 페이지로 만든다.
    pool: 그래프를 나눠 그릴 프로세스 풀 (없으면 순서대로 그린다).
    반환: (저장한 경로, 그래프 수, 문서 글자)."""
    from . import roles as R
    os.makedirs(chart_dir, exist_ok=True)
    doc = docx_base.new_document()       # 디스크의 서식 파일을 쓰지 않는다 (사내 DRM 대책)
    width_cm = min(max(float(chart_width_cm or DEFAULT_WIDTH_CM), 8.0), MAX_WIDTH_CM)
    _page_for(doc, width_cm)             # 표도 이 페이지 폭에 맞춰 만들어지도록 내용보다 먼저 정한다
    report_docx._style(doc)
    cid = case['id']
    ents = sorted(df['entity'].unique(), key=_natural)
    present = list(dict.fromkeys(df['metric']))
    groups = metrics.ordered(present)
    units = df.drop_duplicates('metric').set_index('metric')['unit'].to_dict()
    systems = df.drop_duplicates('metric').set_index('metric')['source_system'].to_dict()
    st = analyze.stats_table(df)
    pal = P.palette_colors(P.DEFAULT_PALETTE if palette is None else palette)
    vm_color = {e: pal[i % len(pal)] for i, e in enumerate(ents)}       # 문서 안에서 VM 색 고정 (팔레트 색을 VM 순서대로)
    roles = R.restrict(roles or {}, present)[0]
    role_groups = [(r, R.members(roles, r)) for r in R.ROLES if R.members(roles, r)]
    unassigned = [m for m in metrics.flat(present) if m not in roles]
    by_role = bool(role_groups)
    in_doc = present if (not by_role or include_unassigned) else [m for m in present if m in roles]
    dur = (case['end'] - case['start']).total_seconds()
    mg = int(case.get('margin_s') or 0)
    state = {'n': 0}

    # 그래프는 문서를 만들기 전에 모두 맡겨 둔다 (pool 이 있으면 여러 프로세스가 나눠 그린다). 문서에는 아래 순서대로 넣는다.
    by_metric = {m: g for m, g in df.groupby('metric', sort=False)}
    run = (0, dur) if mg else None
    plan, seq = {}, 0
    for m in in_doc:
        d = by_metric.get(m)
        if d is None:
            continue
        b = _bin(d)
        got = set(d['entity'])
        have = [e for e in ents if e in got]                          # 이 지표에 데이터가 있는 VM 만 선·범례에 넣는다
        items = []
        for i in range(0, len(have), LINES_PER_CHART):
            part = have[i:i + LINES_PER_CHART]
            seq += 1
            args = (d.loc[d['entity'].isin(part), ['entity', 'sec', 'value']], part, m, units.get(m, ''),
                    os.path.join(chart_dir, f'{seq:03d}.png'), b, {e: vm_color[e] for e in part}, run)
            items.append((part, pool.submit(args) if pool is not None else None, args))
        plan[m] = (b, have, items)

    def metric_block(m):
        """지표 1개: 그래프(선 = VM, 8대씩 나눔)와 한 줄 설명."""
        b, have, items = plan[m]
        for part, fut, args in items:
            state['n'] += 1
            who = f' ({part[0]} ~ {part[-1]})' if len(items) > 1 else ''
            report_docx._caption(doc, f'그림. {m}{who}')
            doc.add_picture(pool.result(fut, args) if pool is not None else _chart(*args), width=Cm(width_cm))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        missing = [e for e in ents if e not in have]
        doc.add_paragraph(f'{m}: ' + (f'분류 {metrics.category(m)} · ' if by_role else '') + f'단위 {units.get(m) or "없음"} · 수집 시스템 {systems[m]}'
                          + (f' · 데이터 없는 VM: {", ".join(missing)}' if missing else '')
                          + (f' · 그래프는 {b}초 구간 평균' if b else ' · 그래프는 원 데이터'))

    def stats_block(title, ms):
        report_docx._caption(doc, f'표. {title} 통계')
        rows = []
        for m in ms:
            for e in ents:
                x = st[(st['entity'] == e) & (st['metric'] == m)]
                if len(x):
                    x = x.iloc[0]
                    rows.append([m, e, int(x['n']), analyze.fmt(x['mean']), analyze.fmt(x['p95']), analyze.fmt(x['max']),
                                 units.get(m) or '-'])
        report_docx._table(doc, ['지표', 'VM', '표본 수', '평균', 'p95', '최댓값', '단위'], rows)

    doc.add_heading(f'{cid} 성능 리포트', level=0)
    doc.add_paragraph(f'작성일: {date}')

    # §1 케이스 식별
    doc.add_heading('1. 케이스 식별', level=1)
    report_docx._caption(doc, '표. 케이스 정보')
    end_txt = f'{case["end"]:%Y-%m-%d %H:%M:%S}' + (f' (자동 감지: {case.get("end_reason", "")})' if case.get('end_auto') else '')
    info = [
        ['케이스명', cid], ['테스트 그룹', case.get('group') or '-'], ['설명', case.get('label') or '-'], ['호스트', case.get('host') or ', '.join(sorted(df['host'].unique()))],
        ['잡 시작 시각', f'{case["start"]:%Y-%m-%d %H:%M:%S}'], ['잡 종료 시각', end_txt],
        ['실행 시간', f'{dur:,.0f} s'],
        ['수집 점검', case.get('collection_check', '').split('] ', 1)[-1].split('\n')[0] or '-'],
        ['그래프 표시 구간', f'잡 시작 {mg // 60}분 전 ~ 종료 {mg // 60}분 후' if mg else '잡 실행 구간'], ['VM', ', '.join(ents)],
        ['수집 지표', ' · '.join(f'{c} {len(ms)}종' for c, ms in groups)],
    ]
    if by_role:
        info.append(['지표 역할', R.summary(roles) + (f', 역할 미지정 {len(unassigned)}개'
                     + (' (Appendix A 수록)' if include_unassigned else ' (문서 미수록)') if unassigned else '')])
    report_docx._table(doc, ['항목', '값'], info)
    doc.add_paragraph('그래프의 가로축은 잡 시작 시각을 0초로 둔 경과 시간(Elapsed time). 선 하나는 VM 하나이고 모두 실선이며 색으로 구별. '
                      + ('세로 점선은 잡 시작과 종료 시각이고, 그 바깥은 앞뒤 여유 구간. ' if mg else '')
                      + '표의 통계는 잡 실행 구간(시작~종료 시각)의 원 데이터 기준.')

    # §2 요약 표: 역할 지정이 있으면 핵심 성능 지표, 없으면 기존 서버별 요약 표 순서
    doc.add_heading('2. 요약', level=1)
    core = R.members(roles, R.ROLES[0])
    keys = core or [m for m in SUMMARY_PRIORITY if m in present] or metrics.flat(present)[:6]
    head = '핵심 성능 지표' if core else '주요 지표'
    report_docx._caption(doc, f'표. VM 별 {head} (평균 / 최대 · 남은 자원 지표는 평균 / 최소)')
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

    # §3~ 본문: 역할별 절(역할 지정이 있을 때) 또는 분류별 절(기본 양식)
    body = role_groups if by_role else groups
    for sec_no, (title, ms) in enumerate(body, start=3):
        doc.add_heading(f'{sec_no}. {title}', level=1)
        if by_role:
            doc.add_paragraph(f'{title}: {metrics.ROLE_DESC[title]}.')
        doc.add_paragraph(f'데이터: {", ".join(sorted({systems[m] for m in ms}))}')
        for m in ms:
            metric_block(m)
        stats_block(title if by_role else f'{title} 지표', ms)

    # Appendix: 역할 미지정 지표(옵션), 데이터 출처, 데이터 가용성(문서 끝)
    letters = iter('ABCDEF')
    if by_role and include_unassigned and unassigned:
        doc.add_heading(f'Appendix {next(letters)}. 역할 미지정 지표', level=1)
        doc.add_paragraph('역할을 지정하지 않은 지표를 분류별로 수록.')
        for cat, ms in metrics.ordered(unassigned):
            doc.add_heading(cat, level=2)
            for m in ms:
                metric_block(m)
            stats_block(f'{cat} 지표', ms)

    doc.add_heading(f'Appendix {next(letters)}. 데이터 출처', level=1)
    rows = []
    for cat, ms in metrics.ordered(in_doc):
        for sysname in sorted({systems[m] for m in ms}):
            k = sum(1 for m in ms if systems[m] == sysname)
            rows.append([cat, f'{k}종', sysname, '데이터 파일의 시각 그대로 (변환 없음)'])
    report_docx._table(doc, ['분류', '지표 수', '수집 시스템', '시간대'], rows)

    doc.add_heading(f'Appendix {next(letters)}. 데이터 가용성', level=1)
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
    text = docx_base.text_of(doc)        # 통제 용어 검사용: 저장한 파일을 다시 열지 않는다 (DRM 대책)
    return _save(doc, path, log), state['n'], text
