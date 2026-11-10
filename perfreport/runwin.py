"""잡 실행 구간: 종료 시각 자동 감지와 TC 별 데이터 정리(원본에서 그 TC 구간만 잘라 저장).

종료 시각 자동 감지
  케이스 정보의 '잡 종료 시각' 을 비우면, 잡 시작 이후 CPU 사용률이 기준값 미만으로 IDLE_MIN_S 이상 유지되기
  시작한 시각을 종료 시각으로 쓴다. CPU 사용률은 그 케이스 VM 들의 같은 분 값 중 최댓값을 5분 이동 중앙값으로 고른다.
  기준값은 IDLE_PCT 와 실행 중 p95 의 IDLE_REL 중 큰 값이다. 잡 시작 직후 MIN_RUN_S 동안은 판정하지 않는다.
"""
import os
import re
import shutil
import time

import pandas as pd

from . import ingest

MARGIN_S = 600          # 잡 실행 구간 앞뒤로 함께 싣는 여유 구간 (10분)
AUTO_MAX_H = 72         # 종료 시각을 찾을 최대 범위 (잡 시작 후 72시간, 같은 호스트의 다음 케이스 시작 전까지)
IDLE_PCT = 5.0          # 이 값 미만이면 유휴로 본다 (CPU 사용률 %)
IDLE_MIN_S = 600        # 유휴가 이 시간 이상 이어져야 종료로 본다 (10분)
MIN_RUN_S = 300         # 잡 시작 후 이 시간 안의 저점은 판정하지 않는다 (5분)
IDLE_REL = 0.10         # 실행 중 CPU 사용률 p95 의 이 비율보다 낮으면 유휴 (유휴 상태가 0% 가 아닌 환경 대응)
SMOOTH_N = 5            # 순간 튐을 거르는 이동 중앙값 표본 수 (1분 간격이면 5분)

CPU_METRICS = [r'^cpu\|usage \(%\)$', r'cpu.*usage.*%', r'cpu_?usage', r'cpu_?util']


def cpu_metric(df):
    names = list(dict.fromkeys(df['metric']))
    for pat in CPU_METRICS:
        hit = [m for m in names if re.search(pat, m.lower())]
        if hit:
            return hit[0]
    return None


def detect_end(df, start):
    """반환: (종료 시각 또는 None, 설명).
    신호 = 케이스 VM 들의 같은 분(minute) CPU 사용률 최댓값을 5분 이동 중앙값으로 고른 값.
    기준값 = max(IDLE_PCT, 신호 p95 의 IDLE_REL). 신호가 기준값 미만으로 IDLE_MIN_S 이상 이어지기 시작한 분을 종료 시각으로 쓴다."""
    m = cpu_metric(df)
    if not m:
        return None, 'CPU 사용률 지표가 없어 종료 시각을 감지하지 못함'
    d = df[(df['metric'] == m) & (df['t'] >= start)]
    if d.empty:
        return None, f'{m} 데이터가 잡 시작 이후에 없음'
    s = d.assign(t=d['t'].dt.floor('min')).groupby('t')['value'].max().sort_index()
    s = s.rolling(SMOOTH_N, center=True, min_periods=1).median()
    th = max(IDLE_PCT, float(s.quantile(0.95)) * IDLE_REL)
    s = s[s.index >= start + pd.Timedelta(seconds=MIN_RUN_S)]
    times, idle = list(s.index), list(s < th)
    i = 0
    while i < len(times):
        if idle[i]:
            j = i
            while j + 1 < len(times) and idle[j + 1]:
                j += 1
            if (times[j] - times[i]).total_seconds() >= IDLE_MIN_S:
                return times[i], (f'{m} VM 최댓값(5분 이동 중앙값)이 {th:.1f}% 미만으로 {IDLE_MIN_S // 60}분 이상 '
                                  f'유지되기 시작한 시각, 기준값 = max({IDLE_PCT:g}%, 실행 중 p95 의 {IDLE_REL:.0%})')
            i = j + 1
        else:
            i += 1
    # 유휴 구간이 없고 데이터가 먼저 끝났으면(잡 구간만 잘라 둔 파일 등) 마지막 표본 시각을 쓰고 그 사실을 남긴다
    last = d['t'].max()
    if len(times) and last < times[-1] + pd.Timedelta(minutes=2):
        return last, (f'유휴 구간 없이 데이터가 끝나 데이터 마지막 시각을 씀 ({m} 이 {th:.1f}% 미만으로 '
                      f'{IDLE_MIN_S // 60}분 이상 유지되는 구간을 찾지 못함)')
    return None, f'{m} 이 {th:.1f}% 미만으로 {IDLE_MIN_S // 60}분 이상 유지되는 구간이 없음'


def _safe_dir(name):
    return re.sub(r'[\\/:*?"<>|]+', '_', str(name)).strip() or 'case'


def _decider(hosts, ents_l):
    """행 하나를 남길지 정하는 함수. s = 행의 대상 이름(VM 또는 ESX).
    VM 은 리포트와 같은 규칙으로 고른 이름(ents_l)이다. 와일드카드로 고른 VM 중 다른 호스트에 있는 VM 은 그 단계에서 이미 빠져 있다."""
    memo = {}

    def keep(s):
        s = str(s or '').strip()
        if s not in memo:
            if not hosts:                                  # 케이스에 호스트 이름이 없으면 VM 으로만 고른다
                memo[s] = not s or s.lower() in ents_l
            else:                                          # 이 케이스의 VM, 또는 ESX 호스트 자신의 기록
                memo[s] = s.lower() in ents_l or any(ingest.same_host(s, h) for h in hosts)
        return memo[s]
    return keep


NAMED_KINDS = ('ops', 'tat', 'phase')    # TC 정리 파일 이름에 쓰는 로그 종류. 그 밖의 로그는 etc

# 형식을 등록하지 않은 로그에서 줄마다 시각을 찾는 규칙 (줄의 첫 시각을 쓴다)
_MON = {m: i for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
_TS_ISO = re.compile(rb'(\d{4})[-/](\d{2})[-/](\d{2})[T _](\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?(?:\s*(Z|[+-]\d{2}:?\d{2}))?')
_TS_DMY = re.compile(rb'(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec) (\d{4}),? (\d{2}):(\d{2}):(\d{2})')
_TS_SYS = re.compile(rb'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\b')


def _line_time(b, year):
    """줄(바이트) → (시각, 시간대 환산 여부) 또는 (None, False). 시간대 표기(Z, +09:00)가 있으면 이 PC 의 현지 시각으로 환산한다."""
    m = _TS_ISO.search(b)
    if m:
        y, mo, d, hh, mi, ss, tz = m.groups()
        try:
            t = pd.Timestamp(int(y), int(mo), int(d), int(hh), int(mi), int(ss))
        except ValueError:
            return None, False
        if tz:
            z = tz.decode()
            off = 0 if z == 'Z' else (1 if z[0] == '+' else -1) * (int(z[1:3]) * 60 + int(z[-2:]))
            utc = t - pd.Timedelta(minutes=off)
            local = pd.Timestamp(utc.tz_localize('UTC').tz_convert(_local_tz()).tz_localize(None))
            return local, True
        return t, False
    m = _TS_DMY.search(b)
    if m:
        d, mon, y, hh, mi, ss = m.groups()
        try:
            return pd.Timestamp(int(y), _MON[mon.decode()[:3]], int(d), int(hh), int(mi), int(ss)), False
        except (ValueError, KeyError):
            return None, False
    m = _TS_SYS.search(b)
    if m:
        mon, d, hh, mi, ss = m.groups()
        try:
            return pd.Timestamp(year, _MON[mon.decode()], int(d), int(hh), int(mi), int(ss)), False
        except (ValueError, KeyError):
            return None, False
    return None, False


def _local_tz():
    import datetime as _dt
    return _dt.datetime.now().astimezone().tzinfo


def _slice_unknown(path, lo, hi, names_rx, year):
    """형식을 등록하지 않은 로그: 시각이 [lo, hi] 안이고 케이스의 VM · 호스트 이름이 들어 있는 줄만 남긴다.
    시각이 없는 줄(여러 줄짜리 기록의 다음 줄)은 바로 앞 시각 줄을 따른다. CSV 는 첫 줄(머리글)을 남긴다.
    반환: (남긴 줄 목록, 첫 시각, 마지막 시각, 시간대 환산 여부) 또는 None."""
    kept, times, conv, follow = [], [], False, False
    is_csv = path.lower().endswith('.csv')
    header = None
    with open(path, 'rb') as f:
        for k, b in enumerate(f):
            if k == 0 and is_csv:
                header = b
                continue
            t, c = _line_time(b, year)
            if t is None:
                if follow:
                    kept.append(b)
                continue
            follow = lo <= t <= hi and bool(names_rx.search(b))
            if follow:
                kept.append(b)
                times.append(t)
                conv = conv or c
    if not times:
        return None
    return ([header] if header is not None else []) + kept, min(times), max(times), conv


def split_case(case, sources, profiles, ents, out_root, base_dir=None, log=print, date=None, extra=None, unknown=None):
    """원본 파일에서 이 케이스의 ESX 호스트 · VM 기록 중 시각 구간(앞뒤 여유 포함) 행만 잘라 out_root/<케이스명>/ 에 저장한다.

    남기는 행. 행의 대상 이름은 입력 프로파일의 대상 열 · 키다(VCF Operations 의 Name, Key:Value 로그의 Hostname, CSV 의 VM).
      1. 대상 이름이 케이스의 호스트 이름과 같은 행: ESX 호스트 자신의 기록. 도메인을 붙인 이름도 같은 호스트로 본다.
      2. 대상 이름이 이 케이스의 VM 인 행. VM 은 리포트와 같은 규칙(ingest.load_case)으로 고른다.
    extra: [(종류, 원본 행 목록)] — 이 케이스에 연결된 잡 TAT 원장 행(tat) · phase 소요 시간 행(phase). CSV 로 저장한다.
    unknown: 형식을 등록하지 않은 로그 파일 경로. 줄마다 시각과 VM · 호스트 이름을 찾아 이 케이스의 줄만 etc 로 저장한다(_slice_unknown).
    파일 이름: <케이스명>-<종류>-<정리 날짜 YYYYMMDD><원본 확장자>. 종류는 내용으로 정한다:
      ops = VCF Operations 지표 export, tat = 잡 TAT 원장, phase = phase 소요 시간. 같은 종류가 여러 파일이면 <종류>-01, -02 …
      그 밖의 로그는 <케이스명>-etc-<번호>-<날짜>. 번호는 종류 안에서 구간 안 첫 시각 순서다.
    잘라낸 파일은 원본과 같은 형식(열 · 표기)을 유지한다. 폴더는 매번 새로 만든다(덮어쓰기)."""
    lo = case['start'] - pd.Timedelta(seconds=case.get('margin_s', 0))
    hi = case['end'] + pd.Timedelta(seconds=case.get('margin_s', 0))
    date = date or time.strftime('%Y%m%d')
    dest = os.path.join(out_root, _safe_dir(case['id']))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    hosts = [h.strip() for h in re.split(r'[;,]', str(case.get('host') or '')) if h.strip()]
    keep_row = _decider(hosts, {e.lower() for e in ents})
    parts, dropped, kept = [], 0, set()
    for src in sources:
        prof = profiles[src['profile']]
        rel = os.path.relpath(src['path'], base_dir) if base_dir else os.path.basename(src['path'])
        if prof['type'] == 'csv_wide':
            df = pd.read_csv(src['path'], dtype=str, keep_default_na=False, encoding=prof.get('encoding', 'utf-8-sig'))
            t = ingest.parse_time(df[prof['time_column']], prof.get('time_format'), prof.get('timezone'))
            in_t = (t >= lo) & (t <= hi)
            df, t = df[in_t], t[in_t]
            ec = prof.get('entity_column')
            if ec in df.columns:
                subj = df[ec].astype(str).str.strip()
                keep = subj.map(keep_row).astype(bool)
                kept |= set(subj[keep])
            else:
                keep = pd.Series(True, index=df.index)       # 대상 열이 없는 파일은 구간만 자른다
            dropped += int(len(df) - keep.sum())
            if keep.any():
                parts.append((t[keep].min(), rel, prof, t[keep], df[keep], None, prof.get('short_name') or 'etc'))
        else:   # kv_text: 줄 단위로 거른다. 머리 줄(시각 키가 없는 첫 줄)은 그대로 둔다
            tkey, ekey = prof['time_key'], prof.get('entity_key')
            tre = re.compile(re.escape(tkey) + r':\s*([0-9T:\-\./ ]+?)(?=,|$)')
            ere = re.compile(re.escape(ekey) + r':\s*([^,\s"\]]+)') if ekey else None
            lines, times = [], []
            with open(src['path'], encoding=prof.get('encoding', 'utf-8'), errors='replace') as f:
                for k, line in enumerate(f):
                    mt = tre.search(line)
                    if not mt:
                        if k == 0:
                            lines.append(line)
                        continue
                    tv = pd.to_datetime(mt.group(1).strip(), format=prof.get('time_format'), errors='coerce')
                    if pd.isna(tv) or not lo <= tv <= hi:
                        continue
                    if ere:
                        me = ere.search(line)
                        who = me.group(1) if me else ''
                        if not keep_row(who):
                            dropped += 1
                            continue
                        kept.add(who)
                    lines.append(line)
                    times.append(tv)
            if not times:
                continue
            tt = pd.Series(times)
            parts.append((tt.min(), rel, prof, tt, None, lines, prof.get('short_name') or 'etc'))
    for kind, rows in extra or []:          # 잡 TAT 원장 · phase 소요 시간: 이 케이스에 연결된 행만
        if not rows:
            continue
        cols = list(dict.fromkeys(k for r in rows for k in r))
        tcol = 'PSStartTime' if 'PSStartTime' in cols else ('JobStart_KST' if 'JobStart_KST' in cols else None)
        tt = pd.Series([str(r.get(tcol, '')) for r in rows]) if tcol else pd.Series([''] * len(rows))
        files = sorted({os.path.relpath(r['__file'], base_dir) if base_dir and r.get('__file') else '' for r in rows} - {''})
        desc = {'tat': '잡 TAT 원장', 'phase': 'phase 소요 시간'}[kind]
        parts.append((pd.Timestamp(case['start']), ', '.join(files) or kind, {'source_system': desc},
                      tt, pd.DataFrame([{k: r.get(k, '') for k in cols if k != '__file'} for r in rows]), None, kind))
    conv_any = False
    if unknown:                           # 형식을 등록하지 않은 로그: 시각과 VM · 호스트 이름이 있는 줄만
        names = sorted({str(e) for e in ents if str(e)} | set(hosts), key=len, reverse=True)
        if names:
            names_rx = re.compile(rb'(?<![A-Za-z0-9_\-])(' + b'|'.join(re.escape(n.encode()) for n in names) + rb')(?![A-Za-z0-9_\-])', re.I)
            for path in unknown:
                got = _slice_unknown(path, lo, hi, names_rx, pd.Timestamp(case['start']).year)
                if not got:
                    continue
                lines_b, t0, t1, conv = got
                conv_any = conv_any or conv
                rel = os.path.relpath(path, base_dir) if base_dir else os.path.basename(path)
                parts.append((t0, rel, {'source_system': '형식 미등록 (줄의 시각 · VM 이름으로 잘라냄)'},
                              pd.Series([t0, t1]), None, lines_b, 'etc'))
    manifest = []
    by_kind = {}
    for p in sorted(parts, key=lambda x: (x[0], x[1])):
        by_kind.setdefault(p[6], []).append(p)
    for kind in [k for k in NAMED_KINDS if k in by_kind] + (['etc'] if 'etc' in by_kind else []):
        group = by_kind[kind]
        for k, (_, rel, prof, tt, part, lines, _kind) in enumerate(group, start=1):
            ext = '.csv' if kind in ('tat', 'phase') else os.path.splitext(rel)[1]
            num = f'-{k:02d}' if (kind == 'etc' or len(group) > 1) else ''
            name = f'{_safe_dir(case["id"])}-{kind}{num}-{date}{ext}'
            if part is not None:
                part.to_csv(os.path.join(dest, name), index=False, encoding='utf-8-sig')
            elif lines and isinstance(lines[0], bytes):        # 형식 미등록 로그: 원본 바이트 그대로
                with open(os.path.join(dest, name), 'wb') as f:
                    f.writelines(lines)
            else:
                with open(os.path.join(dest, name), 'w', encoding='utf-8') as f:
                    f.writelines(lines)
            manifest.append({'파일': name, '종류': kind, '원본': rel, '형식': prof.get('source_system', ''),
                             '행 수': int(len(tt)), '첫 시각': str(tt.min()) if len(tt) else '', '마지막 시각': str(tt.max()) if len(tt) else ''})
    pd.DataFrame(manifest, columns=['파일', '종류', '원본', '형식', '행 수', '첫 시각', '마지막 시각']).to_csv(
        os.path.join(dest, 'manifest.csv'), index=False, encoding='utf-8-sig')
    own = {n for n in kept if n and any(ingest.same_host(n, h) for h in hosts)}
    vms = sorted(n for n in kept if n and n not in own)
    with open(os.path.join(dest, 'case.txt'), 'w', encoding='utf-8') as f:
        f.write(f'케이스명: {case["id"]}\n테스트 그룹: {case.get("group", "")}\n호스트: {case.get("host", "")}\n'
                f'VM: {", ".join(vms) or "-"}\n'
                f'호스트 자신의 기록: {", ".join(sorted(own)) or "-"}\n잡 시작 시각: {case["start"]}\n잡 종료 시각: {case["end"]}'
                + (' (자동 감지)' if case.get('end_auto') else '') + f'\n저장 구간: {lo} ~ {hi} (앞뒤 {case.get("margin_s", 0) // 60}분 포함)\n'
                f'정리한 날짜: {date}\n'
                f'남긴 기록: 위 VM 과 호스트 자신의 기록 (VM 은 리포트와 같은 규칙으로 고름)\n'
                f'구간 안에서 뺀 다른 호스트 · VM 의 행: {dropped:,}개\n'
                + ('형식 미등록 로그의 시각 중 시간대 표기(Z, +09:00 등)가 있는 시각은 이 PC 의 현지 시각으로 환산해 구간을 정함\n' if conv_any else ''))
    log(f'[정리] {case["id"]}: 파일 {len(manifest)}개 → {dest}' + (f' (구간 안 다른 호스트 · VM 행 {dropped:,}개 제외)' if dropped else ''))
    return dest, manifest


def check_collection(df, case):
    """잡 실행 구간 안의 수집 끊김 점검. 수집 시스템 · VM 마다 표본 간격을 구해 그보다 크게 빈 구간을 찾는다.
    반환: (문제 목록, 시트 비고용 요약 문장). 문제 목록 항목: VM · 수집 시스템 · 종류 · 시작 · 끝 · 길이(초) · 정상 간격(초)."""
    start, end = case['start'], case['end']
    issues, info = [], []
    for (e, sysname), g in df.groupby(['entity', 'source_system']):
        t = g['t'].drop_duplicates().sort_values()
        if len(t) < 3:
            issues.append({'vm': e, 'system': sysname, 'kind': '표본 부족', 'from': start, 'to': end,
                           'sec': (end - start).total_seconds(), 'interval': None})
            continue
        step = float(t.diff().dt.total_seconds().median())
        limit = max(step * 2.5, step + 60)
        info.append((sysname, step))
        run = t[(t >= start) & (t <= end)]
        if run.empty:
            issues.append({'vm': e, 'system': sysname, 'kind': '실행 구간 데이터 없음', 'from': start, 'to': end,
                           'sec': (end - start).total_seconds(), 'interval': step})
            continue
        if (run.iloc[0] - start).total_seconds() > limit and t.iloc[0] > start - pd.Timedelta(seconds=step):
            issues.append({'vm': e, 'system': sysname, 'kind': '시작 후 수집 지연', 'from': start, 'to': run.iloc[0],
                           'sec': (run.iloc[0] - start).total_seconds(), 'interval': step})
        if (end - run.iloc[-1]).total_seconds() > limit and t.iloc[-1] < end + pd.Timedelta(seconds=step):
            issues.append({'vm': e, 'system': sysname, 'kind': '종료 전 수집 중단', 'from': run.iloc[-1], 'to': end,
                           'sec': (end - run.iloc[-1]).total_seconds(), 'interval': step})
        d = run.diff().dt.total_seconds()
        for i in d[d > limit].index:
            prev = run[run.index < i].iloc[-1]
            issues.append({'vm': e, 'system': sysname, 'kind': '수집 끊김', 'from': prev, 'to': run[i],
                           'sec': d[i], 'interval': step})
    steps = {}
    for sysname, step in info:                     # 같은 수집 시스템이라도 파일마다 간격이 다를 수 있다: 모두 적는다
        steps.setdefault(sysname.split(' (')[0], set()).add(round(step))
    basis = ', '.join(f'{s} {"·".join(str(v) for v in sorted(vs))}초 간격' for s, vs in steps.items())
    stamp = f'[수집 점검 {pd.Timestamp.now():%Y-%m-%d}]'
    if not issues:
        return issues, f'{stamp} 끊김 없음 ({basis})'
    lines = [f'{stamp} 문제 {len(issues)}건 ({basis})']
    for x in issues:
        lines.append(f'- {x["vm"]} {x["system"].split(" (")[0]}: {x["kind"]} {x["from"]:%m-%d %H:%M:%S}~{x["to"]:%H:%M:%S}'
                     f' ({x["sec"] / 60:.0f}분)')
    return issues, '\n'.join(lines)
