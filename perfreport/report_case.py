"""케이스별 성능 리포트(docx): 케이스 1건 = 문서 1개. 그 케이스의 실행 구간에 수집된 지표를 전부 그래프로 싣는다.

구성 (기본 양식: 지표 역할 지정이 없을 때)
  §1 케이스 식별
  §2 요약 표 (VM 별 주요 지표: 지표마다 평균 칸과 최대 칸, 데이터에 있는 지표만)
  §3 TAT (잡 TAT 원장이 연결되고 TAT 측정 잡이 있는 케이스만, jobs.py): 잡별 elapsed 표 · 평균 · 최대 − 최소 · 저하율(비교 기준값을 줬을 때)
  §4 phase 소요 시간 (phase 소요 시간 CSV 가 연결된 케이스만, phase.py): 표 + 단계 누적 막대그래프. TAT 가 있으면 둘의 차이를 적는다.
     이 케이스의 시계열 그래프 배경에 phase 구간을 그린다 (번갈아 음영 · 경계선 · 3% 이상 단계만 이름 · Load | Compute 주황 점선)
  §5 동시 실행 잡 (잡 TAT 원장에 동시 실행 잡이 있을 때): 잡 종류 · 호스트마다 잡 요약 · 기준 구간 대비 가동 구간 · 잡별 elapsed 그림 · 세트별 표
  없는 절은 빼고 번호를 당긴다. 그 뒤 지표 절.
  §3~ 분류별 절 (CPU · Memory · Guest · Disk · Network · 기타: 데이터에 있는 분류만)
       지표마다 그래프 1장(선 = VM, 최대 8개, 넘으면 나눔) + 한 줄 설명, 절 끝에 통계 표
  Appendix A 데이터 출처 (분류별 수집 시스템 · 시간대)
  Appendix B 지표별 표본 수 (지표 × VM, 실행 구간 안)
  Appendix C 데이터 가용성 (VM · 수집 시스템별 실행 구간 안 표본 시각 수 · 최대 공백): 문서 끝

구성 (지표 역할 지정이 있을 때)
  §2 요약 표는 핵심 성능 지표, §3~ 는 역할별 절(핵심 성능 지표 → 원인 분석 지표 → 세부 분석 지표 → 상충 지표 →
  측정 조건 확인 항목, 지정된 역할만). 역할 미지정 지표는 옵션에 따라 Appendix A(분류별)에 수록하거나 뺀다.
  그 뒤 데이터 출처, 지표별 표본 수, 데이터 가용성.

그림 안에는 축 · 범례 · 데이터만 둔다(축 라벨 영문). 제목과 설명은 본문 텍스트로 쓴다. 모든 선은 실선이고 색으로 구별한다.
컨플루언스로 가져오기: 문서 글자의 '|' 는 ' > ' 로 바꾸고(docx_base.confluence_safe), 값이 없는 칸은 비우며, 병합 칸은 쓰지 않는다.
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
from . import jobs as jobs_mod
from . import phase as phase_mod
from . import units as units_mod
from . import palette as P

LINES_PER_CHART = 8
CHART_DPI = 150          # 그래프 해상도: 14 x 7.5 in → 약 2,100 x 1,100 픽셀 (본문 폭 16.5 cm 에서 약 320 ppi)
PNG_LEVEL = 3            # PNG 압축 수준: 기본값 6 보다 빠르고 파일은 조금 크다
DEFAULT_WIDTH_CM = 16.5  # 문서 그래프 폭 기본값 = 세로 페이지 본문 폭 (6.5 in)
MAX_WIDTH_CM = 31.5      # 컨플루언스 Word 가져오기의 기본 그림 크기 제한(Word 에 보이는 크기 1200 x 900 px) 안의 최대 폭
A3_LANDSCAPE_CM = (42.0, 29.7)   # 가로 Letter 페이지(본문 최대 약 25.9 cm)보다 넓은 그래프는 A3 가로 페이지에 싣는다
SUMMARY_PRIORITY = [   # 요약 표에 넣는 지표: 기존 성능 리포트의 서버별 요약 표 순서 (데이터에 있는 것만)
    'CPU|Usage (%)', 'Super Metrics|Resource-Utilization (%)', 'CPU|Co-stop (%)', 'CPU|Ready (%)',
    'CPU|Peak vCPU Co-Stop within collection cycle (%)', 'CPU|Peak vCPU Ready within collection cycle (%)',
    'Memory|Usage (%)', units_mod.THP_COVERAGE, 'Guest|Used Memory (GB)', 'Guest|Free Memory (GB)', 'Guest|Remaining Swap Space (GB)',
    'Virtual Disk:Aggregate of all instances|Read Latency (ms)',
    'Virtual Disk:Aggregate of all instances|Write Latency (ms)']


def _natural(t):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', str(t))]


def safe_name(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', str(s)).strip('_')[:60] or 'case'


def _bands(ax, bands, load_end, total):
    """phase 배경 밴드 (이전 케이스 리포트와 같은 규칙): 단계를 번갈아 음영, 단계 시작마다 경계선,
    전체의 3% 이상인 단계만 그림 위쪽 바깥에 이름, Load | Compute 경계는 주황 점선."""
    for i, (nm, s0, s1) in enumerate(bands):
        if s1 <= s0:
            continue
        if i % 2 == 0:
            ax.axvspan(s0, s1, color=phase_mod.BAND, zorder=0)
        ax.axvline(s0, color=phase_mod.BAND_EDGE, lw=0.6, zorder=1)
    if load_end:
        ax.axvline(load_end, color=phase_mod.LOAD_COLOR, lw=1.6, ls='--', zorder=2)
        ax.text(load_end, 0.5, ' Load | Compute ', transform=ax.get_xaxis_transform(), ha='center', va='center', fontsize=12,
                color=phase_mod.LOAD_COLOR, fontweight='bold', rotation=90, bbox=dict(fc='white', ec='none', alpha=0.75, pad=1.5))
    for nm, s0, s1 in bands:
        if total and (s1 - s0) / total >= phase_mod.LABEL_MIN_SHARE:
            ax.text((s0 + s1) / 2, 1.012, nm, transform=ax.get_xaxis_transform(), ha='center', va='bottom', fontsize=12,
                    color=phase_mod.BAND_TEXT)


def _chart(d, ents, metric, unit, path, bin_s, colors=None, run=None, bands=None, load_end=None, phase_total=None):
    """VM 선 그래프 1장. d: 한 케이스·한 지표의 행. bands 를 주면 배경에 phase 구간을 그린다."""
    if bin_s:
        d = d.assign(sec=(d['sec'] // bin_s) * bin_s + bin_s / 2).groupby(['entity', 'sec'], as_index=False)['value'].mean()
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=CHART_DPI)
    if bands:
        _bands(ax, bands, load_end, phase_total)
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


def _phase_stack(path, items):
    """phase 누적 막대그래프 (이전 phase 누적 막대그래프와 같은 형식): 케이스마다 막대 1개, 아래부터 실행 순서로 단계 self 시간을 쌓는다.
    items: [(막대 이름, phase 요약)]. 막대 높이의 5% 이상인 단계는 막대 안에 값, 막대 위에 Total."""
    names = []
    for _, ph in items:
        for st_name, _ in ph['stages']:
            if st_name not in names:
                names.append(st_name)
    tot_by = {n: sum(dict(ph['stages']).get(n, 0.0) for _, ph in items) for n in names}
    cmap = phase_mod.colors(names, tot_by)
    rest = set(phase_mod.REST)
    mx = max(ph['total'] for _, ph in items) or 1.0
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=CHART_DPI)
    bottom = [0.0] * len(items)
    for n in names:
        vals = [dict(ph['stages']).get(n, 0.0) for _, ph in items]
        if max(vals) <= 0:
            continue
        ax.bar(range(len(items)), vals, bottom=bottom, width=0.5, color=cmap[n], edgecolor='white', linewidth=0.6, label=n, zorder=3)
        for i, v in enumerate(vals):
            if v >= mx * 0.05:
                ax.text(i, bottom[i] + v / 2, f'{v:,.0f}', ha='center', va='center', fontsize=12,
                        color=P.TEXT_PRIMARY if cmap[n] in rest else 'white')
        bottom = [b + v for b, v in zip(bottom, vals)]
    for i, b in enumerate(bottom):
        ax.text(i, b + mx * 0.015, f'Total {b:,.0f}s', ha='center', va='bottom', fontsize=13, fontweight='bold', color=P.TEXT_PRIMARY)
    ax.set_xticks(range(len(items)))
    ax.set_xticklabels([lbl for lbl, _ in items], fontsize=13)
    if len(items) == 1:
        ax.set_xlim(-1.0, 1.0)
    ax.set_ylim(0, mx * 1.15)
    ax.set_ylabel('Cumulative phase self time (s)', fontsize=14, color=P.TEXT_SECONDARY)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
    ax.tick_params(colors=P.TEXT_SECONDARY, labelsize=13)
    ax.grid(axis='y', color=P.GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc='upper left', bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=12,
              title='Execution order (top = last)', title_fontsize=12)
    fig.savefig(path, facecolor='white', bbox_inches='tight', pil_kwargs={'compress_level': PNG_LEVEL})
    plt.close(fig)
    return path


def _conc_chart(path, g, color, bands=None, load_end=None, phase_total=None):
    """동시 실행 잡의 잡별 elapsed (이전 리포트의 세트별 elapsed 그림과 같은 형식): 점 = 잡, 선 = 세트 평균, 가로축 = 세트 시작 경과 시간."""
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=CHART_DPI)
    if bands:
        _bands(ax, bands, load_end, phase_total)
    xs, ys = [p[0] for p in g['points']], [p[1] for p in g['points']]
    ax.plot(xs, ys, 'o', color=color, ms=6, alpha=0.45, mew=0, zorder=3, label='Job (dot)')
    ax.plot([r[0] for r in g['set_rows']], [r[2] for r in g['set_rows']], '-', color=color, lw=2.0, zorder=4, label='Set mean (line)')
    ax.set_ylim(0, max(ys) * 1.22 if ys else 1)
    ax.set_xlim(min(0.0, min(xs, default=0.0)) - g['window_s'] * 0.035, max(g['window_s'], max(xs, default=0.0)) * 1.06)
    for (x0, _n, mean, _lo, hi), js in zip(g['set_rows'], g['set_jobs']):     # 세트마다: 평균은 굵게 위에, 잡별 값은 오른쪽에
        ax.annotate(f'{mean:,.0f}', (x0, hi), xytext=(0, 9), textcoords='offset points', ha='center', va='bottom',
                    fontsize=12, fontweight='bold', color=color)
        vals = sorted((e for *_r, e in js), reverse=True)
        ax.annotate('\n'.join(f'{v:,.0f}' for v in vals), (x0, hi), xytext=(9, 0), textcoords='offset points',
                    ha='left', va='top', fontsize=9.5, color=P.TEXT_SECONDARY, linespacing=1.1)
    ax.grid(True, color=P.GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=P.TEXT_SECONDARY, labelsize=13)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f'{v:,.0f}'))
    ax.set_xlabel('Set start elapsed time (s)', fontsize=14, color=P.TEXT_SECONDARY)
    ax.set_ylabel('Job elapsed (s)', fontsize=14, color=P.TEXT_SECONDARY)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, fontsize=13)
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
    그래도 본문 폭이 좁으면 좌우 여백을 줄인다. 여백을 1 cm 로 줄여도 모자라면 A3 가로 페이지로 바꾼다."""
    sec = doc.sections[0]
    if width_cm <= DEFAULT_WIDTH_CM + 0.01:
        return
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    if width_cm > sec.page_width / 360000 - 2.0:
        sec.page_width, sec.page_height = Cm(A3_LANDSCAPE_CM[0]), Cm(A3_LANDSCAPE_CM[1])
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
               palette=None, chart_width_cm=None, pool=None, phase=None, jobs=None, tat_base=None):
    """case: {id, label, host, start, end}. df: 이 케이스의 행만 (case 열 = case id).
    roles: {지표: (역할, 순서)}. 이 케이스 데이터에 있는 지표로 역할이 하나라도 있으면 역할 순서로 절을 나눈다.
    include_unassigned: 역할 미지정 지표를 Appendix A 에 넣을지. 역할이 없으면 기본 양식(분류별 절).
    palette: 그래프 색상 팔레트 번호(0부터, 기본 0 = 파랑 계열). VM 은 팔레트 색을 VM 순서대로 받는다.
    chart_width_cm: 문서 안 그래프 폭. 세로 페이지 본문 폭보다 넓으면 가로 방향 페이지로 만든다.
    pool: 그래프를 나눠 그릴 프로세스 풀 (없으면 순서대로 그린다).
    phase: 이 케이스에 연결된 phase 요약(phase.summarize). 있으면 phase 소요 시간 절을 넣고 시계열 그래프 배경에 phase 구간을 그린다.
    jobs: 이 케이스에 연결된 잡 정리(jobs.classify). tat_base: {'value': TAT 비교 기준값(초), 'note': 설명} 또는 None.
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
    state = {'n': 0, 'order': []}      # order: 문서에 실은 지표 순서 (Appendix 표본 수 표에 쓴다)

    # 그래프는 문서를 만들기 전에 모두 맡겨 둔다 (pool 이 있으면 여러 프로세스가 나눠 그린다). 문서에는 아래 순서대로 넣는다.
    by_metric = {m: g for m, g in df.groupby('metric', sort=False)}
    run = (0, dur) if mg else None
    ph_args = (phase['bands'], phase.get('load'), phase['total']) if phase else (None, None, None)
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
                    os.path.join(chart_dir, f'{seq:03d}.png'), b, {e: vm_color[e] for e in part}, run) + ph_args
            items.append((part, pool.submit(args) if pool is not None else None, args))
        plan[m] = (b, have, items)

    def metric_block(m):
        """지표 1개: 그래프(선 = VM, 8대씩 나눔)와 한 줄 설명."""
        b, have, items = plan[m]
        state['order'].append(m)
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
                    rows.append([m, e, analyze.fmt(x['mean']), analyze.fmt(x['p95']), analyze.fmt(x['max']),
                                 units.get(m) or ''])
        report_docx._table(doc, ['지표', 'VM', '평균', 'p95', '최댓값', '단위'], rows)     # 표본 수는 Appendix 의 지표별 표본 수 표

    doc.add_heading(f'{cid} 성능 리포트', level=0)
    doc.add_paragraph(f'작성일: {date}')

    # §1 케이스 식별
    doc.add_heading('1. 케이스 식별', level=1)
    report_docx._caption(doc, '표. 케이스 정보')
    end_txt = f'{case["end"]:%Y-%m-%d %H:%M:%S}' + (f' (자동 감지: {case.get("end_reason", "")})' if case.get('end_auto') else '')
    info = [
        ['케이스명', cid], ['테스트 그룹', case.get('group') or '없음'], ['설명', case.get('label') or '없음'], ['호스트', case.get('host') or ', '.join(sorted(df['host'].unique()))],
        ['잡 시작 시각', f'{case["start"]:%Y-%m-%d %H:%M:%S}'], ['잡 종료 시각', end_txt],
        ['실행 시간', f'{dur:,.0f} s'],
        ['수집 점검', case.get('collection_check', '').split('] ', 1)[-1].split('\n')[0] or '없음'],
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
    pick = units_mod.same_metric(present)          # 단위를 GB · Gbps 로 바꿨어도 같은 지표를 찾는다
    keys = core or list(dict.fromkeys(filter(None, map(pick, SUMMARY_PRIORITY)))) or metrics.flat(present)[:6]
    head = '핵심 성능 지표' if core else '주요 지표'
    report_docx._caption(doc, f'표. VM 별 {head} (지표마다 평균과 최대, 남은 자원 지표는 평균과 최소)')
    rows = []
    for e in ents:
        r = [e]
        for m in keys:
            x = st[(st['entity'] == e) & (st['metric'] == m)]
            edge = 'min' if metrics.is_remaining(m) else 'max'
            r += [analyze.fmt(x['mean'].iloc[0]), analyze.fmt(x[edge].iloc[0])] if len(x) else ['', '']
        rows.append(r)
    # 머리글 두 줄: 윗줄에 지표 이름을 한 번만(평균 칸 위), 아랫줄에 평균 · 최대. 병합 칸은 컨플루언스로 가져올 때 깨질 수 있어 쓰지 않는다
    report_docx._table(doc, ['VM'] + [x for m in keys for x in (m, '')], rows,
                       header2=[''] + [x for m in keys for x in ('평균', '최소' if metrics.is_remaining(m) else '최대')])
    doc.add_paragraph('지표마다 평균과 최대를 각 칸에 적음. 여유 메모리 · 잔여 swap 처럼 남은 자원을 재는 지표는 최대 대신 최솟값. '
                      '값이 0 인 지표는 0, 수집되지 않은 지표는 빈 칸.')

    no = 3
    mains = (jobs or {}).get('mains') or []
    conc = (jobs or {}).get('conc') or []
    # TAT (TAT 측정 잡이 있을 때). VM 여러 대에서 돌렸으면 VM 마다 표를 따로 둔다
    if mains:
        doc.add_heading(f'{no}. TAT', level=1)
        no += 1
        doc.add_paragraph('잡 TAT 원장에서 VM 마다 케이스 잡 시작 시각에 시작해 실행 구간의 절반 이상 동안 실행된 세트를 TAT 측정 잡으로 판단. '
                          'TAT 는 그 세트 잡들의 elapsed 평균.'
                          + (f' TAT 측정 잡이 VM {len(mains)}대에 있어 VM 마다 따로 적음.' if len(mains) > 1 else ''))
        for m in mains:
            form = '개별형(1_of_1 잡을 동시에 실행)' if m['form'] == 'individual' else '일괄형(한 명령으로 여러 잡 실행)'
            nj = len(m['jobs'])
            if len(mains) > 1:
                doc.add_heading(m['label'], level=2)
            doc.add_paragraph(f'TAT 측정 잡: {m["code"]} · 호스트 {", ".join(m["hosts"]) or "기록 없음"} · {form} 잡 {nj}개.'
                              + (f' 같은 VM 의 같은 종류 다른 세트 {m["other_sets"]}개는 TAT 에 넣지 않음.' if m['other_sets'] else ''))
            report_docx._caption(doc, f'표. {m["label"]} 잡별 elapsed')
            sp = m['max'] - m['min']
            report_docx._table(doc, ['잡', '호스트', '시작', '종료', 'elapsed (s)'],
                               [[n, h, a, z, f'{e:,.2f}'] for n, h, a, z, e in m['jobs']]
                               + [[f'잡 {nj}개 평균 (TAT)', '', '', '', f'{m["tat"]:,.2f}'],
                                  ['최대 − 최소', '', '', '', f'{sp:,.2f} (평균 대비 {sp / m["tat"] * 100:.2f}%)']])
            deg = jobs_mod.degradation(m['tat'], (tat_base or {}).get('value'))
            if deg is not None:
                b = tat_base['value']
                doc.add_paragraph(f'저하율: ({m["tat"]:,.2f} − {b:,.2f}) ÷ {b:,.2f} × 100 = {deg:+.2f}% (양수 = 비교 기준보다 긺). '
                                  f'비교 기준: {tat_base.get("note") or "설명 없음"} {b:,.2f} s.')

    # phase 소요 시간 (phase 소요 시간 CSV 가 연결된 케이스만)
    if phase:
        doc.add_heading(f'{no}. phase 소요 시간', level=1)
        no += 1
        exp = phase.get('expected')
        doc.add_paragraph(f'잡 {phase["jobs"]}개의 단계별 self 시간 평균'
                          + (f' (동시 실행 {exp}잡 중 {phase["jobs"]}잡분 기록)' if exp and exp > phase['jobs'] else '')
                          + '. self 시간은 그 단계만의 소요 시간이고, 단계의 시작과 종료는 잡 시작 기준으로 self 시간을 차례로 더한 값.'
                          + (f' Load 는 {phase_mod.LOAD_STAGE} 단계 끝까지의 누적 시간({phase_mod.LOAD_STAGE}_total) 평균, Compute 는 전체에서 Load 를 뺀 값.'
                             if phase.get('load') else ''))
        report_docx._caption(doc, '표. phase 별 소요 시간')
        report_docx._table(doc, ['단계 (phase)', '시작 (s)', '종료 (s)', 'self 시간 (s)', '비율 (%)'],
                           [[n, f'{a:,.0f}', f'{z:,.0f}', f'{v:,.0f}', f'{r:.1f}' if r is not None else '']
                            for n, a, z, v, r in phase_mod.table_rows(phase)])
        state['n'] += 1
        report_docx._caption(doc, '그림. phase 별 self 시간 누적')
        doc.add_picture(_phase_stack(os.path.join(chart_dir, 'phase_stack.png'), [(cid, phase)]), width=Cm(width_cm))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        doc.add_paragraph('막대 하나는 이 케이스의 잡 평균이고, 아래부터 실행 순서로 단계 self 시간을 쌓음. '
                          '이 케이스의 시계열 그래프 배경에 phase 구간을 표시: 단계를 번갈아 음영, 단계 경계선, 전체의 3% 이상인 단계만 이름'
                          + (', 주황 점선은 Load 와 Compute 의 경계.' if phase.get('load') else '.'))
        main = next((m for m in mains if set(m['hosts']) & set(phase.get('hosts') or [])), mains[0] if mains else None)
        if main:
            diff = phase['total'] - main['tat']
            doc.add_paragraph(f'phase 합계와 TAT 비교: phase 합계 {phase["total"]:,.2f} s, TAT {main["tat"]:,.2f} s, 차이 {diff:+,.2f} s '
                              f'(phase 소요 시간 CSV 의 단계 합계 잡 {phase["jobs"]}개 평균, 잡 TAT 원장 {main["label"]} 의 elapsed 잡 {len(main["jobs"])}개 평균).'
                              + (' 두 기록의 잡 수가 다름.' if phase['jobs'] != len(main['jobs']) else ''))

    # 동시 실행 잡 (잡 TAT 원장에 TAT 측정 잡이 아닌 잡이 있을 때)
    if conc:
        doc.add_heading(f'{no}. 동시 실행 잡', level=1)
        no += 1
        ref = 'TAT 측정 잡 실행 구간' if mains else '케이스 실행 구간'
        doc.add_paragraph(f'세트는 같은 잡 종류 · 같은 호스트에서 시작 시각이 10초 안인 잡 묶음. 기준 구간은 {ref} '
                          f'({jobs["window"][0]} ~ {jobs["window"][1]}). 기준 구간 중 시작한 세트는 기준 구간 시작 10초 전에 시작한 세트부터 '
                          '기준 구간 끝까지 시작한 세트이고, 끝난 뒤 시작한 세트는 넣지 않음.')
        for g in conc:
            doc.add_heading(g['label'], level=2)
            report_docx._caption(doc, f'표. {g["label"]} 잡 요약')
            report_docx._table(doc, ['항목', '값'], [
                [f'{ref} 중 시작한 세트', f'{g["sets_in_window"]}회'],
                ['총 세트 / 총 잡', f'{g["sets"]}세트 / {g["jobs"]}잡 (세트당 {g["jobs_per_set"]:g}잡)'],
                ['잡 elapsed 평균 / 최소 / 최대', f'{g["mean"]:,.2f} / {g["min"]:,.2f} / {g["max"]:,.2f} s'],
                ['첫 세트 시작 / 마지막 세트 종료', f'{g["first_start"]} / {g["last_end"]}']])
            report_docx._caption(doc, f'표. {g["label"]} 의 {ref} 대비 가동 구간')
            report_docx._table(doc, ['항목', '값'], [
                ['기준 구간', f'{g["window_s"]:,.0f} s'],
                ['가동 구간 (기준 구간 안, 세트 간 겹침 제거)', f'{g["busy_s"]:,.0f} s (기준 구간의 {g["coverage_pct"]:.1f}%)'],
                ['기준 구간 밖 가동 시간', f'{g["outside_s"]:,.0f} s'],
                ['세트 간 겹침으로 중복 제거한 시간', f'{g["overlap_s"]:,.0f} s'],
                ['최대 공백 (기준 구간 시작 전 · 끝난 뒤 포함)', f'{g["max_gap_s"]:,.0f} s'],
                ['첫 세트 시작 전 / 마지막 세트 종료 후 공백', f'{g["head_gap_s"]:,.0f} s / {g["tail_gap_s"]:,.0f} s']])
            state['n'] += 1
            report_docx._caption(doc, f'그림. {g["label"]} 잡별 elapsed (세트 시작 시각 기준)')
            gpath = os.path.join(chart_dir, f'jobs_{state["n"]:03d}.png')
            doc.add_picture(_conc_chart(gpath, g, jobs_mod.COLORS['dot'], *ph_args), width=Cm(width_cm))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            doc.add_paragraph('점 하나는 잡 하나, 선은 세트 평균. 세트마다 굵은 숫자는 세트 평균, 그 오른쪽 숫자는 잡마다의 elapsed. '
                              '가로축은 기준 구간 시작을 0초로 둔 세트 시작 경과 시간' + (', 배경은 phase 구간.' if phase else '.'))
            report_docx._caption(doc, f'표. {g["label"]} 세트별 실측')
            report_docx._table(doc, ['세트', '세트 시작 (s)', '잡 수', '잡 elapsed 평균 (s)', '최소 (s)', '최대 (s)'],
                               [[i, f'{a:,.0f}', n, f'{m:,.2f}', f'{lo:,.2f}', f'{hi:,.2f}']
                                for i, (a, n, m, lo, hi) in enumerate(g['set_rows'], 1)])
            report_docx._caption(doc, f'표. {g["label"]} 잡별 elapsed')
            report_docx._table(doc, ['세트', '잡', '시작', '종료', 'elapsed (s)'],
                               [[i, n, a, z, f'{e:,.2f}'] for i, js in enumerate(g['set_jobs'], 1) for n, a, z, e in js])

    # 본문: 역할별 절(역할 지정이 있을 때) 또는 분류별 절(기본 양식). 번호는 TAT · phase · 동시 실행 잡 절 다음부터
    body = role_groups if by_role else groups
    for sec_no, (title, ms) in enumerate(body, start=no):
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

    doc.add_heading(f'Appendix {next(letters)}. 지표별 표본 수', level=1)
    doc.add_paragraph('잡 실행 구간(시작~종료 시각) 안의 지표별 · VM 별 표본 수. 같은 지표에서 VM 끼리 수가 다르면 그 VM 의 수집이 빠졌거나 수집 간격이 다르다는 뜻. '
                      '수집되지 않은 칸은 빈 칸.')
    nmap = {(r.metric, r.entity): int(r.n) for r in st[['metric', 'entity', 'n']].itertuples()}
    rows = [[m] + [f'{nmap[(m, e)]:,}' if (m, e) in nmap else '' for e in ents] for m in dict.fromkeys(state['order'])]
    report_docx._table(doc, ['지표'] + ents, rows)

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
                             f'{x["sec"] / 60:.1f}', f'{x["interval"]:g}' if x['interval'] else ''] for x in iss])
    else:
        doc.add_paragraph('수집 끊김 없음.')

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    docx_base.confluence_safe(doc)       # 컨플루언스 Word 가져오기에서 표가 깨지지 않게 '|' 등을 바꾼다
    text = docx_base.text_of(doc)        # 통제 용어 검사용: 저장한 파일을 다시 열지 않는다 (DRM 대책)
    return _save(doc, path, log), state['n'], text
