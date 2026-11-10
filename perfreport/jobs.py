"""잡 TAT 원장: 잡마다 시작 · 종료 · elapsed 가 기록된 CSV 를 읽어 케이스에 붙인다.

입력 CSV (로그 수집 시스템에서 내보낸 잡 기록, 한 행 = 잡 1개). 데이터 폴더에 함께 넣으면 머리글을 보고 찾는다.
  필요한 열: PSStartTime, PSEndTime (예: 2026-08-11_10:00:00), Job_elapse (초)
  쓰는 열: Job_code (잡 종류), job_info (잡 이름, 끝에 <i>_of_<n>), hostname. 다른 열은 쓰지 않는다.
  같은 잡이 두 번 기록된 행은 한 번만 센다.

케이스 연결
  - 케이스 실행 구간 앞뒤 10분 안에 시작한 잡. hostname 이 케이스의 VM 과 맞는 잡이 있으면 그 잡만 쓴다.
    hostname 이 어느 케이스의 VM 과도 맞지 않고 같은 구간의 케이스가 여럿이면 어느 케이스인지 정할 수 없어 쓰지 않는다.

잡 구분 (VM 이름이 아니라 기록 내용으로 판단)
  - 세트 = 같은 Job_code · 같은 hostname 에서 시작 시각이 10초 안인 잡 묶음.
  - TAT 측정 잡 = VM(Job_code · hostname)마다, 케이스 잡 시작 시각 앞뒤 10분 안에 시작한 세트 가운데 잡 elapsed 평균이 가장 긴 세트.
    그 평균이 케이스 실행 구간의 절반 이상일 때만 TAT 측정 잡으로 본다. TAT = 그 세트 잡들의 elapsed 평균.
    여러 VM 에서 돌렸으면(예: 같은 잡을 VM 두 대에서 실행) TAT 측정 잡이 VM 마다 하나씩 생기고 TAT 도 VM 마다 낸다.
  - 동시 실행 잡 = TAT 측정 잡이 아닌 나머지 잡. Job_code · hostname 마다 세트로 묶어 정리한다.
    기준 구간 = TAT 측정 잡 전체의 실행 구간(첫 잡 시작 ~ 마지막 잡 종료). TAT 측정 잡이 없으면 케이스 실행 구간.
    기준 구간 중 시작한 세트: 기준 구간 시작 10초 전에 시작한 세트(동시 시작)부터 기준 구간 끝까지 시작한 세트. 끝난 뒤 시작한 세트는 뺀다.
"""
import csv
import fnmatch
import io
import re
import statistics

import pandas as pd

SET_TOL_S = 10
MATCH_S = 600
# 그래프 색: 이전 리포트와 같은 색. TAT 막대 = 이전 주간 보고서 TAT 막대(녹), 기준선 = 회색 점선,
# 동시 실행 잡 평균 막대 = 이전 주간 보고서 동시 실행 잡 막대(파랑), 잡별 elapsed 점 · 선 = 이전 케이스 리포트(동시 실행 잡이 있는 케이스의 주황),
# 여러 케이스를 한 그래프에 그릴 때의 케이스 색 = 이전 주간 보고서의 케이스 색 순서
COLORS = {'tat': '#68AD3B', 'base': '#A6A6A6', 'conc': '#3D7CAB', 'dot': '#A05010', 'ink': '#333F48', 'ink2': '#6E7880',
          'cases': ['#9DC3E6', '#6FA8CE', '#3D7CAB', '#2E5F86', '#1F3864', '#A12D26']}
_FMT = ('%Y-%m-%d_%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d_%H-%M-%S')
NEED = ('PSStartTime', 'PSEndTime', 'Job_elapse')


def _header(path):
    try:
        with io.open(path, encoding='utf-8-sig', errors='replace') as f:
            first = f.readline()
    except OSError:
        return []
    return [c.strip() for c in next(csv.reader([first]))] if first else []


def is_tat_csv(path):
    """잡 TAT 원장 CSV 인지: PSStartTime · PSEndTime · Job_elapse 열이 모두 있어야 한다."""
    return path.lower().endswith('.csv') and all(c in _header(path) for c in NEED)


def _when(v):
    s = str(v or '').strip()
    for f in _FMT:
        try:
            return pd.Timestamp(pd.to_datetime(s, format=f))
        except (ValueError, TypeError):
            continue
    return None


def _f(v):
    try:
        return float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def read_jobs(path):
    """CSV 1개 → 잡 목록 [{host, code, info, start, end, elapsed, file, row}]. row = 원본 행 (TC 정리에 쓴다)."""
    with io.open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        r = {(k or '').strip(): v for k, v in r.items()}
        s, el = _when(r.get('PSStartTime')), _f(r.get('Job_elapse'))
        if s is None or el is None:
            continue
        e = _when(r.get('PSEndTime')) or s + pd.Timedelta(seconds=el)
        info = str(r.get('job_info') or '').strip()
        code = str(r.get('Job_code') or '').strip() or re.sub(r'_\d+_of_\d+$', '', info) or 'job'
        out.append({'host': str(r.get('hostname') or '').strip(), 'code': code, 'info': info,
                    'start': s, 'end': e, 'elapsed': el, 'file': path, 'row': r})
    return out


def read_all(paths):
    out, seen = [], set()
    for p in paths:
        for j in read_jobs(p):
            k = (j['host'].lower(), j['code'], j['info'], j['start'], j['elapsed'])
            if k not in seen:                       # 같은 잡이 여러 파일 · 여러 행에 있으면 한 번만
                seen.add(k)
                out.append(j)
    return out


def _host_match(host, case):
    h = host.lower()
    if not h:
        return False
    pats = [p.strip().lower() for p in re.split(r'[;,]', case.get('vms') or '') if p.strip()]
    names = [str(e).lower() for e in case.get('_entities') or []]
    return any(fnmatch.fnmatchcase(h, p) for p in pats) or h in names or any(h.split('.')[0] == n.split('.')[0] for n in names)


def _sets(jobs):
    """{(Job_code, hostname): [[잡...], ...]} 세트 = 시작 시각이 세트 첫 잡과 10초 안인 잡 묶음."""
    by = {}
    for j in sorted(jobs, key=lambda j: (j['start'], j['info'])):
        sets = by.setdefault((j['code'], j['host']), [])
        if sets and (j['start'] - sets[-1][0]['start']).total_seconds() <= SET_TOL_S:
            sets[-1].append(j)
        else:
            sets.append([j])
    return by


def _iso(t):
    return f'{t:%Y-%m-%d %H:%M:%S}'


def classify(case, jobs):
    """케이스의 잡 → {'main': TAT 측정 잡 또는 None, 'conc': [동시 실행 잡 묶음], 'window': [시작, 끝]} 또는 None."""
    if not jobs:
        return None
    cs, ce = pd.Timestamp(case['start']), pd.Timestamp(case['end'])
    dur = max((ce - cs).total_seconds(), 1.0)
    by = _sets(jobs)
    # TAT 측정 잡: VM(잡 종류 · hostname)마다 케이스 시작 ±10분에 시작해 실행 구간 절반 이상 실행된 세트 가운데 가장 긴 세트.
    # 같은 잡을 VM 두 대에서 실행하면 TAT 측정 잡이 둘이 되고, TAT 도 VM 마다 따로 낸다.
    picked = {}
    for k, sets in by.items():
        best = None
        for i, st in enumerate(sets):
            if abs((st[0]['start'] - cs).total_seconds()) > MATCH_S:
                continue
            m = statistics.mean(j['elapsed'] for j in st)
            if m >= dur * 0.5 and (best is None or m > best[0]):
                best = (m, i)
        if best:
            picked[k] = best[1]
    mains = []
    for k, i in picked.items():
        js = sorted(by[k][i], key=lambda j: j['info'])
        els = [j['elapsed'] for j in js]
        mains.append({'code': k[0], 'hosts': sorted({j['host'] for j in js if j['host']}),
                      'label': f'{k[0]} · {k[1]}' if len(picked) > 1 and k[1] else k[0],
                      'form': 'individual' if all(re.search(r'_1_of_1$', j['info']) for j in js) else 'batch',
                      'jobs': [[j['info'] or j['code'], j['host'], _iso(j['start']), _iso(j['end']), round(j['elapsed'], 2)] for j in js],
                      'tat': round(statistics.mean(els), 2), 'min': round(min(els), 2), 'max': round(max(els), 2),
                      'start': _iso(min(j['start'] for j in js)), 'end': _iso(max(j['end'] for j in js)),
                      'other_sets': len(by[k]) - 1})
    mains.sort(key=lambda m: (m['hosts'], m['code']))
    w0 = min(pd.Timestamp(m['start']) for m in mains) if mains else cs
    w1 = max(pd.Timestamp(m['end']) for m in mains) if mains else ce
    win = max((w1 - w0).total_seconds(), 1.0)
    hosts_per_code = {}
    for (code, host) in by:
        hosts_per_code.setdefault(code, set()).add(host)
    conc = []
    for (code, host), sets in by.items():
        sets = [st for i, st in enumerate(sets) if picked.get((code, host)) != i]
        if not sets:
            continue
        rows, spans = [], []
        for st in sets:
            els = [j['elapsed'] for j in st]
            s0, s1 = min(j['start'] for j in st), max(j['end'] for j in st)
            rows.append([round((s0 - w0).total_seconds(), 1), len(st), round(statistics.mean(els), 2),
                         round(min(els), 2), round(max(els), 2)])
            spans.append((s0, s1))
        merged = []                                 # 기준 구간 안 가동 구간: 세트 구간의 합집합 (겹침을 두 번 세지 않는다)
        for s0, s1 in sorted(spans):
            a, b = max(s0, w0), min(s1, w1)
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        busy = sum((b - a).total_seconds() for a, b in merged)
        gaps = [(merged[i + 1][0] - merged[i][1]).total_seconds() for i in range(len(merged) - 1)]
        head = (merged[0][0] - w0).total_seconds() if merged else win
        tail = (w1 - merged[-1][1]).total_seconds() if merged else win
        allu = []
        for s0, s1 in sorted(spans):
            if allu and s0 <= allu[-1][1]:
                allu[-1][1] = max(allu[-1][1], s1)
            else:
                allu.append([s0, s1])
        union_all = sum((b - a).total_seconds() for a, b in allu)
        span_sum = sum((b - a).total_seconds() for a, b in spans)
        els = [j['elapsed'] for st in sets for j in st]
        n_jobs = len(els)
        label = f'{code} · {host}' if len(hosts_per_code[code]) > 1 and host else code
        conc.append({'label': label, 'code': code, 'host': host,
                     'sets': len(sets), 'jobs': n_jobs, 'jobs_per_set': round(n_jobs / len(sets), 2),
                     'sets_in_window': sum(1 for r in rows if -SET_TOL_S <= r[0] <= win),
                     'mean': round(statistics.mean(els), 2), 'min': round(min(els), 2), 'max': round(max(els), 2),
                     'first_start': _iso(min(s for s, _ in spans)), 'last_end': _iso(max(e for _, e in spans)),
                     'window_s': round(win, 1), 'busy_s': round(busy, 1), 'coverage_pct': round(busy / win * 100, 1),
                     'outside_s': round(union_all - busy, 1), 'overlap_s': round(span_sum - union_all, 1),
                     'max_gap_s': round(max(gaps + [head, tail]), 1), 'head_gap_s': round(head, 1), 'tail_gap_s': round(tail, 1),
                     'set_rows': rows,
                     'set_jobs': [[[j['info'] or j['code'], _iso(j['start']), _iso(j['end']), round(j['elapsed'], 2)]
                                   for j in sorted(st, key=lambda j: j['info'])] for st in sets],
                     'points': [[round((j['start'] - w0).total_seconds(), 1), round(j['elapsed'], 2)] for st in sets for j in st]})
    conc.sort(key=lambda g: (g['code'], g['host']))
    if not mains and not conc:
        return None
    return {'mains': mains, 'main': mains[0] if mains else None, 'conc': conc, 'window': [_iso(w0), _iso(w1)], 'by_main': bool(mains)}


def assign(cases, jobs, log=print):
    """케이스마다 잡 정리 결과를 c['jobs'] 에, 그 케이스에 쓴 원본 행을 c['_job_rows'] 에 넣는다."""
    got = {c['id']: [] for c in cases}
    unsure = set()
    for j in jobs:
        win = [c for c in cases
               if pd.Timestamp(c['start']) - pd.Timedelta(seconds=MATCH_S) <= j['start'] <= pd.Timestamp(c['end']) + pd.Timedelta(seconds=MATCH_S)]
        if not win:
            continue
        by_host = [c for c in win if _host_match(j['host'], c)]
        if by_host:
            for c in by_host:
                got[c['id']].append(j)
        elif any(_host_match(j['host'], c) for c in cases):
            continue                                  # 다른 케이스의 VM 잡이 구간 밖에 있는 경우: 시각만 겹치는 케이스에 붙이지 않는다
        elif len(win) == 1:
            got[win[0]['id']].append(j)
        else:
            unsure.add((tuple(sorted(c['id'] for c in win)), j['host'] or '없음'))
    for ids, host in sorted(unsure):
        log(f'[잡] 실행 구간이 겹치는 케이스({", ".join(ids)})가 여럿이고 잡의 hostname({host})이 '
            '어느 케이스의 VM 이름과도 맞지 않아 그 잡은 쓰지 않음')
    for c in cases:
        r = classify(c, got[c['id']])
        if not r:
            continue
        c['jobs'], c['_job_rows'] = r, [dict(j['row'], __file=j['file']) for j in got[c['id']]]
        log(f'[잡] {c["id"]}: '
            + (' · '.join(f'TAT 측정 잡 {m["label"]} {len(m["jobs"])}개 평균 {m["tat"]:,.2f} s' for m in r['mains']) or 'TAT 측정 잡 없음')
            + ''.join(f' · 동시 실행 잡 {g["label"]} {g["sets"]}세트(기준 구간 중 시작 {g["sets_in_window"]}회)' for g in r['conc']))


def degradation(tat, base):
    """저하율 % = (TAT - 기준) ÷ 기준 × 100. 기준이 없으면 None."""
    try:
        b = float(base)
    except (TypeError, ValueError):
        return None
    return (tat - b) / b * 100 if b > 0 else None
