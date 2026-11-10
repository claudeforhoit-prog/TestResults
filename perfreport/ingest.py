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


def to_number(col):
    """parse_number 의 열 단위 판: 쉼표를 지우고 숫자로 읽는다. 읽지 못하면 NaN. 행마다 함수를 부르지 않아 빠르다."""
    s = col.astype(str).str.strip().str.replace(',', '', regex=False)
    return pd.to_numeric(s, errors='coerce')


def map_unique(s, fn):
    """같은 값은 한 번만 계산해 열 전체에 붙인다 (행이 수백만 개여도 값의 가짓수만큼만 계산)."""
    u = pd.unique(s)
    return s.map(dict(zip(u, (fn(x) for x in u))))


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


# VM 이 올라가 있는 호스트(ESX)를 나타내는 열 이름. 프로파일의 host_column 다음으로 이 순서대로 찾는다.
# 대소문자 · 밑줄 · 하이픈은 가리지 않는다. 'Hostname' 은 넣지 않는다: Key:Value 로그에서는 VM 자신의 이름이고,
# PowerCLI 목록에서는 게스트 OS 의 호스트 이름이라 ESX 를 가리키지 않는다.
PARENT_HOST_COLUMNS = ['Summary|Parent Host', 'Parent Host', 'ParentHost', 'ESX Host', 'ESXHost', 'ESXi Host', 'ESXiHost',
                       'ESX Hostname', 'ESXi Hostname', 'ESX Host Name', 'ESXi Host Name', 'ESX', 'ESXi',
                       'VMHost', 'VM Host', 'Host', 'Host Name']


def _norm_col(c):
    return re.sub(r'\s+', ' ', re.sub(r'[_\-]+', ' ', str(c))).strip().lower()


def host_columns(columns, prof=None, exclude=()):
    """columns 중 상위 호스트(ESX) 열 이름 목록. 프로파일의 host_column 후보를 먼저 보고, 이어서 PARENT_HOST_COLUMNS."""
    hc = (prof or {}).get('host_column') or []
    want = ([hc] if isinstance(hc, str) else list(hc)) + PARENT_HOST_COLUMNS
    have = {}
    for c in columns:
        have.setdefault(_norm_col(c), c)
    skip = {_norm_col(e) for e in exclude if e}
    out = []
    for w in want:
        k = _norm_col(w)
        if k in have and k not in skip and have[k] not in out:
            out.append(have[k])
    return out


def same_host(name, host):
    """같은 장비 이름인지. 대소문자를 가리지 않고, 도메인을 붙인 이름(esx01.corp.local)과 짧은 이름(esx01)도 같게 본다."""
    a, b = str(name).strip().lower(), str(host).strip().lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b + '.') or b.startswith(a + '.'))


def named_after_host(entity, host):
    """VM 이름이 호스트 이름으로 시작하는지(예: 호스트 esx01 → esx01-vm01, node7 → node7app01).
    호스트 이름 뒤에 같은 종류의 글자가 이어지면 다른 호스트로 본다: esx1 과 esx10-vm01 처럼 숫자 뒤에 숫자가 이어지는 경우."""
    e, h = str(entity).strip().lower(), str(host).strip().lower()
    if h and not e.startswith(h) and '.' in h:     # 호스트 이름을 도메인까지 적었으면(esx01.corp.local) 짧은 이름(esx01)으로 본다
        h = h.split('.', 1)[0]
    if not h or not e.startswith(h):
        return False
    if len(e) == len(h):
        return True
    a, b = h[-1], e[len(h)]
    return not ((a.isdigit() and b.isdigit()) or (a.isalpha() and b.isalpha()))


def _select_metrics(cols, prof):
    inc = prof.get('metrics', 'all_numeric')
    exc = set(prof.get('exclude', []))
    if inc == 'all_numeric':
        return [c for c in cols if c not in exc]
    return [c for c in inc if c in cols]


def read_csv_wide(path, prof):
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding=prof.get('encoding', 'utf-8-sig'))
    tcol, ecol = prof['time_column'], prof.get('entity_column')
    hcol = next(iter(host_columns(df.columns, prof, exclude=[tcol, ecol])), None)   # 상위 호스트 열 후보 중 있는 열
    t = parse_time(df[tcol], prof.get('time_format'), prof.get('timezone'))
    ent = df[ecol] if ecol else pd.Series([prof.get('entity', 'host')] * len(df))
    host = df[hcol] if hcol else None
    skip = {tcol, ecol, hcol}
    cols = [c for c in df.columns if c not in skip]
    rows = []
    for c in _select_metrics(cols, prof):
        v = to_number(df[c])
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
    hkey = next(iter(host_columns(df.columns, prof, exclude=[tkey, ekey])), None)   # 상위 호스트 키 (예: ESXHost:esx01)
    skip = {tkey, ekey, hkey, *where.keys()}
    rows = []
    for c in _select_metrics([c for c in df.columns if c not in skip], prof):
        v = to_number(df[c])
        if v.notna().sum() == 0:
            continue
        part = pd.DataFrame({'t': t, 'entity': ent, 'metric': c, 'value': v})
        if hkey:
            part['_host'] = df[hkey].fillna('').values
        rows.append(part)
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
    if rename:
        df['metric'] = map_unique(df['metric'], lambda m: rename.get(m, m))
    units = prof.get('units', {})
    df['unit'] = map_unique(df['metric'], lambda m: unit_of(m, units))
    if src.get('host'):
        df['host'] = src['host']
    elif '_host' in df and df['_host'].astype(str).str.len().gt(0).any():
        h = df['_host'].astype(str).str.strip()                     # 데이터의 호스트 열 (빈 칸은 아래 규칙으로 채운다)
        df['host'] = h.where(h != '', map_unique(df['entity'], lambda e: host_of(e, prof.get('host_from_entity'))))
    else:
        df['host'] = map_unique(df['entity'], lambda e: host_of(e, prof.get('host_from_entity')))   # 규칙이 없으면 VM 이름 그대로
    df = df.drop(columns=['_host'], errors='ignore')
    df['role'] = src.get('role') or map_unique(df['entity'], lambda e: src.get('roles', {}).get(e, e))
    df['_role_given'] = bool(src.get('role') or src.get('roles'))
    df['source_system'] = prof.get('source_system', prof['type'])
    df['_src'] = src['path']
    return df


def _one_host_per_vm(df, hosts, case_host):
    """VM 마다 호스트 표기를 하나로 맞춘다. 파일마다 호스트 정보가 다르게 붙으면(CSV 는 esx01.corp.local, 로그는 없음)
    같은 VM 이 그래프 목록에 두 번 나오기 때문이다.
      1. 데이터의 호스트 값(호스트 열 · 키)이 있으면: 케이스 호스트와 같은 장비면 케이스 정보의 표기, 아니면 가장 많이 나온 값
      2. 없으면: VM 이름이 케이스 호스트 이름이거나 그 이름으로 시작하면 그 호스트, 아니면 케이스 정보의 호스트 이름 칸 그대로"""
    if df.empty:
        return df
    ent, host = df['entity'].astype(str), df['host'].astype(str)
    known = df[(host != ent) & (host != '')]
    label = {}
    for e, vals in known.groupby('entity')['host']:
        order = vals.value_counts().index.tolist()
        mine = next((h for v in order for h in hosts if same_host(v, h)), None)
        label[e] = mine or order[0]
    for e in ent.unique():
        if e not in label:
            label[e] = next((h for h in hosts if same_host(e, h) or named_after_host(e, h)), case_host)
    df['host'] = ent.map(label)
    return df


def load_case(case, profiles):
    parts = []
    for src in case['sources']:
        parts.append(load_source(src, profiles[src['profile']]))
    df = pd.concat(parts, ignore_index=True)
    # 역할이 지정되지 않은 소스(예: 여러 VM 이 섞인 로그)는 같은 케이스의 다른 소스에서 VM 이름 → 역할을 가져온다
    known = df[df['_role_given']].drop_duplicates('entity').set_index('entity')['role'].to_dict()
    free = ~df['_role_given']
    df.loc[free, 'role'] = map_unique(df.loc[free, 'entity'], lambda e: known.get(e, e))
    # 케이스에 VM 이 지정되면 그 VM 만 남긴다 (이름 또는 * ? 패턴, 여러 개는 ; 로 구분).
    # 이름을 정확히 적은 VM 은 호스트와 관계없이 쓴다. 와일드카드로 고른 VM 은 케이스 호스트에 있는 것만 쓴다
    # (사용자 지시 2026-11-08): 실행 구간의 상위 호스트 정보(모든 파일 합산)로 다른 호스트에만 있다고 확인된 VM 을 뺀다.
    # 상위 호스트 정보가 없는 VM 은 그대로 쓴다. VM 이름은 호스트끼리 겹치지 않는다고 본다.
    if case.get('vms'):
        pats = [p.strip().lower() for p in re.split(r'[;,]', str(case['vms'])) if p.strip()]
        exact = {p for p in pats if not any(ch in p for ch in '*?[')}
        wild = [p for p in pats if p not in exact]
        el = df['entity'].astype(str).str.lower()
        by_name = el.isin(exact)
        by_wild = el.map({e: any(fnmatch.fnmatchcase(e, p) for p in wild) for e in el.unique()}).astype(bool) & ~by_name
        hosts = [h.strip() for h in re.split(r'[;,]', str(case.get('host') or '')) if h.strip()]
        if hosts and by_wild.any():
            m = pd.Timedelta(seconds=case.get('margin_s', 0) or 0)
            win = by_wild & (df['host'].astype(str) != df['entity'].astype(str))      # 호스트 열 · 키에서 온 값만
            if case.get('start') and case.get('end'):
                win &= (df['t'] >= pd.to_datetime(case['start']) - m) & (df['t'] <= pd.to_datetime(case['end']) + m)
            seen = df[win].groupby('entity')['host'].agg(lambda s: any(same_host(v, h) for v in set(s) for h in hosts))
            by_wild &= ~df['entity'].isin(set(seen[~seen].index))
        df = df[by_name | by_wild].copy()
    # 케이스에 호스트만 지정되면 그 호스트의 VM 만 남긴다 (여러 개는 ; 로 구분)
    elif case.get('host'):
        hosts = [h.strip().lower() for h in re.split(r'[;,]', str(case['host'])) if h.strip()]
        on = {v: any(same_host(v, h) for h in hosts) for v in df['host'].unique()}
        by_col = df['host'].map(on).astype(bool)                        # 호스트 열 또는 호스트 자신의 기록
        nm = {e: next((h for h in hosts if named_after_host(e, h)), None) for e in df['entity'].unique()}
        prefix = df['entity'].map(nm)                                   # VM 이름이 호스트 이름으로 시작
        df = df[by_col | prefix.notna()].copy()
    if case.get('host'):
        df = _one_host_per_vm(df, [h.strip() for h in re.split(r'[;,]', str(case['host'])) if h.strip()], str(case['host']).strip())
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
