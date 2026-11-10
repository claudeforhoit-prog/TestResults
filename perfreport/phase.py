"""phase 소요 시간: 잡 단계(phase)별 소요 시간 CSV 를 읽어 케이스에 붙인다.

입력 CSV (단계 추출 스크립트의 파일 출력, 한 행 = 잡 1개). 데이터 폴더에 함께 넣으면 머리글을 보고 찾는다.
  공통 열: Hostname, Workload, JobStart_KST (예: 2026-07-15T02-05-02), JobIdx, JobTotal, Stage_Count
  단계 열: <단계>_self (그 단계만의 소요 초), <단계>_total (잡 시작부터 그 단계 끝까지 누적 초).
           단계 순서 = 열 순서. 값 -1 은 그 단계가 기록되지 않았다는 뜻이다.
  끝 열: TotalElapsed_Sec, SrcFile

케이스 연결
  - JobStart_KST 가 케이스의 잡 시작 시각 앞뒤 10분 안인 잡을 그 케이스의 잡으로 본다.
  - Hostname 이 케이스의 VM 이름과 맞는 잡이 있으면 그 잡만 쓴다. 이름이 맞지 않고 같은 시각에 시작한 케이스가 둘 이상이면
    어느 케이스인지 정할 수 없어 쓰지 않는다.
  - 같은 잡이 여러 파일에 들어 있으면 한 번만 센다.

계산 (이전 케이스 리포트와 같은 기준)
  - 단계 값 = 연결된 잡들의 self 시간 평균 (동시 실행 잡 평균). 기록이 없는 단계(-1 만 있음)는 뺀다.
  - 단계 구간 = 잡 시작 기준 self 시간 누적. 전체 = self 시간 합.
  - Load = init_total 평균 (init 단계가 있을 때), Compute = 전체 - Load.
"""
import csv
import fnmatch
import io
import re
import statistics

import pandas as pd

MATCH_S = 600
_STAGE = re.compile(r'^([A-Za-z][A-Za-z0-9]*)_(self|total)$')
_TIME_FORMATS = ('%Y-%m-%dT%H-%M-%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d_%H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                 '%Y-%m-%d_%H-%M-%S', '%Y-%m-%d %H-%M-%S')
LOAD_STAGE = 'init'

# 그래프 색: 이전 phase 누적 막대그래프와 같은 색. 주요 단계는 색, 나머지는 회색 계열 (실행 순서대로 돌려 쓴다)
BIG = {'elaboration': '#5B9E4D', 'reduction': '#9CC490', 'parsing': '#4E8C8A', 'transient': '#4A87A8', 'dcinit': '#8FB6CC'}
BIG_ORDER = ['#4A87A8', '#5B9E4D', '#8FB6CC', '#9CC490', '#4E8C8A']     # 단계 이름이 위와 다르면 self 시간이 긴 5단계에 이 순서로
REST = ['#B9C2C8', '#A6ADB4', '#8C9BA5', '#C3CCD2', '#9FA9B0', '#B0B8BE', '#97A1A8', '#C9D0D5', '#AEB6BC', '#BFC7CC']
BAND, BAND_EDGE, BAND_TEXT, LOAD_COLOR = '#F1F4F6', '#C8D2D8', '#6E7880', '#A05010'
LABEL_MIN_SHARE = 0.03      # 배경 밴드 라벨은 전체의 3% 이상인 단계에만 (이전 리포트 규칙)


def _header(path):
    try:
        with io.open(path, encoding='utf-8-sig', errors='replace') as f:
            first = f.readline()
    except OSError:
        return []
    return next(csv.reader([first])) if first else []


def is_phase_csv(path):
    """phase 소요 시간 CSV 인지: JobStart_KST 열과 <단계>_self 열이 셋 이상 있어야 한다."""
    if not path.lower().endswith('.csv'):
        return False
    h = [c.strip() for c in _header(path)]
    return 'JobStart_KST' in h and sum(1 for c in h if c.endswith('_self') and _STAGE.match(c)) >= 3


def _when(v):
    s = str(v or '').strip()
    for f in _TIME_FORMATS:
        try:
            return pd.Timestamp(pd.to_datetime(s, format=f))
        except (ValueError, TypeError):
            continue
    return None


def _num(v):
    try:
        x = float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None          # -1 = 기록 없음


def read_jobs(path):
    """CSV 1개 → 잡 목록 [{host, start, idx, total_jobs, order, self, total, file}]."""
    with io.open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    cols = [c.strip() for c in rows[0].keys() if c]
    order = []
    for c in cols:
        m = _STAGE.match(c)
        if m and m.group(2) == 'self' and m.group(1) not in order:
            order.append(m.group(1))
    jobs = []
    for r in rows:
        r = {(k or '').strip(): v for k, v in r.items()}
        jobs.append({'row': r, 'host': str(r.get('Hostname') or '').strip(), 'start': _when(r.get('JobStart_KST')),
                     'idx': str(r.get('JobIdx') or '').strip(), 'total_jobs': _num(r.get('JobTotal')),
                     'order': order, 'self': {s: _num(r.get(f'{s}_self')) for s in order},
                     'total': {s: _num(r.get(f'{s}_total')) for s in order}, 'file': path})
    return jobs


def read_all(paths):
    out = []
    for p in paths:
        out += read_jobs(p)
    return out


def _key(j):
    return (j['start'], j['idx'], j['host'].lower(), tuple(sorted((k, v) for k, v in j['self'].items())))


def _host_match(host, case):
    """잡의 Hostname 이 케이스의 VM 이름(케이스 정보의 VM 열 패턴 또는 데이터의 VM 이름)과 맞는지."""
    h = host.lower()
    if not h:
        return False
    pats = [p.strip().lower() for p in re.split(r'[;,]', case.get('vms') or '') if p.strip()]
    names = [str(e).lower() for e in case.get('_entities') or []]
    return any(fnmatch.fnmatchcase(h, p) for p in pats) or h in names or any(h.split('.')[0] == n.split('.')[0] for n in names)


def assign(cases, jobs, log=print):
    """케이스마다 phase 요약을 c['phase'] 에 넣는다 (연결된 잡이 없으면 넣지 않는다).
    c['_entities'] 에 그 케이스 데이터의 VM 이름을 넣어 두면 Hostname 으로 케이스를 가린다."""
    got = {c['id']: [] for c in cases}
    unsure = set()
    for j in jobs:
        if j['start'] is None:
            continue
        win = [c for c in cases if abs((j['start'] - pd.Timestamp(c['start'])).total_seconds()) <= MATCH_S]
        if not win:
            continue
        by_host = [c for c in win if _host_match(j['host'], c)]
        if by_host:
            for c in by_host:
                got[c['id']].append(j)
        elif any(_host_match(j['host'], c) for c in cases):
            continue                                  # 다른 케이스의 VM 잡: 시각만 겹치는 케이스에 붙이지 않는다
        elif len(win) == 1:
            got[win[0]['id']].append(j)
        else:
            unsure.add((tuple(sorted(c['id'] for c in win)), j['host'] or '없음'))
    for ids, host in sorted(unsure):
        log(f'[phase] 잡 시작 시각이 같은 케이스({", ".join(ids)})가 여럿이고 잡의 Hostname({host})이 '
            '어느 케이스의 VM 이름과도 맞지 않아 그 잡은 쓰지 않음')
    for c in cases:
        seen, uniq = set(), []
        for j in got[c['id']]:
            k = _key(j)
            if k not in seen:
                seen.add(k)
                uniq.append(j)
        s = summarize(uniq)
        if s:
            c['phase'], c['_phase_rows'] = s, [dict(j['row'], __file=j['file']) for j in uniq]
            log(f'[phase] {c["id"]}: 잡 {s["jobs"]}개 평균 · 단계 {len(s["stages"])}개 · 전체 {s["total"]:,.0f} s'
                + (f' · Load {s["load"]:,.0f} s' if s.get('load') else ''))


def summarize(jobs):
    """잡 목록 → {'stages': [[단계, self 평균]], 'bands': [[단계, 시작, 끝]], 'total', 'load', 'jobs', 'expected', 'hosts', 'workers'}."""
    if not jobs:
        return None
    order = []
    for j in jobs:
        for s in j['order']:
            if s not in order:
                order.append(s)
    stages = []
    for s in order:
        vs = [j['self'].get(s) for j in jobs if j['self'].get(s) is not None]
        if vs:
            stages.append([s, round(statistics.mean(vs), 2)])
    if not stages:
        return None
    bands, acc = [], 0.0
    for s, v in stages:
        bands.append([s, round(acc, 2), round(acc + v, 2)])
        acc += v
    lv = [j['total'].get(LOAD_STAGE) for j in jobs if j['total'].get(LOAD_STAGE) is not None]
    exp = [j['total_jobs'] for j in jobs if j['total_jobs']]
    return {'stages': stages, 'bands': bands, 'total': round(acc, 2),
            'load': round(statistics.mean(lv), 2) if lv else None,
            'jobs': len(jobs), 'expected': int(max(exp)) if exp else None,
            'hosts': sorted({j['host'] for j in jobs if j['host']})}


def colors(stage_names, totals=None):
    """단계 → 색. 이전 그래프의 주요 단계 이름이면 그 색, 아니면 self 시간이 긴 5단계에 색, 나머지는 회색 계열."""
    out, ri = {}, 0
    big = {s: BIG[s] for s in stage_names if s in BIG}
    if not big and totals:
        top = sorted(stage_names, key=lambda s: -(totals.get(s) or 0))[:len(BIG_ORDER)]
        big = {s: BIG_ORDER[i] for i, s in enumerate(top)}
    for s in stage_names:
        if s in big:
            out[s] = big[s]
        else:
            out[s] = REST[ri % len(REST)]
            ri += 1
    return out


def table_rows(ph):
    """docx 표의 행: [단계, 시작, 끝, self, 비율] + Load · Compute · 전체 (숫자는 그대로, 표시는 부르는 쪽에서)."""
    tot = ph['total'] or 0.0
    rows = [[s, s0, s1, s1 - s0, (s1 - s0) / tot * 100 if tot else None] for s, s0, s1 in ph['bands']]
    load = ph.get('load')
    names = [s for s, _ in ph['stages']]
    if load and LOAD_STAGE in names:
        i = names.index(LOAD_STAGE)
        rows.append([f'Load ({names[0]} ~ {LOAD_STAGE})', 0.0, load, load, load / tot * 100 if tot else None])
        if i + 1 < len(names):
            rows.append([f'Compute ({names[i + 1]} ~ {names[-1]})', load, tot, tot - load, (tot - load) / tot * 100 if tot else None])
    rows.append(['전체', 0.0, tot, tot, 100.0 if tot else None])
    return rows
