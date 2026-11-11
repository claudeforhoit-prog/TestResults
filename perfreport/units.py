"""단위 변환과 계산 지표.

단위 변환 (창 화면의 체크 칸 · 명령의 --to-gb · --to-gbps, 기본은 원래 단위 그대로)
  - 크기: 이름 끝 괄호 단위 '(KB)' 나 이름 끝 '_MB' 같은 표기를 읽어 GB 로 바꾼다. 바이트 단위는 1024 배수 (1 GB = 1024 MB).
  - 네트워크 속도: Network 분류 지표의 '(KBps)' · '(Mbps)' 같은 속도 단위를 Gbps 로 바꾼다. 비트 단위는 1000 배수
    (1 Gbps = 1000 Mbps)이고, 바이트 단위 속도는 1 KBps = 1024 × 8 bit/s 로 환산한다. 디스크 처리량은 바꾸지 않는다.
  - 이름 안의 단위 표기도 같이 바꾼다 (AnonHugePages_MB → AnonHugePages_GB). 바꾼 이름이 이미 있는 지표면 바꾸지 않는다.
  - 역할 파일 · HTML 선택 파일 · 분석 프로파일의 지표 이름은 단위 표기를 빼고 맞춰 본다 (same_metric).

계산 지표
  - THP Coverage (%) = AnonHugePages ÷ AnonPages × 100. 두 지표가 모두 있고 THP coverage 지표가 아직 없는 VM 에만 만든다.
    AnonPages 가 0 이하인 시각은 비운다.
"""
import re

import numpy as np
import pandas as pd

from . import metrics as metrics_mod

THP_COVERAGE = 'THP Coverage (%)'
_TOKEN = re.compile(r'(?:\((?P<p>[^()]+)\)|_(?P<s>[A-Za-z/]+))\s*$')     # 이름 끝 '(단위)' 또는 '_단위'
_SIZE = re.compile(r'(?P<x>[kKmMgGtT])(?P<i>i?)(?P<b>[Bb])')
_RATE = re.compile(r'(?P<x>[kKmMgGtT]?)(?P<b>[Bb])(?:ps|/s)')
_PREFIX = 'KMGT'


def _token(name):
    """이름 끝 단위 표기 → (단위, 시작 위치, 끝 위치, 괄호 여부). 없으면 None."""
    m = _TOKEN.search(name)
    if not m:
        return None
    if m.group('p') is not None:
        return m.group('p').strip(), m.start('p'), m.end('p'), True
    s = m.group('s')                    # '_Pct' · '_Delta' 같은 이름 끝 낱말은 단위가 아니다: 크기 · 속도 단위만 받는다
    if size_exp(s) is None and not rate_bps(s):
        return None
    return s, m.start('s'), m.end('s'), False


def size_exp(unit):
    """크기 단위 → 1024 의 몇 제곱 바이트인지 (B=0, KB=1, MB=2, GB=3, TB=4). 크기 단위가 아니면 None.
    'Mb' · 'Kb' 처럼 대문자 뒤 소문자 b 는 비트라 크기로 보지 않는다. 'mb' · 'kb' 처럼 모두 소문자면 바이트로 본다."""
    u = (unit or '').strip()
    if u.lower() in ('b', 'byte', 'bytes'):
        return 0
    m = _SIZE.fullmatch(u)
    if not m:
        return None
    if m.group('b') == 'b' and u != u.lower():
        return None
    return _PREFIX.index(m.group('x').upper()) + 1


def rate_bps(unit):
    """네트워크 속도 단위 → 1 단위가 몇 bit/s 인지. 바이트 단위(KBps · MB/s)는 1024 배수 × 8, 비트 단위(kbps · Mbps)는 1000 배수.
    속도 단위가 아니면 None."""
    m = _RATE.fullmatch((unit or '').strip())
    if not m:
        return None
    k = _PREFIX.index(m.group('x').upper()) + 1 if m.group('x') else 0
    return 8.0 * 1024 ** k if m.group('b') == 'B' else 1000.0 ** k


def _plan(name, unit, to_gb, to_gbps):
    """지표 하나의 변환 계획 → (새 이름, 곱할 값, 새 단위) 또는 None."""
    tok = _token(name)
    src = tok[0] if tok else (unit or '')
    new_unit = factor = None
    e = size_exp(src)
    if to_gb and e is not None and src != 'GB':
        new_unit, factor = 'GB', 1024.0 ** (e - 3)
    elif to_gbps and metrics_mod.category(name) == 'Network' and rate_bps(src) and src != 'Gbps':
        new_unit, factor = 'Gbps', rate_bps(src) / 1e9
    if new_unit is None:
        return None
    new_name = name[:tok[1]] + new_unit + name[tok[2]:] if tok else name
    return new_name, factor, new_unit


def convert(df, to_gb=False, to_gbps=False):
    """크기 → GB, 네트워크 속도 → Gbps. 반환: (바꾼 표, {원래 이름: 새 이름})."""
    if not (to_gb or to_gbps) or df.empty:
        return df, {}
    names = list(dict.fromkeys(df['metric']))
    units = df.drop_duplicates('metric').set_index('metric')['unit'].to_dict() if 'unit' in df else {}
    taken = set(names)
    plan = {}
    for m in names:
        p = _plan(m, units.get(m, ''), to_gb, to_gbps)
        if p and (p[0] == m or p[0] not in taken):
            plan[m] = p
            taken.add(p[0])
    if not plan:
        return df, {}
    df = df.copy()
    f = df['metric'].map({m: p[1] for m, p in plan.items()})
    hit = f.notna()
    df.loc[hit, 'value'] = df.loc[hit, 'value'] * f[hit]
    df.loc[hit, 'unit'] = df.loc[hit, 'metric'].map({m: p[2] for m, p in plan.items()})
    df.loc[hit, 'metric'] = df.loc[hit, 'metric'].map({m: p[0] for m, p in plan.items()})
    return df, {m: p[0] for m, p in plan.items() if p[0] != m}


def rename_names(names, to_gb=False, to_gbps=False):
    """지표 이름 목록에 같은 변환을 적용한 이름 (창 화면의 역할 지정 목록용)."""
    out, taken = [], set(names)
    for m in names:
        p = _plan(m, '', to_gb, to_gbps)
        if p and p[0] != m and p[0] not in taken:
            taken.add(p[0])
            out.append(p[0])
        else:
            out.append(m)
    return out


def canon(name):
    """단위 표기를 뺀 비교용 이름: 'AnonHugePages_MB' 와 'AnonHugePages_GB' 는 같은 지표로 본다."""
    tok = _token(name)
    if tok and (size_exp(tok[0]) is not None or rate_bps(tok[0])):
        return (name[:tok[1]] + '<unit>' + name[tok[2]:]).lower()
    return name.lower()


def same_metric(present):
    """present 의 지표 이름으로 바꿔 주는 함수: 이름이 같으면 그대로, 단위 표기만 다르면 present 쪽 이름, 없으면 None."""
    exact = set(present)
    by_canon = {}
    for m in present:
        by_canon.setdefault(canon(m), m)
    return lambda m: m if m in exact else by_canon.get(canon(m))


def _anon_parts(name):
    """'AnonHugePages_MB' · 'Guest THP|AnonPages (KB)' → (앞부분, 'anonhugepages' 또는 'anonpages', 단위). 아니면 None."""
    head, _, last = name.rpartition('|')
    tok = _token(last)
    base = last[:tok[1] - (1 if tok[3] else 0)] if tok else last
    key = re.sub(r'[\s_\-]+', '', base).lower()
    if key not in ('anonhugepages', 'anonpages'):
        return None
    return head, key, tok[0] if tok else ''


def _is_coverage(name):
    return bool(re.search(r'thp[\s_]*coverage', name.lower()))


def coverage_name(head):
    return f'{head}|{THP_COVERAGE}' if head else THP_COVERAGE


def thp_pairs(names):
    """[(AnonHugePages 지표, AnonPages 지표, 만들 THP Coverage 이름)]: 같은 앞부분끼리 짝짓는다."""
    hp, ap = {}, {}
    for m in names:
        p = _anon_parts(m)
        if p:
            (hp if p[1] == 'anonhugepages' else ap).setdefault(p[0], m)
    return [(hp[h], ap[h], coverage_name(h)) for h in hp if h in ap]


def thp_names(names):
    """역할 지정 목록용: 만들어질 THP Coverage 이름을 덧붙인 목록."""
    names = list(names)
    if any(_is_coverage(m) and ' · ' not in m for m in names):
        return names
    return names + [c for _, _, c in thp_pairs(names) if c not in names]


def derive_thp(df):
    """THP Coverage (%) 행을 덧붙인다. AnonHugePages 와 AnonPages 의 같은 케이스 · VM · 시각 값으로 계산한다."""
    if df.empty:
        return df
    names = list(dict.fromkeys(df['metric']))
    pairs = thp_pairs(names)
    if not pairs:
        return df
    vm_level = ~df['metric'].str.contains(' · ', regex=False)            # 'node0 · …' 같은 노드별 지표는 VM 전체 값이 아니다
    cov = df.loc[df['metric'].map(_is_coverage) & vm_level]
    has_cov = set(zip(cov['case'], cov['entity'])) if 'case' in df else set(cov['entity'])   # 케이스 · VM 마다 판단
    units = df.drop_duplicates('metric').set_index('metric')['unit'].to_dict() if 'unit' in df else {}
    keys = ['case', 'entity', 't']
    add = []
    for hp, ap, cov in pairs:
        eh = size_exp(_anon_parts(hp)[2] or units.get(hp, ''))
        ea = size_exp(_anon_parts(ap)[2] or units.get(ap, ''))
        if eh is None or ea is None:                 # 한쪽만 단위를 알면 같은 단위로 본다
            eh = ea = eh if eh is not None else (ea if ea is not None else 0)
        a = df[df['metric'] == hp]
        if has_cov:
            key = list(zip(a['case'], a['entity'])) if 'case' in df else list(a['entity'])
            a = a[[k not in has_cov for k in key]]
        b = df.loc[df['metric'] == ap, keys + ['value']].drop_duplicates(keys).rename(columns={'value': '_ap'})
        j = a.merge(b, on=keys, how='inner')
        if j.empty:
            continue
        hp_b = j['value'].astype(float) * 1024.0 ** eh
        ap_b = j['_ap'].astype(float) * 1024.0 ** ea
        j['value'] = np.where(ap_b > 0, hp_b / ap_b.where(ap_b > 0) * 100.0, np.nan)
        j = j.drop(columns=['_ap'])
        j = j[j['value'].notna()]
        j['metric'], j['unit'] = cov, '%'
        add.append(j)
    if not add:
        return df
    return pd.concat([df] + add, ignore_index=True)
