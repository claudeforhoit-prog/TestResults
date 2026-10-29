"""잡 실행 구간: 종료 시각 자동 감지와 TC 별 데이터 정리(원본에서 그 TC 구간만 잘라 저장).

종료 시각 자동 감지
  케이스 정보의 '잡 종료 시각' 을 비우면, 잡 시작 이후 CPU 사용률이 기준값 미만으로 IDLE_MIN_S 이상 유지되기
  시작한 시각을 종료 시각으로 쓴다. CPU 사용률은 그 케이스 VM 들의 같은 분 값 중 최댓값을 5분 이동 중앙값으로 고른다.
  기준값은 IDLE_PCT 와 실행 중 p95 의 IDLE_REL 중 큰 값이다. 잡 시작 직후 MIN_RUN_S 동안은 판정하지 않는다.
"""
import os
import re
import shutil

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


def split_case(case, sources, profiles, ents, out_root, base_dir=None, log=print):
    """원본 파일에서 이 케이스의 VM · 시각 구간(앞뒤 여유 포함) 행만 잘라 out_root/<케이스명>/ 에 저장한다.
    잘라낸 파일은 원본과 같은 형식(열 · 표기)을 유지한다. 폴더는 매번 새로 만든다(덮어쓰기)."""
    lo = case['start'] - pd.Timedelta(seconds=case.get('margin_s', 0))
    hi = case['end'] + pd.Timedelta(seconds=case.get('margin_s', 0))
    dest = os.path.join(out_root, _safe_dir(case['id']))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    ents_l = {e.lower() for e in ents}
    manifest = []
    for src in sources:
        prof = profiles[src['profile']]
        rel = os.path.relpath(src['path'], base_dir) if base_dir else os.path.basename(src['path'])
        name = rel.replace(os.sep, '__')
        if prof['type'] == 'csv_wide':
            df = pd.read_csv(src['path'], dtype=str, keep_default_na=False, encoding=prof.get('encoding', 'utf-8-sig'))
            t = ingest.parse_time(df[prof['time_column']], prof.get('time_format'), prof.get('timezone'))
            ec = prof.get('entity_column')
            keep = (t >= lo) & (t <= hi)
            if ec:
                keep &= df[ec].str.lower().isin(ents_l)
            part = df[keep]
            if part.empty:
                continue
            part.to_csv(os.path.join(dest, name), index=False, encoding='utf-8-sig')
            tt = t[keep]
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
                    if ere:
                        me = ere.search(line)
                        if not me or me.group(1).lower() not in ents_l:
                            continue
                    tv = pd.to_datetime(mt.group(1).strip(), format=prof.get('time_format'), errors='coerce')
                    if pd.notna(tv) and lo <= tv <= hi:
                        lines.append(line)
                        times.append(tv)
            if not times:
                continue
            with open(os.path.join(dest, name), 'w', encoding='utf-8') as f:
                f.writelines(lines)
            tt = pd.Series(times)
        manifest.append({'파일': name, '원본': rel, '형식': prof.get('source_system', src['profile']),
                         '행 수': int(len(tt)), '첫 시각': str(tt.min()), '마지막 시각': str(tt.max())})
    pd.DataFrame(manifest).to_csv(os.path.join(dest, 'manifest.csv'), index=False, encoding='utf-8-sig')
    with open(os.path.join(dest, 'case.txt'), 'w', encoding='utf-8') as f:
        f.write(f'케이스명: {case["id"]}\n테스트 그룹: {case.get("group", "")}\n호스트: {case.get("host", "")}\n'
                f'VM: {", ".join(sorted(ents))}\n잡 시작 시각: {case["start"]}\n잡 종료 시각: {case["end"]}'
                + (' (자동 감지)' if case.get('end_auto') else '') + f'\n저장 구간: {lo} ~ {hi} (앞뒤 {case.get("margin_s", 0) // 60}분 포함)\n')
    log(f'[정리] {case["id"]}: 파일 {len(manifest)}개 → {dest}')
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
