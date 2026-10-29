"""입력 파일 → 공통 표(long format).

공통 표 열: case, host, entity, role, metric, unit, t(datetime), sec(케이스 시작 기준 경과 초), value

입력 프로파일(YAML) 종류
  type: csv_wide   한 행 = 한 시각, 지표는 열 (VCF Operations export 등)
  type: kv_text    한 줄 = 한 레코드, 'Key:Value, Key:Value' 형식 (syslog 계열 텍스트)
"""
import csv
import fnmatch
import io
import re

import pandas as pd

UNIT_RE = re.compile(r'\(([^()]*)\)\s*$')
KV_RE = re.compile(r'(?:(?<=[\s,\[\]])|^)([A-Za-z_][A-Za-z0-9_]*):\s*([^,\s"\]]+)')
MONTH_FIX = [(r'\bSept\b', 'Sep')]


def parse_number(s):
    if s is None:
        return None
    s = str(s).strip().replace(',', '')
    if s in ('', '-', 'N/A', 'NaN', 'nan', 'null'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_time(series, fmt=None, tz=None):
    s = series.astype(str)
    for pat, rep in MONTH_FIX:
        s = s.str.replace(pat, rep, regex=True)
    t = pd.to_datetime(s, format=fmt, errors='coerce') if fmt else None
    if t is None or t.notna().mean() < 0.5:          # 지정 형식으로 절반도 못 읽으면 일반 날짜 형식으로 다시 읽는다
        t = pd.to_datetime(s, errors='coerce')
    if getattr(t.dt, 'tz', None) is not None:
        t = t.dt.tz_convert(tz or 'Asia/Seoul').dt.tz_localize(None)
    return t


def unit_of(name, units_map):
    if name in units_map:
        return units_map[name]
    m = UNIT_RE.search(name)
    return m.group(1) if m else ''


def host_of(entity, rule):
    if not rule:
        return entity
    m = re.match(rule, entity)
    return m.group(1) if m else entity


def _select_metrics(cols, prof):
    inc = prof.get('metrics', 'all_numeric')
    exc = set(prof.get('exclude', []))
    if inc == 'all_numeric':
        return [c for c in cols if c not in exc]
    return [c for c in inc if c in cols]


def read_csv_wide(path, prof):
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding=prof.get('encoding', 'utf-8-sig'))
    tcol, ecol = prof['time_column'], prof.get('entity_column')
    hc = prof.get('host_column') or []
    hcol = next((c for c in ([hc] if isinstance(hc, str) else hc) if c and c in df.columns), None)   # 후보 중 있는 열
    t = parse_time(df[tcol], prof.get('time_format'), prof.get('timezone'))
    ent = df[ecol] if ecol else pd.Series([prof.get('entity', 'host')] * len(df))
    host = df[hcol] if hcol else None
    skip = {tcol, ecol, hcol}
    cols = [c for c in df.columns if c not in skip]
    rows = []
    for c in _select_metrics(cols, prof):
        v = df[c].map(parse_number)
        if v.notna().sum() == 0:
            continue                       # 숫자가 하나도 없는 열은 지표가 아니다
        part = pd.DataFrame({'t': t, 'entity': ent, 'metric': c, 'value': v})
        if host is not None:
            part['_host'] = host.values
        rows.append(part)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=['t', 'entity', 'metric', 'value'])


def read_kv_text(path, prof):
    must = prof.get('line_contains')
    tkey, ekey = prof['time_key'], prof.get('entity_key')
    where = prof.get('where', {})
    recs = []
    with io.open(path, encoding=prof.get('encoding', 'utf-8'), errors='replace') as f:
        for line in f:
            if must and must not in line:
                continue
            kv = {}
            for k, v in KV_RE.findall(line):
                kv.setdefault(k, v)
            # 시각 값은 공백을 포함할 수 있어 따로 읽는다 (예: Timestamp:2026-09-14 15:13:12)
            m = re.search(re.escape(tkey) + r':\s*([0-9T:\-\./ ]+?)(?=,|$)', line)
            if not m:
                continue
            kv[tkey] = m.group(1).strip()
            if any(kv.get(k) != v for k, v in where.items()):
                continue
            recs.append(kv)
    if not recs:
        return pd.DataFrame(columns=['t', 'entity', 'metric', 'value'])
    df = pd.DataFrame(recs)
    t = parse_time(df[tkey], prof.get('time_format'), prof.get('timezone'))
    ent = df[ekey] if ekey and ekey in df else pd.Series([prof.get('entity', 'host')] * len(df))
    skip = {tkey, ekey, *where.keys()}
    rows = []
    for c in _select_metrics([c for c in df.columns if c not in skip], prof):
        v = df[c].map(parse_number)
        if v.notna().sum() == 0:
            continue
        rows.append(pd.DataFrame({'t': t, 'entity': ent, 'metric': c, 'value': v}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=['t', 'entity', 'metric', 'value'])


READERS = {'csv_wide': read_csv_wide, 'kv_text': read_kv_text}
_CACHE = {}     # 같은 파일을 여러 케이스가 쓰면 한 번만 읽는다


def detect_profile(path, profiles):
    """파일 앞부분을 보고 맞는 입력 프로파일 이름을 고른다. 맞는 것이 없으면 None."""
    try:
        with io.open(path, encoding='utf-8-sig', errors='replace') as f:
            head = [next(f, '') for _ in range(300)]
    except OSError:
        return None
    header = next(csv.reader([head[0]])) if head and head[0] else []
    for name, prof in profiles.items():
        if prof['type'] == 'csv_wide':
            need = [prof['time_column']] + ([prof['entity_column']] if prof.get('entity_column') else [])
            if all(c in header for c in need):
                return name
        elif prof['type'] == 'kv_text':
            key = prof.get('line_contains') or f"{prof['time_key']}:"
            if any(key in line for line in head):
                return name
    return None


def load_source(src, prof):
    key = (src['path'], id(prof))
    if key not in _CACHE:
        _CACHE[key] = READERS[prof['type']](src['path'], prof).dropna(subset=['t', 'value'])
    df = _CACHE[key].copy()
    if src.get('entity'):
        df = df[df['entity'] == src['entity']]
    rename = prof.get('rename', {})
    df['metric'] = df['metric'].map(lambda m: rename.get(m, m))
    units = prof.get('units', {})
    df['unit'] = df['metric'].map(lambda m: unit_of(m, units))
    if src.get('host'):
        df['host'] = src['host']
    elif '_host' in df and df['_host'].astype(str).str.len().gt(0).any():
        df['host'] = df['_host'].astype(str)                       # 데이터의 호스트 열
    else:
        df['host'] = df['entity'].map(lambda e: host_of(e, prof.get('host_from_entity')))   # 규칙이 없으면 VM 이름 그대로
    df = df.drop(columns=['_host'], errors='ignore')
    df['role'] = src.get('role') or df['entity'].map(lambda e: src.get('roles', {}).get(e, e))
    df['_role_given'] = bool(src.get('role') or src.get('roles'))
    df['source_system'] = prof.get('source_system', prof['type'])
    df['_src'] = src['path']
    return df


def load_case(case, profiles):
    parts = []
    for src in case['sources']:
        parts.append(load_source(src, profiles[src['profile']]))
    df = pd.concat(parts, ignore_index=True)
    # 역할이 지정되지 않은 소스(예: 여러 VM 이 섞인 로그)는 같은 케이스의 다른 소스에서 VM 이름 → 역할을 가져온다
    known = df[df['_role_given']].drop_duplicates('entity').set_index('entity')['role'].to_dict()
    free = ~df['_role_given']
    df.loc[free, 'role'] = df.loc[free, 'entity'].map(lambda e: known.get(e, e))
    # 케이스에 VM 이 지정되면 그 VM 만 남긴다 (이름 또는 * ? 패턴, 여러 개는 ; 로 구분). 호스트 이름 규칙과 무관하게 쓴다.
    if case.get('vms'):
        pats = [p.strip().lower() for p in re.split(r'[;,]', str(case['vms'])) if p.strip()]
        df = df[df['entity'].str.lower().map(lambda e: any(fnmatch.fnmatchcase(e, p) for p in pats))].copy()
        if case.get('host') and not (df['host'].str.lower() == str(case['host']).strip().lower()).any():
            df['host'] = str(case['host']).strip()             # 데이터에 호스트 정보가 없으면 케이스의 호스트 이름을 쓴다
    # 케이스에 호스트만 지정되면 그 호스트의 VM 만 남긴다 (여러 개는 ; 로 구분)
    elif case.get('host'):
        hosts = [h.strip().lower() for h in re.split(r'[;,]', str(case['host'])) if h.strip()]
        hl, el = df['host'].str.lower(), df['entity'].str.lower()
        by_col = hl.isin(hosts)                                         # 호스트 열 또는 호스트 자신의 기록
        prefix = el.map(lambda e: next((h for h in hosts if e.startswith(h)), None))   # VM 이름이 호스트 이름으로 시작
        df = df[by_col | prefix.notna()].copy()
        pre = prefix[df.index]
        orig = {h.lower(): h for h in re.split(r'[;,]', str(case['host'])) if h.strip()}
        df.loc[pre.notna() & ~by_col[df.index], 'host'] = pre[pre.notna() & ~by_col[df.index]].map(lambda h: orig.get(h, h).strip())
    df = df.drop_duplicates(subset=['entity', 'metric', 't'])      # 기간이 겹치는 파일이 함께 있어도 한 번만 센다
    start = pd.to_datetime(case['start']) if case.get('start') else df['t'].min()
    end = pd.to_datetime(case['end']) if case.get('end') else df['t'].max()
    # 그래프에는 잡 실행 구간 앞뒤 여유 구간(margin_s)까지 싣고, 통계는 실행 구간(in_run)만 쓴다
    m = pd.Timedelta(seconds=case.get('margin_s', 0) or 0)
    df = df[(df['t'] >= start - m) & (df['t'] <= end + m)].copy()
    df['case'] = case['id']
    df['sec'] = (df['t'] - start).dt.total_seconds()
    df['in_run'] = (df['t'] >= start) & (df['t'] <= end)
    return df, start, end
