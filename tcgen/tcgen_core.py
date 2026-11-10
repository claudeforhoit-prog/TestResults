# -*- coding: utf-8 -*-
"""
tcgen_core.py - 테스트 실행 명령 생성기의 계산과 출력 (화면 코드와 분리)

- 설정, 실행 명령 목록, 모니터링 명령 목록, 테스트 케이스 표의 저장과 불러오기 (데이터 폴더, 생성기 폴더와 분리)
- 백업, 백업 불러오기, 데이터 폴더 변경, 예전 판 데이터 옮기기
- 테스트 케이스 시각 계산: 잡 시작, 모니터링 시작(잡 시작 N분 전), 모니터링 중지
- crontab 줄 생성: 변수 <vm이름> <실행시각> 치환, % 처리, 줄 끝 표식
- 붙여넣기 블록 생성: 점프 VM 최초 설정, VM 등록(점프 VM 에서 ssh), ESX 등록(root SSH 창)
- 테스트 기록 CSV 행 추가, 로그 CSV 저장

블록은 ASCII 문자만 쓴다. 점프 VM 의 셸이 csh(tcsh) 이든 bash 이든 같은 블록이 동작하도록
here-document 끝 표시는 따옴표 없이 쓰고(<< TCEOF ... TCEOF), 본문의 \\, $, ` 앞에는 역슬래시를 넣는다.
tcsh(6.18.01 ~ 최신 소스의 heredoc 처리 동일)와 sh, bash 는 이 세 글자 앞의 역슬래시만 풀어 원래 글을 넘긴다.
비밀번호는 here-document 밖에서 ~/.tcgen/setpw 로 입력받는다. 붙여넣을 때 줄바꿈이 CR+LF 로 들어와 남은
빈 줄이 비밀번호로 읽히지 않도록, setpw 는 남은 입력을 먼저 비우고 빈 입력이면 다시 묻는다.

% 처리: VM 의 cron(cronie)은 명령 안의 % 를 줄바꿈으로, \\% 를 % 로 바꾼다. 그래서 VM 줄은 % 를 \\% 로 쓴다.
ESX 의 crond(busybox)는 명령을 바꾸지 않고 sh -c 로 실행하므로 ESX 줄은 입력한 그대로 쓴다.
"""
import codecs
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import sys

VERSION = "1.0"
KST_TO_UTC = dt.timedelta(hours=9)
CHUNK = 100            # 붙여넣는 글 한 줄에 담는 명령 조각 길이(문자 수)
NONE_JOB = "(없음)"

SETTINGS_FILE = "settings.json"
RUN_FILE = "run_commands.csv"
MON_FILE = "monitor_commands.csv"
CASES_FILE = "test_cases.json"       # 탭 3 테스트 케이스 표와 적용 체크 상태

CASE_DEFAULTS = {
    "applied": False,      # 적용 체크: 이미 등록한 케이스. [명령어 생성] 에서 빠진다
    "name": "", "esx": "", "pvm": "", "svm": "", "pvm_job": "(없음)", "svm_job": "(없음)",
    "interval": "", "start": "", "monitors": [], "stop_mode": "auto", "stop_time": "",
}
DEFAULT_CASE_DIR = "testcases"
DEFAULT_RECORD = "test_record.csv"

DEFAULT_SETTINGS = {
    "record_csv": "",          # 비우면 <생성기 폴더>/testcases/test_record.csv
    "log_dir": "",             # 비우면 <생성기 폴더>/testcases
    "vm_user": "",             # 비우면 점프 VM 계정 그대로 ssh
    "mon_lead_min": 5,         # 모니터링 시작 = 잡 시작 N분 전
    "stop_lead_min": 10,       # 모니터링 중지(자동) = 다음 케이스 잡 시작 N분 전
    "pvm_suffix": "ev01",      # PVM 이름 = ESX 호스트 이름 + 이 값
    "svm_suffix": "ev02",      # SVM 이름 = ESX 호스트 이름 + 이 값
}

RUN_FIELDS = ["app", "name", "desc", "cmd"]
RUN_HEADERS = ["App명", "명령어 명", "잡 조건 설명", "경로/실행 명령"]
MON_FIELDS = ["app", "name", "desc", "where", "start", "stop"]
MON_HEADERS = ["App명", "명령어 명", "설명", "실행 위치", "시작 명령", "중지 명령"]

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VAR_HOST_RE = re.compile(r"<\s*vm\s*이름\s*>", re.IGNORECASE)
VAR_START_RE = re.compile(r"<\s*실행\s*시각\s*>")


# ------------------------------------------------------------------ 파일
def app_dir():
    """생성기 폴더: exe 로 빌드했으면 exe 가 있는 폴더, 아니면 이 파일이 있는 폴더."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_paths(base):
    d = os.path.join(base, DEFAULT_CASE_DIR)
    return os.path.join(d, DEFAULT_RECORD), d


def resolve_paths(base, s):
    """설정의 기록 CSV 경로와 로그 폴더를 절대 경로로 바꾼 사본을 반환한다. 빈 값은 기본 경로."""
    r = dict(s)
    rec, logd = default_paths(base)
    for key, dflt in (("record_csv", rec), ("log_dir", logd)):
        p = (r.get(key) or "").strip()
        if not p:
            p = dflt
        elif not os.path.isabs(p):
            p = os.path.join(base, p)
        r[key] = os.path.normpath(p)
    return r


def load_settings(base):
    s = dict(DEFAULT_SETTINGS)
    p = os.path.join(base, SETTINGS_FILE)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                s.update(data)
        except (OSError, ValueError):
            pass
    for k in ("mon_lead_min", "stop_lead_min"):
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            s[k] = DEFAULT_SETTINGS[k]
    for k in ("record_csv", "log_dir", "vm_user", "pvm_suffix", "svm_suffix"):
        s[k] = "" if s.get(k) is None else str(s[k])
    return s


def _atomic_write(path, text, encoding):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def save_settings(base, s):
    out = dict(s)
    rec, logd = default_paths(base)
    for key, dflt in (("record_csv", rec), ("log_dir", logd)):
        if os.path.normcase(os.path.normpath(out.get(key) or "")) == os.path.normcase(dflt):
            out[key] = ""          # 기본 경로는 비워 저장해 생성기 폴더를 옮겨도 따라가게 한다
    _atomic_write(os.path.join(base, SETTINGS_FILE), json.dumps(out, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _decode(data):
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8"), "utf-8"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp949"), "cp949"


def load_rows(path, fields, headers):
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        text, _ = _decode(f.read())
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    idx = {h.strip(): i for i, h in enumerate(rows[0])}
    out = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = {}
        for fld, h in zip(fields, headers):
            i = idx.get(h)
            d[fld] = row[i].strip() if i is not None and i < len(row) else ""
        out.append(d)
    return out


def save_rows(path, fields, headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(k, "") for k in fields])
    _atomic_write(path, buf.getvalue(), "utf-8-sig")


def _clean_case(c):
    d = {k: c.get(k, v) for k, v in CASE_DEFAULTS.items()}
    for k in ("name", "esx", "pvm", "svm", "pvm_job", "svm_job", "interval", "start", "stop_mode", "stop_time"):
        d[k] = "" if d[k] is None else str(d[k])
    d["stop_mode"] = d["stop_mode"] if d["stop_mode"] in ("auto", "manual", "none") else "auto"
    d["applied"] = bool(d["applied"])
    mons = []
    for m in d.get("monitors") or []:
        if isinstance(m, dict) and m.get("name"):
            mons.append({"name": str(m["name"]), "targets": [t for t in (m.get("targets") or []) if t in ("PVM", "SVM", "ESX")]})
    d["monitors"] = mons
    return d


def load_cases(base):
    """탭 3 테스트 케이스 표(적용 체크 포함)를 불러온다. 파일이 없으면 빈 목록."""
    p = os.path.join(base, CASES_FILE)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig") as f:
        data = json.load(f)
    rows = data.get("cases", []) if isinstance(data, dict) else data
    return [_clean_case(c) for c in rows if isinstance(c, dict)]


def save_cases(base, cases):
    data = {"version": 1, "cases": [_clean_case(c) for c in cases]}
    _atomic_write(os.path.join(base, CASES_FILE), json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def backup_settings(base, now=None):
    """설정 파일, 명령 목록, 테스트 케이스 표를 <생성기 폴더>/backup/<년월일시분초>/ 에 복사한다. (폴더, 복사한 파일 목록) 반환."""
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    dest = os.path.join(base, "backup", ts)
    os.makedirs(dest, exist_ok=True)
    copied = []
    for fn in (SETTINGS_FILE, RUN_FILE, MON_FILE, CASES_FILE):
        src = os.path.join(base, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fn))
            copied.append(fn)
    return dest, copied


# ------------------------------------------------------------------ 데이터 폴더 (생성기 폴더와 분리)
# 데이터 파일은 생성기 폴더가 아니라 데이터 폴더에 둔다. 생성기를 다시 빌드하거나 폴더를 바꿔도 남게 하려는 것이다.
# 데이터 폴더 기본 위치: <사용자 폴더>/tcgen_data. 위치를 바꾸면 그 경로를 설정 파일(config.json)에 적는다.
# 설정 파일 위치: Windows 는 %APPDATA%\tcgen\config.json, 그 밖에는 ~/.config/tcgen/config.json
DATA_FILES = (SETTINGS_FILE, RUN_FILE, MON_FILE, CASES_FILE)
CONFIG_FILE = "config.json"
BACKUP_PREFIX = "tcgen_backup_"


def config_dir():
    d = os.environ.get("TCGEN_CONFIG_DIR")          # 시험용
    if d:
        return d
    if os.name == "nt" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "tcgen")
    return os.path.join(os.path.expanduser("~"), ".config", "tcgen")


def default_data_dir():
    return os.environ.get("TCGEN_DEFAULT_DATA_DIR") or os.path.join(os.path.expanduser("~"), "tcgen_data")


def load_config():
    try:
        with open(os.path.join(config_dir(), CONFIG_FILE), encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    _atomic_write(os.path.join(d, CONFIG_FILE), json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")


def data_dir(cfg=None):
    cfg = load_config() if cfg is None else cfg
    d = (cfg.get("data_dir") or "").strip()
    return os.path.normpath(d) if d else os.path.normpath(default_data_dir())


def has_data(d):
    return any(os.path.isfile(os.path.join(d, f)) for f in DATA_FILES)


def same_dir(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _inside(path, folder):
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def copy_data(src, dst, dirs=(), overwrite=True):
    """src 의 데이터 파일 4개와 dirs 에 적은 하위 폴더를 dst 로 복사한다. 복사한 항목 이름 목록을 반환한다.
    하위 폴더(테스트 기록, 로그, 백업) 안의 파일은 dst 에 같은 이름이 없을 때만 복사해 기록을 덮어쓰지 않는다."""
    os.makedirs(dst, exist_ok=True)
    done = []
    for fn in DATA_FILES:
        s = os.path.join(src, fn)
        if os.path.isfile(s) and (overwrite or not os.path.exists(os.path.join(dst, fn))):
            shutil.copy2(s, os.path.join(dst, fn))
            done.append(fn)
    for sub in dirs:
        sd = os.path.join(src, sub)
        if not os.path.isdir(sd):
            continue
        n = skipped = 0
        for cur, _, files in os.walk(sd):
            rel = os.path.relpath(cur, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            for fn in files:
                t = os.path.join(dst, rel, fn)
                if os.path.exists(t):
                    skipped += 1
                    continue
                shutil.copy2(os.path.join(cur, fn), t)
                n += 1
        if n or skipped:
            done.append("%s%s 파일 %d개%s" % (sub, os.sep, n, (" (같은 이름이 있어 건너뛴 파일 %d개)" % skipped) if skipped else ""))
    return done


def _remap_settings_paths(old, new):
    """settings.json 의 기록 CSV, 로그 폴더가 예전 폴더 안을 가리키면 새 폴더의 같은 위치로 바꾼다."""
    if not os.path.isfile(os.path.join(new, SETTINGS_FILE)):
        return
    s = load_settings(new)
    changed = False
    for key in ("record_csv", "log_dir"):
        v = (s.get(key) or "").strip()
        if v and os.path.isabs(v) and _inside(v, old):
            s[key] = os.path.join(new, os.path.relpath(v, old))
            changed = True
    if changed:
        save_settings(new, resolve_paths(new, s))


def migrate_from(old, new, now=None):
    """예전 판이 생성기 폴더(old)에 저장한 데이터를 데이터 폴더(new)로 복사한다. new 에 데이터가 없을 때만 한다.
    복사한 뒤 생성기 폴더의 데이터 파일 4개는 old/moved_to_data_folder_<시각> 으로 옮겨, 어느 파일을 쓰는지 헷갈리지 않게 한다
    (지우지 않는다. 옮기지 못하면 그대로 둔다)."""
    if same_dir(old, new) or has_data(new) or not has_data(old):
        return []
    done = copy_data(old, new, dirs=(DEFAULT_CASE_DIR, "backup"))
    _remap_settings_paths(old, new)
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    aside = os.path.join(old, "moved_to_data_folder_" + ts)
    try:
        os.makedirs(aside, exist_ok=True)
        for fn in DATA_FILES:
            if os.path.isfile(os.path.join(old, fn)):
                os.replace(os.path.join(old, fn), os.path.join(aside, fn))
    except OSError:
        pass
    return done


def change_data_dir(cur, new, copy_current=True):
    """데이터 폴더를 new 로 바꾸고 config.json 에 적는다. copy_current 이면 현재 데이터를 new 로 복사한다(new 에 데이터가 없을 때)."""
    os.makedirs(new, exist_ok=True)
    done = []
    if copy_current and not has_data(new):
        done = copy_data(cur, new, dirs=(DEFAULT_CASE_DIR, "backup"))
        _remap_settings_paths(cur, new)
    cfg = load_config()
    cfg["data_dir"] = os.path.normpath(new)
    save_config(cfg)
    return done


def backup_to(base, parent, now=None):
    """데이터 폴더(base)의 데이터 파일 4개와 testcases 폴더를 parent 아래 tcgen_backup_<년월일시분초> 폴더로 복사한다."""
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    dest = os.path.join(parent, BACKUP_PREFIX + ts)
    return dest, copy_data(base, dest, dirs=(DEFAULT_CASE_DIR,))


def restore_from(src, base, now=None):
    """백업 폴더(src)의 데이터 파일로 데이터 폴더(base)의 파일을 바꾼다. 바꾸기 전 현재 파일은 base/backup/<시각>_before_restore 에
    복사한다. testcases 폴더 안 파일은 같은 이름이 없을 때만 복사한다. (복원 전 백업 폴더, 복사 항목) 반환."""
    if same_dir(src, base):
        raise ValueError("현재 데이터 폴더와 같은 폴더임: %s" % src)
    if not has_data(src):
        raise ValueError("이 폴더에는 tcgen 데이터 파일(%s)이 없음: %s" % (", ".join(DATA_FILES), src))
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    keep = os.path.join(base, "backup", ts + "_before_restore")
    copy_data(base, keep)
    done = copy_data(src, base, dirs=(DEFAULT_CASE_DIR,))
    _remap_settings_paths(src, base)
    return keep, done


# ------------------------------------------------------------------ 시각
def parse_dt(text):
    t = (text or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            d = dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
        if d.second:
            raise ValueError("cron 은 분 단위로 실행됨. 초는 00 으로 입력: '%s'" % text)
        return d
    raise ValueError("시각 형식이 맞지 않음: '%s' (예: 2026-11-09 10:00:00)" % text)


def fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fmt_short(d):
    return d.strftime("%m-%d %H:%M")


def parse_interval(text):
    t = (text or "").strip()
    if not t or t == "-":
        return None
    m = re.fullmatch(r"(\d+)(?::(\d{1,2}))?", t)
    if not m or int(m.group(2) or 0) >= 60:
        raise ValueError("간격은 시간:분 으로 입력 (예: 12:30, 8): '%s'" % text)
    td = dt.timedelta(hours=int(m.group(1)), minutes=int(m.group(2) or 0))
    if td.total_seconds() <= 0:
        raise ValueError("간격은 0 보다 커야 함: '%s'" % text)
    return td


def fmt_interval(td):
    total = int(td.total_seconds() // 60)
    return "%d:%02d" % (total // 60, total % 60)


def recalc_starts(cases, from_index=1):
    """from_index 행부터 아래로, 간격이 있는 행의 잡 시작 = 윗줄 잡 시작 + 간격 으로 다시 계산한다."""
    for i in range(max(1, from_index), len(cases)):
        try:
            iv = parse_interval(cases[i].get("interval"))
            prev = parse_dt(cases[i - 1].get("start"))
        except ValueError:
            continue
        if iv is None:
            continue
        cases[i]["start"] = fmt_dt(prev + iv)


def interval_from_starts(cases, i):
    """i 행의 잡 시작을 직접 고쳤을 때 그 행 간격을 윗줄과의 차이로 맞춘다. 첫 행 간격은 그대로 둔다."""
    if i <= 0:
        return
    try:
        cur = parse_dt(cases[i].get("start"))
        prev = parse_dt(cases[i - 1].get("start"))
    except ValueError:
        cases[i]["interval"] = ""
        return
    diff = cur - prev
    cases[i]["interval"] = fmt_interval(diff) if diff.total_seconds() > 0 else ""


def selected_monitors(c):
    return [m for m in (c.get("monitors") or []) if m.get("targets")]


def case_schedule(cases, mon_lead_min, stop_lead_min):
    """케이스마다 잡 시작, 모니터링 시작, 모니터링 중지 시각과 시각 오류를 반환한다."""
    starts = []
    errs = []
    for c in cases:
        try:
            starts.append(parse_dt(c.get("start")))
            errs.append("")
        except ValueError as e:
            starts.append(None)
            errs.append(str(e))
    out = []
    n = len(cases)
    for i, c in enumerate(cases):
        r = {"job": starts[i], "mon_start": None, "mon_stop": None, "start_error": errs[i], "stop_error": ""}
        if starts[i] is None:
            out.append(r)
            continue
        r["mon_start"] = starts[i] - dt.timedelta(minutes=mon_lead_min)
        mode = c.get("stop_mode") or "auto"
        if mode == "none":
            pass
        elif mode == "manual":
            try:
                r["mon_stop"] = parse_dt(c.get("stop_time"))
            except ValueError as e:
                r["stop_error"] = "모니터링 중지 " + str(e)
        elif i + 1 < n:
            if starts[i + 1] is not None:
                r["mon_stop"] = starts[i + 1] - dt.timedelta(minutes=stop_lead_min)
            else:
                r["stop_error"] = "다음 케이스 잡 시작 시각이 맞지 않아 자동 중지 시각을 정할 수 없음"
        else:
            try:
                iv = parse_interval(c.get("interval"))
            except ValueError:
                iv = None
            if iv:
                r["mon_stop"] = starts[i] + iv - dt.timedelta(minutes=stop_lead_min)
            else:
                r["stop_error"] = ("마지막 케이스에 간격이 없어 자동 중지 시각을 정할 수 없음. "
                                   "간격을 넣거나, 모니터링 중지 칸에서 직접 입력 또는 중지 없음을 고를 것")
        if r["mon_stop"] is not None and r["mon_stop"] <= r["mon_start"]:
            r["stop_error"] = "모니터링 중지 시각이 모니터링 시작 시각보다 빠르거나 같음"
        out.append(r)
    return out


# ------------------------------------------------------------------ 명령과 crontab 줄
def check_command(cmd, label):
    text = (cmd or "").strip()
    if not text:
        return ["%s: 명령이 비어 있음" % label]
    errs = []
    if "!" in text:
        errs.append("%s: ! 문자는 쓸 수 없음 (csh 가 붙여넣는 글의 ! 를 이전 명령 불러오기로 처리)" % label)
    rest = VAR_START_RE.sub("", VAR_HOST_RE.sub("", text))
    if "\t" in rest:
        errs.append("%s: 탭 문자가 있음. 공백으로 바꿀 것 (csh 가 붙여넣는 글의 탭을 자동 완성으로 처리)" % label)
    if any(ord(ch) > 126 or (ord(ch) < 32 and ch != "\t") for ch in rest):
        errs.append("%s: 한글 같은 ASCII 밖의 문자나 줄바꿈이 있음 (한글은 변수 <vm이름>, <실행시각> 에만 쓸 수 있음)" % label)
    return errs


def render_command(cmd, esx=False):
    """등록한 명령을 crontab 명령 부분으로 바꾼다. 변수를 쓴 명령에는 앞에 값 계산 명령을 추가한다."""
    text = (cmd or "").strip()
    uses_host = bool(VAR_HOST_RE.search(text))
    uses_start = bool(VAR_START_RE.search(text))
    body = VAR_START_RE.sub("${TC_START}", VAR_HOST_RE.sub("${TC_HOST}", text))
    if not esx:
        body = re.sub(r"(?<!\\)%", r"\\%", body)
    pre = ""
    if uses_host:
        pre += "TC_HOST=`hostname -s`; "
    if uses_start:
        # \% 는 cronie 에서는 % 로 바뀌고, busybox crond 에서는 sh 가 역슬래시를 지워 % 가 된다
        if esx:   # ESX 시계는 UTC. 파일 이름의 실행 시각은 VM 과 같은 KST 로 맞춘다
            pre += "TC_START=`TZ=KST-9 date +\\%Y\\%m\\%d\\%H\\%M\\%S`; "
        else:
            pre += "TC_START=`date +\\%Y\\%m\\%d\\%H\\%M\\%S`; "
    return pre + body


def marker(case, host):
    return " # tcgen %s %s" % (case, host)


def cron_line(t_kst, cmd, case, host, esx=False):
    t = t_kst - KST_TO_UTC if esx else t_kst
    return "%02d %02d %02d %02d * %s%s" % (t.minute, t.hour, t.day, t.month, render_command(cmd, esx), marker(case, host))


def format_monitors(sel):
    parts = ["%s(%s)" % (m["name"], ",".join(m["targets"])) for m in (sel or []) if m.get("targets")]
    return ", ".join(parts) if parts else NONE_JOB


def _dup_names(rows):
    seen, dup = set(), []
    for r in rows:
        n = (r.get("name") or "").strip()
        if n and n in seen and n not in dup:
            dup.append(n)
        seen.add(n)
    return dup


def build_entries(cases, run_cmds, mon_cmds, settings, now=None):
    """crontab 에 넣을 줄 목록, 오류, 경고, 시각 계산 결과를 반환한다."""
    now = now or dt.datetime.now()
    mon_lead, stop_lead = int(settings["mon_lead_min"]), int(settings["stop_lead_min"])
    entries, errors, warnings = [], [], []
    if mon_lead < 0 or stop_lead < 0:
        errors.append("설정: 모니터링 시작, 중지 분은 0 이상")
    if stop_lead <= mon_lead:
        errors.append("설정: 모니터링 중지 분(%d)은 모니터링 시작 분(%d)보다 커야 함. 같거나 작으면 앞 케이스 중지 명령이 "
                      "다음 케이스 모니터링 시작 뒤에 실행됨" % (stop_lead, mon_lead))
    for label, rows in (("탭 1 실행 명령", run_cmds), ("탭 2 모니터링 명령", mon_cmds)):
        for n in _dup_names(rows):
            errors.append("%s: 명령어 명 '%s' 가 두 번 이상 있음" % (label, n))
        for r in rows:
            if (r.get("name") or "").strip() and not NAME_RE.match(r["name"].strip()):
                errors.append("%s: 명령어 명은 영문, 숫자, . _ - 만 사용 ('%s')" % (label, r["name"]))
    if errors:
        return entries, errors, warnings, case_schedule(cases, mon_lead, stop_lead)
    run_by = {r["name"].strip(): r for r in run_cmds if (r.get("name") or "").strip()}
    mon_by = {m["name"].strip(): m for m in mon_cmds if (m.get("name") or "").strip()}
    sched = case_schedule(cases, mon_lead, stop_lead)    # 적용 체크한 행도 넣어 계산(윗줄 자동 중지 시각의 기준)
    names = [(c.get("name") or "").strip() for c in cases]
    order = {"모니터링 시작": 0, "잡": 1, "모니터링 중지": 2}
    checked = set()

    def cmd_errors(text, label):
        key = (label, text)
        if key in checked:
            return []
        checked.add(key)
        return check_command(text, label)

    def add(c, role, host, kind, name, app, t, cmd, esx):
        line = cron_line(t, cmd, c["name"].strip(), host, esx)
        entries.append({"case": c["name"].strip(), "role": role, "host": host, "kind": kind, "name": name,
                        "app": app, "when": t, "esx": esx, "cmd": cmd.strip(), "line": line})
        if t < now:
            warnings.append("%s %s %s %s: %s 는 지난 시각. cron 은 연도가 없어 내년 같은 날짜에 실행됨"
                            % (c["name"].strip(), host, name, kind, fmt_dt(t)))

    for i, c in enumerate(cases):
        if c.get("applied"):
            continue
        row = "%d행" % (i + 1)
        name = names[i]
        if not NAME_RE.match(name):
            errors.append("%s: 케이스 이름은 영문, 숫자, . _ - 만 사용 ('%s')" % (row, name))
            continue
        if names.count(name) > 1:
            errors.append("%s: 케이스 이름 중복 '%s' (적용 체크한 행 포함). 같은 이름으로 생성하면 먼저 등록한 케이스의 "
                          "crontab 줄이 지워지고 바뀜" % (row, name))
            continue
        s = sched[i]
        sel = selected_monitors(c)
        jobs = [(role, (c.get(key) or "").strip()) for role, key in (("PVM", "pvm_job"), ("SVM", "svm_job"))]
        jobs = [(role, j) for role, j in jobs if j and j != NONE_JOB]
        if not sel and not jobs:
            warnings.append("%s %s: 잡도 모니터링도 없어 등록할 줄이 없음" % (row, name))
            continue
        if s["start_error"]:
            errors.append("%s %s: 잡 시작 %s" % (row, name, s["start_error"]))
            continue
        hosts = {"ESX": (c.get("esx") or "").strip(), "PVM": (c.get("pvm") or "").strip(),
                 "SVM": (c.get("svm") or "").strip()}
        need = set(role for role, _ in jobs)
        for m in sel:
            need.update(m["targets"])
        bad = [r for r in ("ESX", "PVM", "SVM") if r in need and not NAME_RE.match(hosts[r])]
        if bad:
            errors.append("%s %s: %s 이름이 비었거나 영문, 숫자, . _ - 밖의 문자가 있음" % (row, name, ", ".join(bad)))
            continue
        if sel and s["stop_error"]:
            errors.append("%s %s: %s" % (row, name, s["stop_error"]))
            continue
        if sel and s["mon_stop"] is not None and i + 1 < len(cases) and sched[i + 1]["mon_start"] is not None \
                and selected_monitors(cases[i + 1]) and s["mon_stop"] >= sched[i + 1]["mon_start"]:
            warnings.append("%s %s: 모니터링 중지 %s 가 다음 케이스 모니터링 시작 %s 보다 늦음"
                            % (row, name, fmt_dt(s["mon_stop"]), fmt_dt(sched[i + 1]["mon_start"])))
        for m in sel:
            mname = m["name"]
            mc = mon_by.get(mname)
            if not mc:
                errors.append("%s %s: 모니터링 명령 '%s' 가 탭 2 에 없음" % (row, name, mname))
                continue
            esx = (mc.get("where") or "VM").strip().upper() == "ESX"
            wrong = [t for t in m["targets"] if (t == "ESX") != esx]
            if wrong:
                errors.append("%s %s: 모니터링 '%s' 실행 위치는 %s 인데 대상이 %s. 모니터링 칸에서 다시 선택"
                              % (row, name, mname, "ESX" if esx else "VM", ",".join(wrong)))
                continue
            for e in cmd_errors(mc.get("start"), "탭 2 '%s' 시작 명령" % mname):
                errors.append(e)
            stop_cmd = (mc.get("stop") or "").strip()
            if stop_cmd:
                for e in cmd_errors(stop_cmd, "탭 2 '%s' 중지 명령" % mname):
                    errors.append(e)
            for role in m["targets"]:
                add(c, role, hosts[role], "모니터링 시작", mname, mc.get("app", ""), s["mon_start"], mc.get("start", ""), esx)
                if s["mon_stop"] is not None and stop_cmd:
                    add(c, role, hosts[role], "모니터링 중지", mname, mc.get("app", ""), s["mon_stop"], stop_cmd, esx)
        for role, jn in jobs:
            rc = run_by.get(jn)
            if not rc:
                errors.append("%s %s: %s 잡 '%s' 가 탭 1 에 없음" % (row, name, role, jn))
                continue
            for e in cmd_errors(rc.get("cmd"), "탭 1 '%s' 명령" % jn):
                errors.append(e)
            add(c, role, hosts[role], "잡", jn, rc.get("app", ""), s["job"], rc.get("cmd", ""), False)
    entries.sort(key=lambda e: (e["when"], order[e["kind"]]))
    return entries, errors, warnings, sched


# ------------------------------------------------------------------ 붙여넣기 블록
def _sq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _printf_lines(text, target):
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    out = []
    for j, p in enumerate(parts):
        fmt = "'%s\\n'" if j == len(parts) - 1 else "'%s'"
        out.append("printf %s %s >> %s" % (fmt, _sq(p), target))
    return out


def _bre(s):
    return re.sub(r"([.\[\]*^$\\])", r"\\\1", s)


def _hd(line):
    """따옴표 없는 끝 표시 here-document 의 본문 한 줄: \\, $, ` 앞에 역슬래시를 넣는다."""
    return re.sub(r"([\\$`])", r"\\\1", line)


def hd_unescape(text):
    """_hd 의 반대. 셸이 본문을 받아 처리한 결과와 같다(시험용)."""
    return re.sub(r"\\([\\$`])", r"\1", text)


VMSSH_LINES = [
    'SSH_ASKPASS="$HOME/.tcgen/askpass"; DISPLAY="${DISPLAY:-tcgen:0}"; SSH_ASKPASS_REQUIRE=force',
    "export SSH_ASKPASS DISPLAY SSH_ASKPASS_REQUIRE",
    'O="-x -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1"',
    "if ssh -G -o StrictHostKeyChecking=accept-new localhost >/dev/null 2>&1; then",
    '  O="$O -o StrictHostKeyChecking=accept-new"',
    "else",
    '  O="$O -o StrictHostKeyChecking=no"',
    "fi",
    'echo "---- $1"',
    'setsid ssh $O "$1" sh -s',
    "R=$?",
    '[ $R -eq 0 ] || echo "==== $1 FAILED (ssh exit $R)"',
    "exit $R",
]


# 비밀번호 입력 스크립트(~/.tcgen/setpw). 붙여넣기에서 남은 줄을 1초 동안 비우고, 빈 입력이면 다시 묻고, CR 을 지운다.
SETPW_LINES = [
    "D=$HOME/.tcgen",
    "umask 077",
    "while read -r -t 1 X 2>/dev/null; do :; done",
    "trap 'stty echo 2>/dev/null' 0 2 15",
    "stty -echo 2>/dev/null",
    "PW=",
    'while [ -z "$PW" ]; do',
    "  printf 'VM password: '",
    "  IFS= read -r PW || exit 1",
    "  PW=`printf '%s' \"$PW\" | tr -d '\\r'`",
    "  echo",
    "done",
    "stty echo 2>/dev/null",
    "printf '%s\\n' \"$PW\" > $D/vmpw",
    "chmod 600 $D/vmpw",
    'echo "saved: $D/vmpw"',
]


def setup_block():
    """점프 VM 최초 1회: 자동 로그인용 스크립트(askpass, vmssh, setpw) 생성 후 setpw 로 VM 비밀번호 입력."""
    body = ["umask 077",
            "D=$HOME/.tcgen; mkdir -p $D",
            "printf '%s\\n' 'cat \"$HOME/.tcgen/vmpw\"' > $D/askpass"]
    for name, src in (("vmssh", VMSSH_LINES), ("setpw", SETPW_LINES)):
        body.append(": > $D/%s" % name)
        for v in src:
            body += _printf_lines(v, "$D/%s" % name)
    body += ["chmod 700 $D/askpass $D/vmssh $D/setpw",
             'echo "created: $D (askpass, vmssh, setpw)"']
    out = ["sh << TCSETUP"] + [_hd(x) for x in body] + ["TCSETUP", "sh ~/.tcgen/setpw"]
    return "\n".join(out) + "\n"


def vm_block(host, user, lines, cases):
    target = "%s@%s" % (user, host) if user else host
    body = ["N=$HOME/.tcgen_cron.new; P=$HOME/.tcgen_cron.pat",
            ": > $P"]
    for c in cases:
        body += _printf_lines(_bre(marker(c, host)) + "$", "$P")
    body.append("crontab -l 2>/dev/null | grep -v -f $P > $N")
    for ln in lines:
        body += _printf_lines(ln, "$N")
    body += ["crontab $N && rm -f $N $P || echo \"==== crontab FAILED, kept $N\"",
             "echo \"== `hostname -s`  `date '+%Y-%m-%d %H:%M %Z'`\"",
             "crontab -l | grep ' # tcgen '"]
    out = ["sh ~/.tcgen/vmssh %s << TCEOF" % target] + [_hd(x) for x in body] + ["TCEOF"]
    return "\n".join(out) + "\n"


def esx_block(host, lines, cases):
    out = ["# tcgen ESX %s : paste into the root SSH session of this host" % host,
           "CT=/var/spool/cron/crontabs/root; N=/tmp/tcgen_cron.new; P=/tmp/tcgen_cron.pat",
           "cp $CT /tmp/root.crontab.tcgen.bak",
           ": > $P"]
    for c in cases:
        out += _printf_lines(_bre(marker(c, host)) + "$", "$P")
    out.append("grep -v -f $P $CT > $N")
    for ln in lines:
        out += _printf_lines(ln, "$N")
    out += ["chmod u+w $CT",
            "cat $N > $CT && /bin/echo root >> /var/spool/cron/crontabs/cron.update",
            "rm -f $N $P",
            "echo \"== `hostname -s`  `date -u '+%Y-%m-%d %H:%M'` UTC  (KST `TZ=KST-9 date '+%m-%d %H:%M'`)\"",
            "grep ' # tcgen ' $CT"]
    return "\n".join(out) + "\n"


def build_blocks(entries, settings):
    """VM 등록 블록(전 VM 을 하나로)과 ESX 호스트별 블록을 만든다. 같은 호스트 안에서는 시각 순서."""
    vm_hosts, esx_hosts = [], []
    by_host = {}
    for e in entries:
        key = (e["esx"], e["host"])
        if key not in by_host:
            by_host[key] = {"lines": [], "cases": []}
            (esx_hosts if e["esx"] else vm_hosts).append(e["host"])
        by_host[key]["lines"].append(e["line"])
        if e["case"] not in by_host[key]["cases"]:
            by_host[key]["cases"].append(e["case"])
    user = (settings.get("vm_user") or "").strip()
    vm_text = "".join(vm_block(h, user, by_host[(False, h)]["lines"], by_host[(False, h)]["cases"]) for h in vm_hosts)
    esx = {h: esx_block(h, by_host[(True, h)]["lines"], by_host[(True, h)]["cases"]) for h in esx_hosts}
    return vm_text, esx


# ------------------------------------------------------------------ 기록
RECORD_HEADER = ["테스트 케이스 이름", "생성 시각", "ESX 호스트", "PVM", "SVM",
                 "PVM 잡", "PVM 잡 조건", "PVM 잡 명령", "SVM 잡", "SVM 잡 조건", "SVM 잡 명령",
                 "잡 시작(KST)", "간격", "모니터링(대상)", "모니터링 시작(KST)", "모니터링 중지(KST)"]
LOG_HEADER = ["테스트 케이스 이름", "생성 시각", "구분", "대상", "호스트", "명령어 명", "App명", "실행 시각(KST)",
              "crontab 시각 기준", "명령", "crontab 줄", "ESX 호스트", "PVM", "SVM", "잡 시작(KST)", "간격",
              "모니터링(대상)", "모니터링 시작(KST)", "모니터링 중지(KST)"]


def _stop_text(c, s):
    if not selected_monitors(c):
        return ""
    if (c.get("stop_mode") or "auto") == "none":
        return "중지 없음"
    return fmt_dt(s["mon_stop"]) if s.get("mon_stop") else ""


def append_csv(path, header, rows):
    """CSV 에 행을 추가한다. 새 파일이면 머리글을 쓴다. 기존 파일 머리글이 다르면 같은 이름의 열에만 값을 넣는다.
    첫 열에는 항상 테스트 케이스 이름을 넣는다. 기존 파일에 없어 기록하지 못한 열 이름 목록을 반환한다."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(buf.getvalue())
        return []
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:4096]:
        raise ValueError("테스트 기록 CSV 를 글자로 읽을 수 없음 (암호화된 파일일 수 있음): %s" % path)
    try:
        text, enc = _decode(data)
    except UnicodeDecodeError:
        raise ValueError("테스트 기록 CSV 의 글자 인코딩을 알 수 없음 (UTF-8, CP949 아님): %s" % path)
    first = next(csv.reader(io.StringIO(text)), [])
    existing = [h.strip() for h in first]
    if existing == header:
        out_rows, missing = rows, []
    else:
        idx = {h: i for i, h in enumerate(header)}
        out_rows = []
        for r in rows:
            o = []
            for j, h in enumerate(existing):
                if j == 0:
                    o.append(r[0])
                elif h in idx:
                    o.append(r[idx[h]])
                else:
                    o.append("")
            out_rows.append(o)
        missing = [h for h in header[1:] if h not in existing]
    buf = io.StringIO()
    if data and not data.endswith(b"\n"):
        buf.write("\r\n")
    csv.writer(buf).writerows(out_rows)
    with open(path, "a", encoding=enc, newline="", errors="replace") as f:
        f.write(buf.getvalue())
    return missing


def write_records(settings, cases, sched, entries, run_cmds, gen_time):
    """테스트 기록 CSV 에 케이스마다 한 행 추가, 로그 폴더에 testcaselogging-<년월일시분초>.csv 저장."""
    run_by = {r["name"].strip(): r for r in run_cmds if (r.get("name") or "").strip()}
    gen_text = fmt_dt(gen_time)
    used = set(e["case"] for e in entries)
    rec_rows = []
    by_case = {}
    for i, c in enumerate(cases):
        name = (c.get("name") or "").strip()
        if name not in used:
            continue
        s = sched[i]
        by_case[name] = (c, s)
        jobs = []
        for key in ("pvm_job", "svm_job"):
            j = (c.get(key) or "").strip()
            j = "" if j == NONE_JOB else j
            rc = run_by.get(j, {})
            jobs += [j, rc.get("desc", "") if j else "", rc.get("cmd", "") if j else ""]
        has_mon = bool(selected_monitors(c))
        rec_rows.append([name, gen_text, c.get("esx", ""), c.get("pvm", ""), c.get("svm", "")] + jobs +
                        [fmt_dt(s["job"]) if s["job"] else "", c.get("interval", ""),
                         format_monitors(c.get("monitors")) if has_mon else "",
                         fmt_dt(s["mon_start"]) if (has_mon and s["mon_start"]) else "", _stop_text(c, s)])
    missing = append_csv(settings["record_csv"], RECORD_HEADER, rec_rows)
    log_rows = []
    for e in entries:
        c, s = by_case[e["case"]]
        has_mon = bool(selected_monitors(c))
        log_rows.append([e["case"], gen_text, e["kind"], e["role"], e["host"], e["name"], e["app"], fmt_dt(e["when"]),
                         "UTC" if e["esx"] else "KST", e["cmd"], e["line"], c.get("esx", ""), c.get("pvm", ""),
                         c.get("svm", ""), fmt_dt(s["job"]) if s["job"] else "", c.get("interval", ""),
                         format_monitors(c.get("monitors")) if has_mon else "",
                         fmt_dt(s["mon_start"]) if (has_mon and s["mon_start"]) else "", _stop_text(c, s)])
    os.makedirs(settings["log_dir"], exist_ok=True)
    log_path = os.path.join(settings["log_dir"], "testcaselogging-%s.csv" % gen_time.strftime("%Y%m%d%H%M%S"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LOG_HEADER)
    w.writerows(log_rows)
    with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    return settings["record_csv"], log_path, len(rec_rows), missing


def generate(cases, run_cmds, mon_cmds, settings, now=None, write=True):
    """화면의 [명령어 생성] 한 번에 해당한다. 오류가 있으면 블록과 기록을 만들지 않는다.
    settings 의 record_csv, log_dir 는 resolve_paths 로 바꾼 절대 경로여야 한다."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    entries, errors, warnings, sched = build_entries(cases, run_cmds, mon_cmds, settings, now)
    res = {"errors": errors, "warnings": warnings, "entries": entries, "sched": sched,
           "vm": "", "esx": {}, "setup": setup_block(), "record": "", "log": "", "record_rows": 0,
           "record_missing": [], "gen_time": now}
    res["skipped"] = sum(1 for c in cases if c.get("applied"))
    if errors or not entries:
        if not entries and not errors:
            if cases and all(c.get("applied") for c in cases):
                res["errors"] = ["모든 케이스가 적용 체크되어 있어 생성할 케이스가 없음. 새로 등록할 케이스는 적용 체크를 끌 것"]
            else:
                res["errors"] = ["등록할 줄이 없음. 적용 체크를 끈 케이스마다 잡이나 모니터링을 하나 이상 지정할 것"]
        return res
    res["vm"], res["esx"] = build_blocks(entries, settings)
    if write:
        res["record"], res["log"], res["record_rows"], res["record_missing"] = write_records(
            settings, cases, sched, entries, run_cmds, now)
    return res
