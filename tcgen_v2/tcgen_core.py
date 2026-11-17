# -*- coding: utf-8 -*-
"""
tcgen_core.py - 테스트 실행 명령 생성기 v2 의 계산과 출력 (화면 코드와 분리)

진행 방식은 두 가지다(설정 run_mode).
- time (시각 지정): v1.4 와 같은 동작. 케이스마다 잡 시작 시각을 정해 crontab 줄을 만든다. 블록 A, B, C, D.
- seq  (순서 실행): 시각을 정하지 않는다. 점프 VM 의 진행 스크립트(tcrun)가 케이스를 표의 순서대로 진행한다.
  설정 프로파일 적용, 모니터링 시작, 잡 시작, 프로세스 이름으로 잡 종료 확인, 모니터링 중지를 차례로 한다. 블록 A, P, S, D.
  이 부분은 파일 아래쪽 '순서 실행' 절에 있고, 점프 VM 에 설치하는 파일의 내용은 tcgen_payload.py 에 있다.

아래는 v1.4 에서 이어지는 설명이다.

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
import base64
import codecs
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

VERSION = "2.9"
KST_TO_UTC = dt.timedelta(hours=9)
CHUNK = 100            # 붙여넣는 글 한 줄에 담는 명령 조각 길이(문자 수)
NONE_JOB = "(없음)"

SETTINGS_FILE = "settings.json"
RUN_FILE = "run_commands.csv"
MON_FILE = "monitor_commands.csv"
CASES_FILE = "test_cases.json"       # 탭 4 테스트 케이스 표와 적용 체크 상태
PROFILES_FILE = "profiles.json"      # 탭 3 설정 프로파일 (순서 실행에서 케이스 시작 전에 적용할 ESX 호스트, VM 설정)

CASES_VERSION = 2                     # 2: 케이스마다 VM 목록 (1: PVM, SVM 칸 고정)
CASE_DEFAULTS = {
    "applied": False,      # 적용 체크: 이미 등록한 케이스. [명령어 생성] 에서 빠진다
    "name": "", "esx": "",
    "vms": [],             # VM 목록: {"name": VM 이름, "job": 탭 1 명령어 명 또는 (없음), "monitors": [탭 2 명령어 명],
                           #           "end_ref": 리포트 생성기의 종료 기준 VM 이면 True}
    "esx_monitors": [],    # ESX 호스트에서 실행할 모니터링 (탭 2 명령어 명)
    "interval": "", "start": "", "stop_mode": "auto", "stop_time": "",
    "desc": "",            # 케이스 설명 (리포트용 CSV 의 '설명')
    "group": "",           # 리포트용 CSV 의 '테스트 그룹'. 같은 그룹의 케이스를 리포트 생성기가 HTML 하나에 모은다
    "end_time": "",        # 리포트용 CSV 의 '잡 종료 시각'. 비우면 리포트 생성기가 자동으로 정한다
    "esx_profiles": [],    # 순서 실행: 케이스 시작 전에 적용할 ESX 호스트 설정 프로파일 이름 (탭 3, 대상 ESX)
    "max_wait": "",        # 순서 실행: 잡 종료를 기다리는 최대 시간 (시간:분). 비우면 제한 없음
    "seq_start": "",       # 순서 실행: 잡을 시작할 시각. '10:00', '2026-11-20 10:00:00', '+8:00'(앞 케이스 시작 + 8시간), '자동'(앞 케이스가
                           # 끝난 뒤). 빈 값은 core 에서는 자동과 같고, 화면(tcgen_app)은 잡이 있는 케이스의 빈 값을 '입력 필요' 로 보이며 생성하지 않는다
    "applied_stop": "",    # 시각 지정: 적용 체크한 케이스의 자동 모니터링 중지 시각을 고정해 둔 값(freeze_applied). 적용 체크를 끄면 지운다
}
DEFAULT_CASE_DIR = "testcases"
DEFAULT_RECORD = "test_record.csv"

DEFAULT_SETTINGS = {
    "record_csv": "",          # 비우면 <생성기 폴더>/testcases/test_record.csv
    "log_dir": "",             # 비우면 <생성기 폴더>/testcases
    "vm_user": "",             # 비우면 점프 VM 계정 그대로 ssh
    "mon_lead_min": 5,         # 모니터링 시작 = 잡 시작 N분 전
    "stop_lead_min": 10,       # 모니터링 중지(자동) = 다음 케이스 잡 시작 N분 전
    "vm_prefix": "ev",         # VM 이름 = ESX 호스트 이름 + 이 값 + 두 자리 번호 (ev01, ev02 ...)
    "default_vms": 2,          # 새 케이스의 VM 수
    "run_mode": "seq",         # 진행 방식: seq = 순서 실행, time = 시각 지정(v1.4 방식)
    "jump_log_dir": "",        # 순서 실행: 점프 VM 의 로그 폴더. 비우면 ~/tcgen_log (작업 폴더를 적었으면 <작업 폴더>/log)
    "jump_dir": "",            # 순서 실행: 점프 VM 작업 폴더(tcrun, ssh 키, VM 비밀번호, 실행 기록). 비우면 ~/.tcgen
    "esx_addr": "",            # 순서 실행: ESX 접속 주소 '호스트이름=주소;호스트이름=주소'. 비우면 ESX 호스트 이름으로 접속
    "seq_settle_min": 10,      # 순서 실행: VM 을 켠 케이스는 마지막으로 켠 VM 의 준비(1GB page backing, ssh 로그인) N분 뒤부터 잡 시작 (0 = 기다리지 않음)
    "seq_gap_min": 30,         # 순서 실행: 앞 케이스의 잡이 끝난 뒤 다음 케이스의 잡을 시작하기까지 최소 N분 (0 = 기다리지 않음)
    "seq_start_spacing_min": 5,    # 순서 실행: 모든 잡 시작은 서로 N분 이상 떨어진다(ESX 호스트가 달라도, 이 점프 VM 의 다른 실행과도)
    "seq_prep_lead_min": 60,   # 순서 실행: 시작 시각을 적은 케이스는 그 시각 N분 전에 준비(설정 적용, VM 전원)를 시작한다 (0 = 차례가 오면 바로)
    "seq_poll_sec": 1800,      # 순서 실행: 잡 종료 확인 간격(초). 화면에서는 분으로 고친다. 잡이 실행되는 동안 VM 에 접속하는 주기라서 길게 둔다
    "seq_first_check_sec": 60, # 순서 실행: 잡 시작 뒤 프로세스가 떴는지 처음 확인하는 때(초). 한 번만 확인하고 그 뒤로는 위 간격을 따른다
    "seq_gone_count": 3,       # 순서 실행: 프로세스가 연속으로 이 횟수만큼 없으면 잡 종료로 판정
    "seq_start_wait_min": 10,  # 순서 실행: 잡 시작 뒤 이 시간 안에 프로세스가 한 번도 보이지 않으면 멈춤
    "seq_poweron_wait_min": 90,    # 순서 실행: ESX 의 VM 켜기 작업(vim-cmd vmsvc/power.on)이 끝나기를 기다리는 최대 시간
    "seq_boot_wait_min": 30,   # 순서 실행: VM 을 켠 뒤 ssh 로그인을 기다리는 최대 시간
    "seq_shutdown_wait_min": 10,   # 순서 실행: 게스트 종료를 기다리는 최대 시간. 넘으면 전원 끄기
}
RUN_MODES = ("seq", "time")
SEQ_INT_SETTINGS = (("seq_settle_min", "VM 켠 뒤 잡 시작까지 최소 분", 0), ("seq_gap_min", "케이스 사이 간격 분", 0),
                    ("seq_start_spacing_min", "잡 시작 최소 간격 분", 1), ("seq_prep_lead_min", "시작 시각 준비 분", 0),
                    ("seq_poll_sec", "잡 종료 확인 간격(초)", 5), ("seq_first_check_sec", "잡 시작 뒤 첫 확인(초)", 1),
                    ("seq_gone_count", "연속 없음 횟수", 1), ("seq_start_wait_min", "시작 대기 분", 1),
                    ("seq_poweron_wait_min", "VM 켜기 대기 분", 1), ("seq_boot_wait_min", "VM 부팅 대기 분", 1),
                    ("seq_shutdown_wait_min", "종료 대기 분", 1))

RUN_FIELDS = ["app", "name", "desc", "cmd", "proc"]
RUN_HEADERS = ["App명", "명령어 명", "잡 조건 설명", "경로/실행 명령", "프로세스 이름"]   # 프로세스 이름: 순서 실행의 잡 종료 확인용
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


# 테스트 케이스 파일: 탭 4 의 표를 사용자가 정한 파일에 저장하고 불러온다(화면의 [불러오기], [다른 이름으로 저장]).
# 정하지 않으면 데이터 폴더의 test_cases.json 에 자동 저장한다(v1.4, v2.2 까지와 같은 자리). 설정의 case_file 이 정한 파일의
# 경로다(빈 값 = 정하지 않음). 실행 명령, 모니터링 명령, 설정 프로파일과 그 밖의 설정은 어느 파일을 쓰든 같이 쓴다.
# 기록 CSV 와 로그 폴더는 테스트 케이스 파일마다 마지막에 쓰던 값을 기억한다(설정의 case_file_paths).
# 메모리의 설정에서 record_csv, log_dir 는 항상 지금 쓰는 테스트 케이스 파일의 값이다.
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


def case_file(s):
    """사용자가 정한 테스트 케이스 파일의 경로. 정하지 않았으면 빈 문자열(데이터 폴더의 test_cases.json 을 쓴다)."""
    return ((s or {}).get("case_file") or "").strip()


def cases_path(base, path=""):
    """테스트 케이스 표를 저장하는 파일. path(사용자가 정한 파일)가 비어 있으면 데이터 폴더의 test_cases.json."""
    return os.path.normpath(path) if (path or "").strip() else os.path.join(base, CASES_FILE)


def case_file_key(base, path):
    """테스트 케이스 파일별 값을 찾는 열쇠. 정하지 않았거나 데이터 폴더의 test_cases.json 을 고른 경우는 빈 문자열."""
    if not (path or "").strip():
        return ""
    k = os.path.normcase(os.path.abspath(path.strip()))
    return "" if k == os.path.normcase(os.path.abspath(os.path.join(base, CASES_FILE))) else k


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
    for k in ("mon_lead_min", "stop_lead_min", "default_vms"):
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            s[k] = DEFAULT_SETTINGS[k]
    s["default_vms"] = min(max(s["default_vms"], 0), 20)
    for k, _, _ in SEQ_INT_SETTINGS:
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            s[k] = DEFAULT_SETTINGS[k]
    for k in ("record_csv", "log_dir", "vm_user", "vm_prefix", "jump_log_dir", "esx_addr"):
        s[k] = "" if s.get(k) is None else str(s[k])
    if s.get("run_mode") not in RUN_MODES:
        s["run_mode"] = DEFAULT_SETTINGS["run_mode"]
    # 테스트 케이스 파일과 파일별 기록 경로. 파일의 맨 위 record_csv, log_dir 는 기본 파일(test_cases.json)을 쓸 때의 값이다
    cf = "" if s.get("case_file") is None else str(s.get("case_file")).strip()
    key = case_file_key(base, cf)
    raw = s.get("case_file_paths") if isinstance(s.get("case_file_paths"), dict) else {}
    paths = {"": {"record_csv": s["record_csv"], "log_dir": s["log_dir"]}}
    for k, q in raw.items():
        if isinstance(k, str) and k and isinstance(q, dict):
            paths[k] = dict((x, "" if q.get(x) is None else str(q.get(x))) for x in ("record_csv", "log_dir"))
    s["case_file"], s["case_file_paths"] = (os.path.normpath(cf) if key else ""), paths
    if key in paths:                    # 처음 쓰는 파일이면 기본 파일의 값을 그대로 쓴다
        s["record_csv"], s["log_dir"] = paths[key]["record_csv"], paths[key]["log_dir"]
    for k in ("case_set", "case_sets", "case_set_paths"):       # 2026-11-16 에 잠깐 있던 '테스트 종류' 의 키. 쓰지 않는다
        s.pop(k, None)
    return s


def _atomic_write(path, text, encoding):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def save_settings(base, s):
    """설정을 저장한다. s 의 record_csv, log_dir 는 지금 쓰는 테스트 케이스 파일의 값이다. 파일에는 기본 파일(test_cases.json)을
    쓸 때의 값을 맨 위에, 사용자가 정한 파일의 값을 case_file_paths 에 적는다. 기본 경로와 같은 값은 비워 저장해 데이터 폴더를
    옮겨도 따라가게 한다. 없어진 파일의 값은 버린다."""
    out = dict(s)
    cf = case_file(out)
    key = case_file_key(base, cf)
    src = out.get("case_file_paths") if isinstance(out.get("case_file_paths"), dict) else {}
    paths = dict((k, dict(v)) for k, v in src.items() if isinstance(v, dict))
    paths[key] = {"record_csv": out.get("record_csv"), "log_dir": out.get("log_dir")}
    clean = {}
    for k, q in paths.items():
        if k and k != key and not os.path.isfile(k):
            continue
        c = {}
        for name, dflt in zip(("record_csv", "log_dir"), default_paths(base)):
            v = "" if q.get(name) is None else str(q.get(name)).strip()
            if v and os.path.normcase(os.path.normpath(v)) == os.path.normcase(dflt):
                v = ""
            c[name] = v
        clean[k] = c
    top = clean.get("", {"record_csv": "", "log_dir": ""})
    out["record_csv"], out["log_dir"] = top["record_csv"], top["log_dir"]
    out["case_file"] = os.path.normpath(cf) if key else ""
    out["case_file_paths"] = dict((k, v) for k, v in clean.items() if k)
    _atomic_write(os.path.join(base, SETTINGS_FILE), json.dumps(out, ensure_ascii=False, indent=2) + "\n", "utf-8")


def switch_case_file(base, s, path):
    """테스트 케이스 파일을 path 로 바꾼 새 설정을 반환한다(설정 저장과 표 읽기, 쓰기는 호출한 쪽이 한다).
    path 가 비어 있거나 데이터 폴더의 test_cases.json 이면 기본 파일이다. 지금 파일의 기록 CSV, 로그 폴더 값을 보관하고,
    path 를 전에 쓴 적이 있으면 그때의 값을 넣는다. 처음 쓰는 파일이면 지금 값을 그대로 둔다."""
    out = dict(s)
    src = s.get("case_file_paths") if isinstance(s.get("case_file_paths"), dict) else {}
    paths = dict((k, dict(v)) for k, v in src.items() if isinstance(v, dict))
    paths[case_file_key(base, case_file(s))] = {"record_csv": s.get("record_csv") or "", "log_dir": s.get("log_dir") or ""}
    key = case_file_key(base, path)
    out["case_file"] = os.path.normpath(path.strip()) if key else ""
    out["case_file_paths"] = paths
    if key in paths:
        out["record_csv"], out["log_dir"] = paths[key]["record_csv"], paths[key]["log_dir"]
    return out


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


def _names(items):
    """모니터링 명령어 명 목록을 정리한다(중복, 빈 이름 제거). 예전 형식의 {"name": ...} 도 받는다."""
    out = []
    for m in items or []:
        n = m.get("name") if isinstance(m, dict) else m
        n = "" if n is None else str(n).strip()
        if n and n not in out:
            out.append(n)
    return out


def _clean_vm(v):
    job = "" if v.get("job") is None else str(v.get("job")).strip()
    power = "" if v.get("power") is None else str(v.get("power")).strip().lower()
    return {"name": "" if v.get("name") is None else str(v.get("name")).strip(),
            "job": job or NONE_JOB, "monitors": _names(v.get("monitors")), "end_ref": bool(v.get("end_ref", False)),
            "profiles": _names(v.get("profiles")), "power": "off" if power in ("off", "끔") else "on"}


def _clean_case(c):
    d = {"applied": bool(c.get("applied", False))}
    for k in ("name", "esx", "interval", "start", "stop_mode", "stop_time", "desc", "group", "end_time", "max_wait", "seq_start",
              "applied_stop"):
        v = c.get(k, CASE_DEFAULTS[k])
        d[k] = "" if v is None else str(v)
    if d["stop_mode"] not in ("auto", "manual", "none"):
        d["stop_mode"] = "auto"
    if not d["applied"]:
        d["applied_stop"] = ""          # 고정한 값은 적용 체크한 케이스에만 둔다
    d["esx_profiles"] = _names(c.get("esx_profiles"))
    if "vms" in c:
        d["vms"] = [_clean_vm(v) for v in (c.get("vms") or []) if isinstance(v, dict)]
        d["esx_monitors"] = _names(c.get("esx_monitors"))
    else:
        # 예전 형식(PVM, SVM 칸): VM 2대로 바꾼다. 모니터링 대상 PVM, SVM 은 그 VM 의 모니터링, ESX 는 ESX 모니터링
        mons = [m for m in (c.get("monitors") or []) if isinstance(m, dict) and m.get("name")]
        d["vms"] = [{"name": str(c.get(nk) or "").strip(), "job": str(c.get(jk) or "").strip() or NONE_JOB,
                     "monitors": _names([m for m in mons if role in (m.get("targets") or [])]), "end_ref": False,
                     "profiles": []}
                    for role, nk, jk in (("PVM", "pvm", "pvm_job"), ("SVM", "svm", "svm_job"))]
        d["esx_monitors"] = _names([m for m in mons if "ESX" in (m.get("targets") or [])])
    return d


# 테스트 케이스 CSV: 화면의 [다른 이름으로 저장] 이 만드는 형식. 케이스 행 아래에 그 케이스의 VM 행이 온다.
CASE_CSV_HEADER = ["구분", "적용", "케이스 이름", "설명", "ESX 호스트", "VM 이름", "잡", "모니터링", "설정", "최대 대기", "테스트 그룹",
                   "종료 기준 VM", "간격", "잡 시작", "모니터링 중지", "모니터링 중지 시각", "잡 종료 (리포트)", "시작 시각 (순서 실행)",
                   "전원", "모니터링 중지 (적용 때 고정)"]
STOP_LABELS = {"auto": "자동", "manual": "직접 입력", "none": "중지 없음"}
CSV_TRUE = ("예", "Y", "YES", "1", "TRUE", "V", "O")


def _join_names(names):
    """이름 여러 개를 한 칸에 적는다: 'a; b'. 이름에 ; 이 들어 있으면 JSON 목록으로 적는다."""
    names = list(names or [])
    if any(";" in n or n != n.strip() for n in names):
        return json.dumps(names, ensure_ascii=False)
    return "; ".join(names)


def _split_names(text):
    t = (text or "").strip()
    if not t:
        return []
    if t.startswith("["):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                return [str(x) for x in v]
        except ValueError:
            pass
    return [x.strip() for x in t.split(";") if x.strip()]


def cases_to_csv(cases):
    """테스트 케이스 표를 CSV 글로 바꾼다. 케이스마다 '케이스' 행 하나와 VM 마다 'VM' 행 하나."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CASE_CSV_HEADER)
    for c in cases:
        c = _clean_case(c)
        w.writerow(["케이스", "예" if c["applied"] else "", c["name"], c["desc"], c["esx"], "", "", _join_names(c["esx_monitors"]),
                    _join_names(c["esx_profiles"]), c["max_wait"], c["group"], "", c["interval"], c["start"],
                    STOP_LABELS[c["stop_mode"]], c["stop_time"], c["end_time"], c["seq_start"], "", c["applied_stop"]])
        for v in c["vms"]:
            w.writerow(["VM", "", "", "", "", v["name"], v["job"], _join_names(v["monitors"]), _join_names(v["profiles"]), "", "",
                        "기준" if v["end_ref"] else "", "", "", "", "", "", "", POWER_LABELS[v["power"]], ""])
    return buf.getvalue()


def cases_from_csv(text):
    """테스트 케이스 CSV 의 글을 케이스 목록으로 바꾼다. 열은 머리글 이름으로 찾는다(순서가 달라도 됨). 형식이 맞지 않으면 ValueError."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any((x or "").strip() for x in r)]
    if not rows:
        return []
    idx = dict((h.strip(), i) for i, h in enumerate(rows[0]))
    if not all(k in idx for k in ("구분", "케이스 이름", "VM 이름")):
        hint = " 이 파일은 생성 내용을 적어 두는 테스트 기록 CSV 로 보인다." if "테스트 케이스 이름" in idx and "생성 시각" in idx else ""
        raise ValueError("테스트 케이스 파일이 아님 (머리글에 구분, 케이스 이름, VM 이름 열이 있어야 함).%s" % hint)

    def cell(r, name):
        i = idx.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    stop_by_label = dict((v, k) for k, v in STOP_LABELS.items())
    out = []
    for n, r in enumerate(rows[1:], 1):
        kind = cell(r, "구분").upper()
        if kind in ("케이스", "CASE"):
            sm = cell(r, "모니터링 중지")
            out.append({"applied": cell(r, "적용").upper() in CSV_TRUE, "name": cell(r, "케이스 이름"), "desc": cell(r, "설명"),
                        "esx": cell(r, "ESX 호스트"), "vms": [], "esx_monitors": _split_names(cell(r, "모니터링")),
                        "esx_profiles": _split_names(cell(r, "설정")), "max_wait": cell(r, "최대 대기"), "group": cell(r, "테스트 그룹"),
                        "interval": cell(r, "간격"), "start": cell(r, "잡 시작"), "stop_mode": stop_by_label.get(sm, sm or "auto"),
                        "stop_time": cell(r, "모니터링 중지 시각"), "end_time": cell(r, "잡 종료 (리포트)"),
                        "seq_start": cell(r, "시작 시각 (순서 실행)"), "applied_stop": cell(r, "모니터링 중지 (적용 때 고정)")})
        elif kind == "VM":
            if not out:
                raise ValueError("데이터 %d번째 행: 케이스 행보다 VM 행이 먼저 나옴" % n)
            out[-1]["vms"].append({"name": cell(r, "VM 이름"), "job": cell(r, "잡") or NONE_JOB, "monitors": _split_names(cell(r, "모니터링")),
                                   "power": cell(r, "전원"),
                                   "profiles": _split_names(cell(r, "설정")), "end_ref": cell(r, "종료 기준 VM") != ""})
        else:
            raise ValueError("데이터 %d번째 행: 구분 칸은 '케이스' 또는 'VM' 이어야 함 ('%s')" % (n, cell(r, "구분")))
    return [_clean_case(c) for c in out]


def load_cases(base, path=""):
    """테스트 케이스 표(적용 체크 포함)를 불러온다. path: 사용자가 정한 파일(빈 값 = 데이터 폴더의 test_cases.json).
    이름이 .json 으로 끝나면 JSON, 그 밖에는 테스트 케이스 CSV 로 읽는다. 파일이 없으면 빈 목록. 예전 형식의 JSON 도 읽는다."""
    p = cases_path(base, path)
    if not os.path.exists(p):
        return []
    if not p.lower().endswith(".json"):
        with open(p, "rb") as f:
            text, _ = _decode(f.read())
        return cases_from_csv(text)
    with open(p, encoding="utf-8-sig") as f:
        data = json.load(f)
    rows = data.get("cases", []) if isinstance(data, dict) else data
    return [_clean_case(c) for c in rows if isinstance(c, dict)]


def save_cases(base, cases, path=""):
    """테스트 케이스 표를 저장한다. path: 사용자가 정한 파일(빈 값 = 데이터 폴더의 test_cases.json). 이름이 .json 이면 JSON, 그 밖에는 CSV."""
    p = cases_path(base, path)
    if (path or "").strip():
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    if not p.lower().endswith(".json"):
        _atomic_write(p, cases_to_csv(cases), "utf-8-sig")
        return
    data = {"version": CASES_VERSION, "cases": [_clean_case(c) for c in cases]}
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def cases_file_version(base):
    p = os.path.join(base, CASES_FILE)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        try:
            return int(data.get("version", 1) or 1)
        except (TypeError, ValueError):
            return 1
    return 1


def upgrade_cases_file(base, now=None):
    """test_cases.json 이 예전 형식(PVM, SVM 칸)이면 원본을 backup/<시각>_before_vm_format/ 에 복사하고 VM 목록 형식으로
    다시 저장한다. 바꿨으면 원본을 복사한 폴더, 아니면 None 을 반환한다."""
    v = cases_file_version(base)
    if v is None or v >= CASES_VERSION:
        return None
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    dest = os.path.join(base, "backup", ts + "_before_vm_format")
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(base, CASES_FILE), os.path.join(dest, CASES_FILE))
    save_cases(base, load_cases(base))
    return dest


def vm_name(esx, n, prefix="ev"):
    esx = (esx or "").strip()
    return "%s%s%02d" % (esx, prefix, n) if esx else ""


def _vm_numbers(c, prefix="ev"):
    esx = (c.get("esx") or "").strip()
    nums = []
    if esx:
        rx = re.compile(re.escape(esx) + re.escape(prefix) + r"(\d+)$")
        for v in c.get("vms") or []:
            m = rx.match(v.get("name") or "")
            if m:
                nums.append(int(m.group(1)))
    return nums


def next_vm_number(c, prefix="ev"):
    """[+ VM 추가] 로 붙일 다음 번호: 'ESX 호스트+ev번호' 이름 중 가장 큰 번호 + 1. 그런 이름이 없으면 VM 수 + 1."""
    nums = _vm_numbers(c, prefix)
    return max(nums) + 1 if nums else len(c.get("vms") or []) + 1


POWER_LABELS = {"on": "켬", "off": "끔"}


def new_vm(name=""):
    return {"name": name, "job": NONE_JOB, "monitors": [], "end_ref": False, "profiles": [], "power": "on"}


def new_case(esx="", start="", count=2, prefix="ev"):
    return {"applied": False, "name": "", "esx": esx, "vms": [new_vm(vm_name(esx, k + 1, prefix)) for k in range(count)],
            "esx_monitors": [], "interval": "", "start": start, "stop_mode": "auto", "stop_time": "",
            "desc": "", "group": "", "end_time": "", "esx_profiles": [], "max_wait": "", "seq_start": "", "applied_stop": ""}


def rename_vms_for_esx(c, old, new, prefix="ev"):
    """ESX 호스트를 바꾸면 자동 이름(옛 호스트+ev번호)과 빈 이름의 VM 을 새 호스트 기준 이름으로 바꾼다. 직접 고친 이름은 둔다."""
    old, new = (old or "").strip(), (new or "").strip()
    vms = c.get("vms") or []
    if old:
        rx = re.compile(re.escape(old) + "(" + re.escape(prefix) + r"\d+)$")
        for v in vms:
            m = rx.match(v.get("name") or "")
            if m:
                v["name"] = (new + m.group(1)) if new else ""
    if new:
        c["esx"] = new
        used = set(_vm_numbers(c, prefix))
        n = 1
        for v in vms:
            if not v.get("name"):
                while n in used:
                    n += 1
                v["name"] = vm_name(new, n, prefix)
                used.add(n)


def case_has_monitors(c):
    return bool(c.get("esx_monitors")) or any(v.get("monitors") for v in (c.get("vms") or []))


def format_vms(c):
    """기록용 VM 구성: 'esx01ev01=job_a, esx01ev02=(없음)'."""
    return ", ".join("%s=%s" % (v.get("name") or "(이름 없음)", v.get("job") or NONE_JOB) for v in (c.get("vms") or []))


def format_case_monitors(c):
    """기록용 모니터링: 'esx01ev01:guest_perf, esx01ev03:guest_perf+guest_gpu, esx01:host_pmu'."""
    parts = ["%s:%s" % (v.get("name") or "(이름 없음)", "+".join(v["monitors"])) for v in (c.get("vms") or []) if v.get("monitors")]
    if c.get("esx_monitors"):
        parts.append("%s:%s" % (c.get("esx") or "ESX", "+".join(c["esx_monitors"])))
    return ", ".join(parts)


def backup_settings(base, now=None):
    """설정 파일, 명령 목록, 테스트 케이스 표를 <생성기 폴더>/backup/<년월일시분초>/ 에 복사한다. (폴더, 복사한 파일 목록) 반환."""
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    dest = os.path.join(base, "backup", ts)
    os.makedirs(dest, exist_ok=True)
    copied = []
    for fn in (SETTINGS_FILE, RUN_FILE, MON_FILE, CASES_FILE, PROFILES_FILE):
        src = os.path.join(base, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, fn))
            copied.append(fn)
    return dest, copied


# ------------------------------------------------------------------ 데이터 폴더 (생성기 폴더와 분리)
# 데이터 파일은 생성기 폴더가 아니라 데이터 폴더에 둔다. 생성기를 다시 빌드하거나 폴더를 바꿔도 남게 하려는 것이다.
# 데이터 폴더 기본 위치: <사용자 폴더>/tcgen2_data. 위치를 바꾸면 그 경로를 설정 파일(config.json)에 적는다.
# 설정 파일 위치: Windows 는 %APPDATA%\tcgen2\config.json, 그 밖에는 ~/.config/tcgen2/config.json
# v1.x 는 tcgen_data 와 tcgen 을 쓴다. v2 는 그 두 곳을 읽기만 한다(import_from_v1).
APP_NAME = "tcgen2"
V1_APP_NAME = "tcgen"
V1_FILES = (SETTINGS_FILE, RUN_FILE, MON_FILE, CASES_FILE)
DATA_FILES = (SETTINGS_FILE, RUN_FILE, MON_FILE, CASES_FILE, PROFILES_FILE)
CONFIG_FILE = "config.json"
BACKUP_PREFIX = "tcgen2_backup_"


def config_dir():
    d = os.environ.get("TCGEN_CONFIG_DIR")          # 시험용
    if d:
        return d
    if os.name == "nt" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", APP_NAME)


def default_data_dir():
    return os.environ.get("TCGEN_DEFAULT_DATA_DIR") or os.path.join(os.path.expanduser("~"), APP_NAME + "_data")


def v1_config_dir():
    d = os.environ.get("TCGEN_V1_CONFIG_DIR")       # 시험용
    if d:
        return d
    if os.name == "nt" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], V1_APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", V1_APP_NAME)


def v1_data_dir():
    """v1.x 의 데이터 폴더: v1 의 config.json 이 가리키는 곳, 없으면 기본 위치 <사용자 폴더>/tcgen_data."""
    try:
        with open(os.path.join(v1_config_dir(), CONFIG_FILE), encoding="utf-8-sig") as f:
            d = json.load(f)
        p = (d.get("data_dir") or "").strip() if isinstance(d, dict) else ""
    except (OSError, ValueError):
        p = ""
    p = p or os.environ.get("TCGEN_V1_DATA_DIR") or os.path.join(os.path.expanduser("~"), V1_APP_NAME + "_data")
    return os.path.normpath(p)


def import_from_v1(new):
    """처음 실행: v2 데이터 폴더(new)에 데이터가 없고 v1 데이터 폴더에 데이터가 있으면 설정, 명령 목록 2개, 테스트 케이스 표를
    복사해 온다. v1 폴더의 파일은 읽기만 한다. 설정의 기록 CSV, 로그 폴더가 v1 폴더 안을 가리키면 v2 폴더의 같은 위치로 바꿔,
    v2 가 v1 폴더에 쓰지 않게 한다. (복사한 파일 이름 목록, v1 데이터 폴더) 반환."""
    old = v1_data_dir()
    if has_data(new) or not os.path.isdir(old) or same_dir(old, new):
        return [], old
    names = [fn for fn in V1_FILES if os.path.isfile(os.path.join(old, fn))]
    if not names:
        return [], old
    os.makedirs(new, exist_ok=True)
    for fn in names:
        shutil.copy2(os.path.join(old, fn), os.path.join(new, fn))
    _remap_settings_paths(old, new)
    return names, old


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
    """src 의 데이터 파일과 dirs 에 적은 하위 폴더를 dst 로 복사한다. 복사한 항목 이름 목록을 반환한다.
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
    cur = case_file_key(new, case_file(s))
    for q in [s] + [v for k, v in s["case_file_paths"].items() if k != cur]:      # 지금 쓰는 파일의 값과 나머지 파일의 값
        for key in ("record_csv", "log_dir"):
            v = (q.get(key) or "").strip()
            if v and os.path.isabs(v) and _inside(v, old):
                q[key] = os.path.join(new, os.path.relpath(v, old))
                changed = True
    if changed:
        save_settings(new, resolve_paths(new, s))


def migrate_from(old, new, now=None):
    """예전 판이 생성기 폴더(old)에 저장한 데이터를 데이터 폴더(new)로 복사한다. new 에 데이터가 없을 때만 한다.
    복사한 뒤 생성기 폴더의 데이터 파일은 old/moved_to_data_folder_<시각> 으로 옮겨, 어느 파일을 쓰는지 헷갈리지 않게 한다
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


CASE_COPY_PREFIX = "case_file_"


def copy_case_file(base, s, dst):
    """사용자가 정한 테스트 케이스 파일(데이터 폴더의 기본 파일이 아닌 것)을 dst 에 case_file_<파일 이름> 으로 복사한다.
    복사했으면 그 이름, 아니면 빈 문자열. 데이터 폴더의 기본 파일(test_cases.json)은 데이터 파일로 따로 복사된다."""
    cf = case_file(s or {})
    if case_file_key(base, cf) and os.path.isfile(cf):
        name = CASE_COPY_PREFIX + os.path.basename(cf)
        shutil.copy2(cf, os.path.join(dst, name))
        return name
    return ""


def backup_case_file(src):
    """백업 폴더(src)의 설정이 가리키는 '사용자가 정한 테스트 케이스 파일'. (그 파일의 경로, 백업에 든 사본의 경로 또는 "") 를
    돌려준다. 백업한 때 기본 파일을 쓰고 있었으면 None."""
    if not os.path.isfile(os.path.join(src, SETTINGS_FILE)):
        return None
    cf = case_file(load_settings(src))
    if not case_file_key(src, cf):
        return None
    copy = os.path.join(src, CASE_COPY_PREFIX + os.path.basename(cf))
    return os.path.normpath(cf), (copy if os.path.isfile(copy) else "")


def backup_to(base, parent, now=None, s=None):
    """데이터 폴더(base)의 데이터 파일과 testcases 폴더를 parent 아래 tcgen2_backup_<년월일시분초> 폴더로 복사한다.
    s(설정)를 주면 사용자가 정한 테스트 케이스 파일도 case_file_<파일 이름> 으로 함께 복사한다(탭 4 의 표는 그 파일에 있다)."""
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    dest = os.path.join(parent, BACKUP_PREFIX + ts)
    done = copy_data(base, dest, dirs=(DEFAULT_CASE_DIR,))
    name = copy_case_file(base, s, dest)
    if name:
        done.append("%s (지금 쓰는 테스트 케이스 파일의 사본)" % name)
    return dest, done


AUTO_BACKUP_DIR = os.path.join("backup", "auto")
AUTO_BACKUP_KEEP = 60          # 하루 두 번(오전, 오후) 만들므로 30일치


def auto_backup_slot(now):
    return now.strftime("%Y%m%d") + ("_AM" if now.hour < 12 else "_PM")


def auto_backup(base, s=None, now=None):
    """하루 두 번(오전, 오후) 데이터 파일을 backup/auto/<년월일>_<AM|PM>/ 에 복사한다. 그 시간대의 백업이 이미 있으면 하지 않는다.
    사용자가 정한 테스트 케이스 파일은 그 폴더에 case_file_<파일 이름> 으로 함께 복사한다. 오래된 백업은 AUTO_BACKUP_KEEP 개만 남긴다.
    만든 폴더를 반환한다. 만들지 않았으면 None."""
    now = now or dt.datetime.now()
    root = os.path.join(base, AUTO_BACKUP_DIR)
    dest = os.path.join(root, auto_backup_slot(now))
    if os.path.isdir(dest) or not has_data(base):
        return None
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    copy_data(base, tmp)
    copy_case_file(base, s, tmp)
    os.rename(tmp, dest)
    olds = sorted(n for n in os.listdir(root) if re.match(r"^\d{8}_(AM|PM)$", n))
    for n in olds[:-AUTO_BACKUP_KEEP]:
        shutil.rmtree(os.path.join(root, n), ignore_errors=True)
    return dest


def restore_from(src, base, now=None):
    """백업 폴더(src)의 데이터 파일로 데이터 폴더(base)의 파일을 바꾼다. 바꾸기 전 현재 파일은 base/backup/<시각>_before_restore 에
    복사한다. testcases 폴더 안 파일은 같은 이름이 없을 때만 복사한다. (복원 전 백업 폴더, 복사 항목) 반환.
    백업에 사용자가 정한 테스트 케이스 파일의 사본(case_file_<이름>)이 있으면 그 파일도 백업한 때의 내용으로 바꾼다
    (바꾸기 전 내용은 before_restore 폴더에 같은 이름으로 복사한다)."""
    if same_dir(src, base):
        raise ValueError("현재 데이터 폴더와 같은 폴더임: %s" % src)
    if not has_data(src):
        raise ValueError("이 폴더에는 tcgen 데이터 파일(%s)이 없음: %s" % (", ".join(DATA_FILES), src))
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    keep = os.path.join(base, "backup", ts + "_before_restore")
    copy_data(base, keep)
    done = copy_data(src, base, dirs=(DEFAULT_CASE_DIR,))
    _remap_settings_paths(src, base)
    got = backup_case_file(src)
    if got and got[1]:
        target, copy = got
        name = os.path.basename(copy)
        try:
            if os.path.isfile(target):
                shutil.copy2(target, os.path.join(keep, name))
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copy2(copy, target)
            done.append("%s (테스트 케이스 파일 %s 을(를) 백업한 때의 내용으로 바꿈)" % (name, target))
        except OSError as e:
            done.append("%s: 테스트 케이스 파일 %s 에 쓰지 못함(%s). [불러오기] 에서 백업 폴더의 이 사본을 고를 것" % (name, target, e))
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


AUTO_WORDS = ("", "자동", "auto", "-")


def parse_end(text):
    """리포트용 잡 종료 시각. 비우거나 '자동' 이면 None(리포트 생성기가 정함). 초까지 쓸 수 있다."""
    t = (text or "").strip()
    if t.lower() in AUTO_WORDS:
        return None
    t = t.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    raise ValueError("시각 형식이 맞지 않음: '%s' (예: 2026-11-09 21:30:00, 비우면 자동)" % text)


def fmt_short(d):
    return d.strftime("%m-%d %H:%M")


SEQ_START_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::00)?$")


def parse_seq_start(text, now=None):
    """순서 실행에서 케이스의 잡을 시작할 시각. 비우거나 '자동' 이면 None(앞 케이스가 끝난 뒤 시작).
    '10:00' 처럼 시:분만 적으면 now 뒤에 처음 오는 그 시각이고, '2026-11-20 10:00' 처럼 날짜까지 적으면 그 시각이다. 분 단위."""
    t = (text or "").strip()
    if t.lower() in AUTO_WORDS:
        return None
    m = SEQ_START_RE.match(t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError("시각 형식이 맞지 않음: '%s' (예: 10:00 또는 2026-11-20 10:00:00)" % text)
        base = (now or dt.datetime.now()).replace(second=0, microsecond=0)
        d = base.replace(hour=h, minute=mi)
        return d if d > base else d + dt.timedelta(days=1)
    try:
        return parse_dt(t)
    except ValueError:
        raise ValueError("시각 형식이 맞지 않음: '%s' (예: 10:00 또는 2026-11-20 10:00:00. 앞 케이스가 끝난 뒤 이어서 실행하려면 자동)" % text)


SEQ_OFFSET_RE = re.compile(r"^\+\s*(\d+)(?::(\d{1,2})|\.(\d+))?$")


def parse_seq_offset(text):
    """시작 시각 칸의 '+간격': 같은 ESX 호스트의 바로 앞 케이스 시작 시각에서 그만큼 뒤에 시작한다.
    '+8:30'(8시간 30분), '+8'(8시간), '+8.5'(소수 시간 = 8시간 30분). + 로 시작하지 않으면 None, 형식이 틀리면 ValueError."""
    t = (text or "").strip()
    if not t.startswith("+"):
        return None
    m = SEQ_OFFSET_RE.match(t)
    if not m or int(m.group(2) or 0) > 59:
        raise ValueError("간격 형식이 맞지 않음: '%s' (예: +8:00, +8, +8.5 = 앞 케이스 시작 시각에서 8시간, 8시간, 8시간 30분 뒤)" % text)
    minutes = int(m.group(1)) * 60
    if m.group(2) is not None:
        minutes += int(m.group(2))
    elif m.group(3) is not None:
        minutes += int(round(float("0." + m.group(3)) * 60))
    if minutes <= 0:
        raise ValueError("간격은 0 보다 커야 함: '%s'" % text)
    return dt.timedelta(minutes=minutes)


def fmt_seq_offset(td):
    total = int(td.total_seconds() // 60)
    return "+%d:%02d" % (total // 60, total % 60)


def fmt_seq_start(text):
    """시작 시각 칸에 적은 글을 저장할 모양으로 고친다: 자동은 빈 문자열, 시:분은 두 자리씩, 날짜가 있으면 '년-월-일 시:분:00',
    '+간격' 은 '+시간:분'."""
    t = (text or "").strip()
    if t.lower() in AUTO_WORDS:
        return ""
    off = parse_seq_offset(t)
    if off is not None:
        return fmt_seq_offset(off)
    m = SEQ_START_RE.match(t)
    if m:
        parse_seq_start(t)
        return "%02d:%s" % (int(m.group(1)), m.group(2))
    return fmt_dt(parse_seq_start(t))


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


# 적용 체크한 케이스는 이미 등록한 기록이다(사용자 요청 2026-11-17 "이미 적용한 테스트 기록은 시작 시간 안바뀌게"). 그 케이스의 잡 시작,
# 간격, 모니터링 중지, 잡 종료 (리포트) 는 다른 케이스를 추가하거나 고치거나 순서를 바꿔도 달라지지 않는다. 아래 함수들이 이 규칙을 지킨다.
def recalc_starts(cases, from_index=1):
    """from_index 행부터 아래로, 간격이 있는 행의 잡 시작 = 윗줄 잡 시작 + 간격 으로 다시 계산한다.
    적용 체크한 행은 바꾸지 않는다. 윗줄이 적용 체크한 케이스인 행도 다시 계산하지 않는다: 윗줄이 바뀌지 않으므로 계산할 일이 없고,
    순서를 바꿔 그 자리에 온 행의 잡 시작을 지난 케이스 기준으로 바꾸면 안 된다. from_index 행만은 사용자가 방금 간격을 고치거나 새로
    추가한 행이므로 윗줄이 적용 체크한 케이스여도 계산한다."""
    for i in range(max(1, from_index), len(cases)):
        if cases[i].get("applied") or (i != from_index and cases[i - 1].get("applied")):
            continue
        try:
            iv = parse_interval(cases[i].get("interval"))
            prev = parse_dt(cases[i - 1].get("start"))
        except ValueError:
            continue
        if iv is None:
            continue
        cases[i]["start"] = fmt_dt(prev + iv)


def interval_from_starts(cases, i):
    """i 행의 잡 시작을 직접 고쳤을 때 그 행 간격을 윗줄과의 차이로 맞춘다. 첫 행 간격과 적용 체크한 행의 간격은 그대로 둔다."""
    if i <= 0 or i >= len(cases) or cases[i].get("applied"):
        return
    try:
        cur = parse_dt(cases[i].get("start"))
        prev = parse_dt(cases[i - 1].get("start"))
    except ValueError:
        cases[i]["interval"] = ""
        return
    diff = cur - prev
    cases[i]["interval"] = fmt_interval(diff) if diff.total_seconds() > 0 else ""


def fit_interval(cases, i):
    """행을 옮기거나 지워 i 행의 윗줄이 달라졌을 때 부른다. i 행과 윗줄이 모두 적용 체크하지 않은 케이스이면 i 행의 잡 시작은 그대로 두고
    간격을 윗줄과의 차이로 맞춘다(뒤에 윗줄을 고치면 i 행이 같은 폭으로 따라 움직인다). 윗줄이 적용 체크한 케이스이면 i 행을 그대로 둔다:
    그 행은 다시 계산되지 않고(recalc_starts), 간격을 지난 케이스와의 차이(며칠)로 바꾸면 마지막 케이스의 자동 모니터링 중지 시각
    (잡 시작 + 간격)이 며칠 뒤가 된다."""
    if 0 < i < len(cases) and not cases[i].get("applied") and not cases[i - 1].get("applied"):
        interval_from_starts(cases, i)


TIME_FIELDS = ("start", "interval", "stop_mode", "stop_time", "end_time")      # 시각 지정에서 쓰는 케이스의 시각 값


def swap_cases(cases, i, j, time_mode):
    """탭 4 의 [위로], [아래로]: 이웃한 i 행과 j 행의 케이스를 서로 바꾼다.
    순서 실행(time_mode 가 False): 케이스의 값은 모두 케이스를 따라간다.
    시각 지정: 두 케이스 모두 적용 체크하지 않았으면 순서만 바꾸고 잡 시작, 간격, 모니터링 중지 설정, 잡 종료 (리포트) 는 그 자리에
    남긴다(자리에 정해 둔 시각에 넣을 케이스를 바꾸는 것). 하나라도 적용 체크한 케이스이면 값이 모두 케이스를 따라간다. 이때는 적용
    체크하지 않은 케이스의 잡 시작도 바꾸지 않고, 윗줄이 달라진 행의 간격만 맞춘다(fit_interval)."""
    a, b = cases[i], cases[j]
    follow = (not time_mode) or bool(a.get("applied")) or bool(b.get("applied"))
    if not follow:
        for key in TIME_FIELDS:
            a[key], b[key] = b.get(key, ""), a.get(key, "")
    cases[i], cases[j] = b, a
    if time_mode and follow:
        for k in (min(i, j), max(i, j), max(i, j) + 1):
            fit_interval(cases, k)


def _applied_stop(c):
    """적용 체크한 케이스에 고정해 둔 자동 모니터링 중지 시각. 적용 체크하지 않았거나 고정한 값이 없으면 None."""
    if not c.get("applied"):
        return None
    try:
        return parse_dt(c.get("applied_stop"))
    except ValueError:
        return None


def freeze_applied(cases, mon_lead_min, stop_lead_min):
    """시각 지정: 적용 체크한 케이스 가운데 모니터링 중지가 자동이고 아직 고정한 값이 없는 케이스에, 지금 표로 계산한 중지 시각을
    applied_stop 으로 적어 둔다. 자동 중지 시각은 다음 케이스의 잡 시작으로 계산하므로, 적어 두지 않으면 다음 케이스가 바뀔 때
    (추가, 삭제, 순서 바꾸기) 이미 등록한 케이스의 중지 시각이 다르게 보인다. 적용 체크하지 않은 케이스의 값은 지운다.
    바꾼 것이 있으면 True."""
    changed, sched = False, None
    for i, c in enumerate(cases):
        if not c.get("applied"):
            if c.get("applied_stop"):
                c["applied_stop"] = ""
                changed = True
            continue
        if (c.get("stop_mode") or "auto") != "auto" or _applied_stop(c) is not None:
            continue
        if sched is None:
            sched = case_schedule(cases, mon_lead_min, stop_lead_min)
        s = sched[i]
        if s.get("mon_stop") is not None and not s.get("stop_error"):
            c["applied_stop"] = fmt_dt(s["mon_stop"])
            changed = True
    return changed


def case_schedule(cases, mon_lead_min, stop_lead_min):
    """케이스마다 잡 시작, 모니터링 시작, 모니터링 중지 시각과 시각 오류를 반환한다.
    적용 체크한 케이스에 고정해 둔 자동 중지 시각(applied_stop)이 있으면 다음 케이스로 계산하지 않고 그 값을 쓴다."""
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
        from_next = False
        r["mon_start"] = starts[i] - dt.timedelta(minutes=mon_lead_min)
        mode = c.get("stop_mode") or "auto"
        if mode == "none":
            pass
        elif mode == "manual":
            try:
                r["mon_stop"] = parse_dt(c.get("stop_time"))
            except ValueError as e:
                r["stop_error"] = "모니터링 중지 " + str(e)
        elif _applied_stop(c) is not None:
            r["mon_stop"] = _applied_stop(c)
        elif i + 1 < n:
            if starts[i + 1] is not None:
                r["mon_stop"] = starts[i + 1] - dt.timedelta(minutes=stop_lead_min)
                from_next = True
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
            if from_next and starts[i + 1] <= starts[i]:
                # 바로 아랫줄이 더 이른 케이스(지난 기록)일 때: 무엇을 하면 되는지 적는다
                r["stop_error"] = ("바로 아랫줄 케이스 %s 의 잡 시작(%s)이 이 케이스의 잡 시작보다 늦지 않아 자동 중지 시각을 정할 수 없음. "
                                   "이 케이스를 그 케이스 아래로 옮기거나 모니터링 중지 칸에서 직접 입력을 고를 것"
                                   % ((cases[i + 1].get("name") or "").strip() or "(이름 없음)", fmt_dt(starts[i + 1])))
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

    def add(c, role, host, kind, name, app, t, cmd, esx, desc=""):
        line = cron_line(t, cmd, c["name"].strip(), host, esx)
        entries.append({"case": c["name"].strip(), "role": role, "host": host, "kind": kind, "name": name,
                        "app": app, "desc": desc, "when": t, "esx": esx, "cmd": cmd.strip(), "line": line})
        if t < now:
            warnings.append("%s %s %s %s: %s 는 지난 시각. cron 은 연도가 없어 내년 같은 날짜에 실행됨"
                            % (c["name"].strip(), host, name, kind, fmt_dt(t)))

    for i, c in enumerate(cases):
        if c.get("applied"):
            continue
        row = "%d번째 케이스" % (i + 1)
        name = names[i]
        if not NAME_RE.match(name):
            errors.append("%s: 케이스 이름은 영문, 숫자, . _ - 만 사용 ('%s')" % (row, name))
            continue
        if names.count(name) > 1:
            errors.append("%s: 케이스 이름 중복 '%s' (적용 체크한 행 포함). 같은 이름으로 생성하면 먼저 등록한 케이스의 "
                          "crontab 줄이 지워지고 바뀜" % (row, name))
            continue
        s = sched[i]
        esx_host = (c.get("esx") or "").strip()
        esx_mons = list(c.get("esx_monitors") or [])
        active = [v for v in (c.get("vms") or [])
                  if ((v.get("job") or NONE_JOB).strip() not in ("", NONE_JOB)) or v.get("monitors")]
        if not active and not esx_mons:
            warnings.append("%s %s: VM 마다 잡도 모니터링도 없고 ESX 모니터링도 없어 등록할 줄이 없음" % (row, name))
            continue
        if s["start_error"]:
            errors.append("%s %s: 잡 시작 %s" % (row, name, s["start_error"]))
            continue
        bad = ["VM '%s'" % (v.get("name") or "이름 없음") for v in active if not NAME_RE.match(v.get("name") or "")]
        if esx_mons and not NAME_RE.match(esx_host):
            bad.append("ESX 호스트 '%s'" % esx_host)
        if bad:
            errors.append("%s %s: 이름이 비었거나 영문, 숫자, . _ - 밖의 문자가 있음: %s" % (row, name, ", ".join(bad)))
            continue
        vm_names = [v["name"] for v in active]
        dups = sorted(set(n for n in vm_names if vm_names.count(n) > 1))
        if dups:
            errors.append("%s %s: 같은 VM 이름이 두 번 이상 있음: %s" % (row, name, ", ".join(dups)))
            continue
        has_mon = bool(esx_mons) or any(v.get("monitors") for v in active)
        if has_mon and s["stop_error"]:
            errors.append("%s %s: %s" % (row, name, s["stop_error"]))
            continue
        if has_mon and s["mon_stop"] is not None and i + 1 < len(cases) and sched[i + 1]["mon_start"] is not None \
                and case_has_monitors(cases[i + 1]) and s["mon_stop"] >= sched[i + 1]["mon_start"]:
            warnings.append("%s %s: 모니터링 중지 %s 가 다음 케이스 모니터링 시작 %s 보다 늦음"
                            % (row, name, fmt_dt(s["mon_stop"]), fmt_dt(sched[i + 1]["mon_start"])))

        def add_monitor(mname, host, esx, where_label):
            mc = mon_by.get(mname)
            if not mc:
                errors.append("%s %s: 모니터링 명령 '%s' 가 탭 2 에 없음" % (row, name, mname))
                return
            if ((mc.get("where") or "VM").strip().upper() == "ESX") != esx:
                errors.append("%s %s: 모니터링 '%s' 의 실행 위치는 %s 인데 %s 에서 골랐음. %s" % (
                    row, name, mname, "VM" if esx else "ESX", where_label,
                    "VM 행의 모니터링 칸에서 고를 것" if esx else "케이스 행의 모니터링 칸에서 고를 것"))
                return
            for e in cmd_errors(mc.get("start"), "탭 2 '%s' 시작 명령" % mname):
                errors.append(e)
            stop_cmd = (mc.get("stop") or "").strip()
            if stop_cmd:
                for e in cmd_errors(stop_cmd, "탭 2 '%s' 중지 명령" % mname):
                    errors.append(e)
            role = "ESX" if esx else "VM"
            add(c, role, host, "모니터링 시작", mname, mc.get("app", ""), s["mon_start"], mc.get("start", ""), esx,
                mc.get("desc", ""))
            if s["mon_stop"] is not None and stop_cmd:
                add(c, role, host, "모니터링 중지", mname, mc.get("app", ""), s["mon_stop"], stop_cmd, esx, mc.get("desc", ""))

        for v in active:
            for mname in v.get("monitors") or []:
                add_monitor(mname, v["name"], False, "VM 행 (%s)" % v["name"])
        for mname in esx_mons:
            add_monitor(mname, esx_host, True, "케이스 행 (ESX 모니터링)")
        for v in active:
            jn = (v.get("job") or NONE_JOB).strip()
            if jn in ("", NONE_JOB):
                continue
            rc = run_by.get(jn)
            if not rc:
                errors.append("%s %s: VM '%s' 의 잡 '%s' 가 탭 1 에 없음" % (row, name, v["name"], jn))
                continue
            for e in cmd_errors(rc.get("cmd"), "탭 1 '%s' 명령" % jn):
                errors.append(e)
            add(c, "VM", v["name"], "잡", jn, rc.get("app", ""), s["job"], rc.get("cmd", ""), False, rc.get("desc", ""))
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


# VM crontab 전체 삭제 (VM 에서 sh 로 실행). 지우기 전 내용은 임시 파일에 받은 뒤, 받기에 성공했을 때만
# VM 홈 폴더의 실행 시각 이름 파일로 옮긴다(앞서 만든 백업을 덮어쓰거나 지우지 않게).
WIPE_LINES = [
    "T=$HOME/.tcgen_crontab.tmp",
    "if crontab -l > $T 2>/dev/null; then",
    "  B=$HOME/tcgen_crontab_backup-`date +%Y%m%d%H%M%S`.txt; mv $T $B",
    "  crontab -r && echo \"== `hostname -s`  crontab deleted\" || echo \"==== `hostname -s`  crontab -r FAILED\"",
    "  echo \"   backup: $B (`wc -l < $B` lines)\"",
    "else",
    "  rm -f $T; echo \"== `hostname -s`  no crontab\"",
    "fi",
    "crontab -l 2>&1 | head -2",
]


def wipe_block(host, user, jdir=None):
    """VM crontab 전체 삭제 블록 한 대분. B 블록과 같은 계정, 같은 방법(작업 폴더의 vmssh, 시각 지정은 ~/.tcgen)으로 접속한다."""
    target = "%s@%s" % (user, host) if user else host
    out = ["sh %s/vmssh %s << TCEOF" % (jdir or DEFAULT_JUMP_DIR, target)] + [_hd(x) for x in WIPE_LINES] + ["TCEOF"]
    return "\n".join(out) + "\n"


def wipe_conflicts(cases, sched, hosts, now):
    """VM crontab 전체 삭제로 줄이 지워지는, 적용 체크한 케이스 가운데 아직 실행할 시각이 남은 케이스 이름."""
    hs, out = set(hosts), []
    for i, c in enumerate(cases):
        if not c.get("applied") or not (set(report_vms(c)) & hs):
            continue
        s = sched[i] if i < len(sched) else {}
        times = [t for t in (s.get("job"), s.get("mon_start"), s.get("mon_stop")) if t]
        if times and max(times) > now:
            out.append((c.get("name") or "").strip() or "%d번째 케이스" % (i + 1))
    return out


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
    """VM 등록 블록(전 VM 을 하나로), ESX 호스트별 블록, VM crontab 전체 삭제 블록(등록 블록과 같은 VM)을 만든다.
    같은 호스트 안에서는 시각 순서. (VM 등록 블록, ESX 블록 사전, 삭제 블록, VM 목록) 반환."""
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
    wipe = "".join(wipe_block(h, user) for h in vm_hosts)
    return vm_text, esx, wipe, vm_hosts


# ------------------------------------------------------------------ 기록
RECORD_HEADER = ["테스트 케이스 이름", "생성 시각", "ESX 호스트", "VM 구성", "잡 시작(KST)", "간격", "모니터링",
                 "모니터링 시작(KST)", "모니터링 중지(KST)", "설명", "테스트 그룹", "잡 종료(리포트)", "종료 기준 VM"]
LOG_HEADER = ["테스트 케이스 이름", "생성 시각", "구분", "대상", "실행 위치", "명령어 명", "App명", "설명", "실행 시각(KST)",
              "crontab 시각 기준", "명령", "crontab 줄", "ESX 호스트", "VM 구성", "잡 시작(KST)", "간격", "모니터링",
              "모니터링 시작(KST)", "모니터링 중지(KST)"]


def _stop_text(c, s):
    if not case_has_monitors(c):
        return ""
    if (c.get("stop_mode") or "auto") == "none":
        return "중지 없음"
    return fmt_dt(s["mon_stop"]) if s.get("mon_stop") else ""


def append_csv(path, header, rows, backup_dir=None, now=None):
    """CSV 에 행을 추가한다. 새 파일이면 머리글을 쓴다. 기존 파일은 머리글 이름으로 열을 맞춰 기록하고, 첫 열에는 항상
    테스트 케이스 이름을 넣는다. 기존 머리글에 없는 열은 머리글 끝에 덧붙이고(기존 행의 그 칸은 비움) 파일을 다시 쓴다.
    다시 쓰기 전 원본은 backup_dir(없으면 같은 폴더)에 <시각>_before_header_change_<파일 이름> 으로 복사한다.
    (덧붙인 열 이름 목록, 원본 복사 경로 또는 None) 반환."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(buf.getvalue())
        return [], None
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:4096]:
        raise ValueError("테스트 기록 CSV 를 글자로 읽을 수 없음 (암호화된 파일일 수 있음): %s" % path)
    bom = data.startswith(codecs.BOM_UTF8)
    try:
        text, enc = _decode(data)
    except UnicodeDecodeError:
        raise ValueError("테스트 기록 CSV 의 글자 인코딩을 알 수 없음 (UTF-8, CP949 아님): %s" % path)
    all_rows = list(csv.reader(io.StringIO(text)))
    existing = [h.strip() for h in (all_rows[0] if all_rows else [])]
    added = [h for h in header[1:] if h not in existing]
    cols = existing + added
    idx = {h: i for i, h in enumerate(header)}
    out_rows = []
    for r in rows:
        o = []
        for j, h in enumerate(cols):
            o.append(r[0] if j == 0 else (r[idx[h]] if h in idx else ""))
        out_rows.append(o)
    if not added:
        buf = io.StringIO()
        if data and not data.endswith(b"\n"):
            buf.write("\r\n")
        csv.writer(buf).writerows(out_rows)
        with open(path, "a", encoding=enc, newline="", errors="replace") as f:
            f.write(buf.getvalue())
        return [], None
    ts = (now or dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    bdir = backup_dir or os.path.dirname(os.path.abspath(path))
    os.makedirs(bdir, exist_ok=True)
    keep = os.path.join(bdir, "%s_before_header_change_%s" % (ts, os.path.basename(path)))
    shutil.copy2(path, keep)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in all_rows[1:]:
        w.writerow(list(r) + [""] * max(0, len(cols) - len(r)))
    w.writerows(out_rows)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=("utf-8-sig" if bom else enc), newline="", errors="replace") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)
    return added, keep


# ------------------------------------------------------------------ 리포트용 케이스 CSV (성능 리포트 생성기의 케이스 정보 파일)
REPORT_HEADER = ["테스트 그룹", "케이스명", "설명", "호스트 이름", "잡 시작 시각", "잡 종료 시각", "VM", "종료 기준 VM"]
REPORT_PREFIX = "perfreport_cases_"


def report_vms(c):
    """리포트에 넣을 VM: 잡이나 모니터링을 지정한 VM. crontab 에 등록하는 VM 과 같다."""
    return [v["name"] for v in (c.get("vms") or [])
            if v.get("name") and ((v.get("job") or NONE_JOB) != NONE_JOB or v.get("monitors"))]


def report_end_refs(c):
    """종료 기준 VM: 종료 기준으로 지정한 VM 가운데 리포트에 넣는 VM."""
    names = set(report_vms(c))
    return [v["name"] for v in (c.get("vms") or []) if v.get("end_ref") and v.get("name") in names]


def report_rows(cases, sched, picks):
    """리포트용 케이스 CSV 의 행. picks: 저장할 케이스 순번. 준 순서(탭 4 의 표 순서)대로 넣는다.
    비교에 쓰는 기준은 이 생성기에서 정하지 않는다(성능 리포트 생성기에서 정한다).
    (행 목록, 오류 목록, 경고 목록) 반환. 오류가 있으면 저장하지 않는다."""
    order = list(picks)
    rows, errors, warnings = [], [], []
    names = [(cases[i].get("name") or "").strip() for i in order]
    for n in sorted(set(x for x in names if x and names.count(x) > 1)):
        errors.append("케이스 이름 중복 '%s'. 리포트 생성기는 같은 이름의 케이스를 읽지 못한다" % n)
    for i in order:
        c, s = cases[i], (sched[i] if i < len(sched) else {})
        name = (c.get("name") or "").strip()
        label = name or "%d번째 케이스" % (i + 1)
        if not name:
            errors.append("%s: 케이스 이름이 비어 있음" % label)
        if not (c.get("esx") or "").strip():
            errors.append("%s: ESX 호스트가 비어 있음 (리포트 생성기의 필수 열 '호스트 이름')" % label)
        job = s.get("job")
        if job is None:
            errors.append("%s: 잡 시작 시각을 알 수 없음 %s" % (label, s.get("start_error") or ""))
        end = ""
        try:
            e = parse_end(c.get("end_time"))
        except ValueError as ex:
            errors.append("%s: 잡 종료 %s" % (label, ex))
            e = None
        if e is not None:
            if job is not None and e <= job:
                errors.append("%s: 잡 종료 %s 가 잡 시작 %s 보다 빠르거나 같음" % (label, fmt_dt(e), fmt_dt(job)))
            end = fmt_dt(e)
        vms = report_vms(c)
        if not vms:
            warnings.append("%s: 잡이나 모니터링을 지정한 VM 이 없어 VM 열을 비움. 리포트 생성기가 호스트 이름으로 VM 을 찾는다" % label)
        left = [v["name"] for v in (c.get("vms") or []) if v.get("end_ref") and v.get("name") and v["name"] not in vms]
        if left:
            warnings.append("%s: 종료 기준 VM %s 는 잡과 모니터링이 없어 리포트 VM 에 들어가지 않으므로 종료 기준에서도 뺌"
                            % (label, ", ".join(left)))
        rows.append([c.get("group", ""), name, c.get("desc", ""), (c.get("esx") or "").strip(),
                     fmt_dt(job) if job else "", end, ";".join(vms), ";".join(report_end_refs(c))])
    return rows, errors, warnings


def write_report_csv(path, rows, backup_dir=None, now=None):
    """리포트용 케이스 CSV 를 UTF-8(BOM) 으로 저장한다. 같은 이름의 파일이 있으면 backup_dir 에
    <시각>_before_overwrite_<파일 이름> 으로 복사한 뒤 쓴다. 복사한 경로 또는 None 반환."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    keep = None
    if os.path.exists(path) and backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        keep = os.path.join(backup_dir, "%s_before_overwrite_%s" % ((now or dt.datetime.now()).strftime("%Y%m%d%H%M%S"),
                                                                    os.path.basename(path)))
        shutil.copy2(path, keep)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(REPORT_HEADER)
    w.writerows(rows)
    try:
        _atomic_write(os.path.abspath(path), buf.getvalue(), "utf-8-sig")
    except OSError:
        try:                                # 엑셀이 파일을 열고 있어 바꾸지 못하면 임시 파일을 남기지 않는다
            os.remove(os.path.abspath(path) + ".tmp")
        except OSError:
            pass
        raise
    return keep


def write_records(settings, cases, sched, entries, run_cmds, gen_time, backup_dir=None):
    """테스트 기록 CSV 에 케이스마다 한 행 추가, 로그 폴더에 testcaselogging-<년월일시분초>.csv 저장."""
    gen_text = fmt_dt(gen_time)
    used = set(e["case"] for e in entries)
    rec_rows = []
    by_case = {}
    for i, c in enumerate(cases):
        name = (c.get("name") or "").strip()
        if name not in used or c.get("applied"):
            continue
        s = sched[i]
        by_case[name] = (c, s)
        has_mon = case_has_monitors(c)
        rec_rows.append([name, gen_text, c.get("esx", ""), format_vms(c), fmt_dt(s["job"]) if s["job"] else "",
                         c.get("interval", ""), format_case_monitors(c) if has_mon else "",
                         fmt_dt(s["mon_start"]) if (has_mon and s["mon_start"]) else "", _stop_text(c, s),
                         c.get("desc", ""), c.get("group", ""), c.get("end_time", ""), ";".join(report_end_refs(c))])
    added, keep = append_csv(settings["record_csv"], RECORD_HEADER, rec_rows, backup_dir=backup_dir, now=gen_time)
    log_rows = []
    for e in entries:
        c, s = by_case[e["case"]]
        has_mon = case_has_monitors(c)
        log_rows.append([e["case"], gen_text, e["kind"], e["host"], e["role"], e["name"], e["app"], e.get("desc", ""),
                         fmt_dt(e["when"]), "UTC" if e["esx"] else "KST", e["cmd"], e["line"], c.get("esx", ""),
                         format_vms(c), fmt_dt(s["job"]) if s["job"] else "", c.get("interval", ""),
                         format_case_monitors(c) if has_mon else "",
                         fmt_dt(s["mon_start"]) if (has_mon and s["mon_start"]) else "", _stop_text(c, s)])
    os.makedirs(settings["log_dir"], exist_ok=True)
    log_path = os.path.join(settings["log_dir"], "testcaselogging-%s.csv" % gen_time.strftime("%Y%m%d%H%M%S"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LOG_HEADER)
    w.writerows(log_rows)
    with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    return settings["record_csv"], log_path, len(rec_rows), added, keep


# ------------------------------------------------------------------ 설정 프로파일 (순서 실행에서 적용)
PROFILE_TARGETS = ("ESX", "VM")
PROFILE_TARGET_LABELS = {"ESX": "ESX 호스트", "VM": "VM"}
PROFILE_FIELDS = ("name", "target", "text", "desc")
ESX_OPT_RE = re.compile(r"^(/[A-Za-z0-9_\-]+){2,}$")
# VM 을 구별하는 값이나 파일 위치를 적는 vmx 키는 프로파일로 바꾸지 못하게 한다(바꾸면 VM 이 켜지지 않거나 다른 VM 으로 인식됨).
VM_KEY_BLOCK = ("displayname", "uuid.bios", "uuid.location", "vc.uuid", "nvram", "config.version", "virtualhw.version",
                ".encoding", "extendedconfigfile", "vmxstats.filename", "sched.swap.derivedname", "checkpoint.vmstate")


def new_profile():
    return {"name": "", "target": "VM", "text": "", "desc": ""}


# 기본 설정 프로파일: (이름, 대상, 설명, 설정 줄). 데이터 폴더에 profiles.json 이 없으면(처음 실행) 이 목록으로 채우고,
# 탭 3 의 [기본 프로파일 추가] 는 표에 없는 이름만 끝에 추가한다. 값을 비운 줄은 그 키를 vmx 에서 지운다는 뜻이다.
# 이름이나 내용을 바꾸면 README 7.4절의 표와 tests/test_seq.py 를 같이 고친다.
_LPAGE_KEYS = ("sched.mem.lpage.enable1GPage", "sched.mem.pin", "sched.mem.prealloc", "sched.mem.prealloc.pinnedMainMem",
               "sched.swap.vmxSwapEnabled")
_HALT_KEYS = ("monitor_control.halt_in_monitor", "monitor.idleLoopSpinBeforeHalt", "monitor.idleLoopMinSpinUS",
              "monitor_control.disable_pause_loop_exiting")
DEFAULT_PROFILES = (
    ("lpage_1g_on", "VM", "1GB page 키 5개 적용 (메모리 전체 예약 필요)",
     ("sched.mem.lpage.enable1GPage = TRUE", "sched.mem.pin = TRUE", "sched.mem.prealloc = TRUE",
      "sched.mem.prealloc.pinnedMainMem = TRUE", "sched.swap.vmxSwapEnabled = FALSE")),
    ("lpage_1g_off", "VM", "1GB page 키 5개 삭제", tuple("%s =" % k for k in _LPAGE_KEYS)),
    ("halt_in_monitor_on", "VM", "유휴 vCPU 처리 키 4개 적용",
     ("monitor_control.halt_in_monitor = TRUE", "monitor.idleLoopSpinBeforeHalt = TRUE", "monitor.idleLoopMinSpinUS = 50",
      "monitor_control.disable_pause_loop_exiting = TRUE")),
    ("halt_in_monitor_off", "VM", "유휴 vCPU 처리 키 4개 삭제", tuple("%s =" % k for k in _HALT_KEYS)),
    ("latency_high", "VM", "Latency Sensitivity high (CPU, 메모리 전체 예약 필요)",
     ("sched.cpu.latencySensitivity = high",)),
    ("latency_normal", "VM", "Latency Sensitivity normal (기본값)", ("sched.cpu.latencySensitivity = normal",)),
    ("vpmc_on", "VM", "vPMC 사용 (게스트 perf 하드웨어 이벤트 수집용)", ("vpmc.enable = TRUE",)),
    ("vpmc_off", "VM", "vPMC 키 삭제", ("vpmc.enable =",)),
    ("kstats_on", "VM", "vmx stats 빌드, vmware.log 보존 수 20", ("vmx.buildType = stats", "vmx.log.keepOld = 20")),
    ("kstats_off", "VM", "vmx stats 빌드 키, 로그 보존 수 키 삭제", ("vmx.buildType =", "vmx.log.keepOld =")),
    ("ht_wholecore_1", "ESX", "Cpu.HTWholeCoreThreshold = 1", ("/Cpu/HTWholeCoreThreshold = 1",)),
    ("ht_wholecore_800", "ESX", "Cpu.HTWholeCoreThreshold = 800 (기본값)", ("/Cpu/HTWholeCoreThreshold = 800",)),
)


def default_profiles():
    """기본 설정 프로파일의 목록(호출할 때마다 새 사전)."""
    return [{"name": n, "target": t, "text": "\n".join(lines) + "\n", "desc": d} for n, t, d, lines in DEFAULT_PROFILES]


def add_default_profiles(rows):
    """탭 3 의 표(rows)에 이름이 없는 기본 프로파일만 끝에 추가한다. 추가한 이름의 목록을 반환한다.
    이름이 같은 행은 내용이 달라도 그대로 둔다(사용자가 고친 내용을 덮어쓰지 않는다)."""
    have = set((r.get("name") or "").strip() for r in rows)
    added = []
    for p in default_profiles():
        if p["name"] not in have:
            rows.append(p)
            added.append(p["name"])
    return added


def _clean_profile(p):
    d = {}
    for k in PROFILE_FIELDS:
        v = p.get(k)
        d[k] = "" if v is None else str(v)
    d["name"] = d["name"].strip()
    d["target"] = "ESX" if d["target"].strip().upper().startswith("ESX") else "VM"
    d["text"] = d["text"].replace("\r\n", "\n").replace("\r", "\n")
    return d


def load_profiles(base, seed=False):
    """탭 3 설정 프로파일 표를 불러온다. 파일이 없으면 빈 목록.
    seed=True 이고 파일이 없으면(처음 실행) 기본 프로파일을 저장하고 그 목록을 반환한다.
    파일이 있으면 비어 있어도 그대로 둔다(사용자가 기본 프로파일을 지운 상태를 유지)."""
    p = os.path.join(base, PROFILES_FILE)
    if not os.path.exists(p):
        if not seed:
            return []
        rows = default_profiles()
        try:
            save_profiles(base, rows)
        except OSError:
            pass                    # 저장하지 못해도 화면에는 보인다. 다음 자동 저장 때 다시 저장한다
        return rows
    with open(p, encoding="utf-8-sig") as f:
        data = json.load(f)
    rows = data.get("profiles", []) if isinstance(data, dict) else data
    return [_clean_profile(r) for r in rows if isinstance(r, dict)]


def save_profiles(base, rows):
    data = {"version": 1, "profiles": [_clean_profile(r) for r in rows]}
    _atomic_write(os.path.join(base, PROFILES_FILE), json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def esx_option_path(key):
    """ESX 호스트 옵션 이름을 esxcli 의 형식으로 바꾼다: '구역.옵션' 은 점을 / 로 바꾸고 앞에 / 를 추가, '/구역/옵션' 은 그대로."""
    k = (key or "").strip()
    if k and not k.startswith("/"):
        k = "/" + k.replace(".", "/")
    return k


def parse_profile_text(text, target):
    """설정 내용(한 줄에 '키 = 값')을 읽어 (항목 목록, 오류 목록)을 반환한다. 항목은 (키, 값)이고 적힌 순서다.
    - 빈 줄과 # 으로 시작하는 줄은 건너뛴다.
    - vmx 줄처럼 값을 큰따옴표로 감싼 것(키 = "값")은 따옴표를 벗긴다.
    - VM 대상: 값을 비우면 그 키를 지우라는 뜻이다. 키는 대소문자를 구분하지 않는다.
    - ESX 대상: 키는 /구역/옵션 또는 구역.옵션 이고 /구역/옵션 으로 바꿔 반환한다. 값을 비울 수 없다.
    - 같은 키를 두 번 이상 적으면 마지막 값을 쓴다."""
    vm = (target or "VM").strip().upper() != "ESX"
    order, last, errors = [], {}, []
    for n, raw in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = "%d번째 줄" % n
        if "=" not in line:
            errors.append("%s: '키 = 값' 형식이 아님 (%s)" % (where, line[:60]))
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if not key:
            errors.append("%s: 키가 비어 있음" % where)
            continue
        if any(ord(ch) < 33 or ord(ch) > 126 or ch in '=#"' for ch in key):
            errors.append("%s: 키에는 ASCII 문자만 쓰고 공백, =, #, 큰따옴표를 쓸 수 없음 ('%s')" % (where, key))
            continue
        if any(ord(ch) < 32 or ord(ch) > 126 or ch == '"' for ch in value):
            errors.append("%s: 값에는 ASCII 문자만 쓰고 큰따옴표와 줄바꿈을 쓸 수 없음 (키 '%s')" % (where, key))
            continue
        if vm:
            low = key.lower()
            if low in VM_KEY_BLOCK or low.endswith(".filename"):
                errors.append("%s: '%s' 는 VM 을 구별하는 값이나 파일 위치를 적는 키라서 프로파일로 바꿀 수 없음" % (where, key))
                continue
            norm = low
        else:
            key = esx_option_path(key)
            if not ESX_OPT_RE.match(key):
                errors.append("%s: ESX 호스트 옵션 이름은 /구역/옵션 또는 구역.옵션 형식 ('%s')" % (where, key))
                continue
            if value == "":
                errors.append("%s: ESX 호스트 옵션 '%s' 의 값이 비어 있음 (호스트 옵션은 지울 수 없음)" % (where, key))
                continue
            norm = key
        if norm not in last:
            order.append(norm)
        last[norm] = (key, value)
    return [last[k] for k in order], errors


def merge_profile_items(lists, vm=True):
    """여러 프로파일의 항목을 적힌 순서로 합친다. 같은 키가 겹치면 뒤 프로파일의 값을 쓴다(VM 키는 대소문자 무시)."""
    order, last = [], {}
    for items in lists:
        for key, value in items:
            norm = key.lower() if vm else key
            if norm not in last:
                order.append(norm)
            last[norm] = (key, value)
    return [last[k] for k in order]


def format_case_profiles(c):
    """기록용 설정: 'esx01:host_a+host_b, esx01ev01:vm_pin'."""
    parts = []
    if c.get("esx_profiles"):
        parts.append("%s:%s" % (c.get("esx") or "ESX", "+".join(c["esx_profiles"])))
    parts += ["%s:%s" % (v.get("name") or "(이름 없음)", "+".join(v["profiles"])) for v in (c.get("vms") or []) if v.get("profiles")]
    return ", ".join(parts)


# ------------------------------------------------------------------ 순서 실행 (seq): 검증, plan, 블록
# 순서 실행에서 PC 는 시각을 정하지 않는다. 케이스의 내용을 plan 한 파일로 만들어 블록에 넣고,
# 점프 VM 의 ~/.tcgen/tcrun 이 plan 을 읽어 ESX 호스트별로 케이스를 표의 순서대로 진행한다. plan 형식은 DEVELOPER.md 5절.
PLAN_FORMAT = "TCGEN-PLAN 2"
DEFAULT_JUMP_LOG = "~/tcgen_log"
DEFAULT_JUMP_DIR = "~/.tcgen"
JUMP_DIR_RE = re.compile(r"^(~/|/)[A-Za-z0-9._/+@,:-]*$")
ADDR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROC_MAX = 80
SEQ_PLAN_NAMES = {"seq_settle_min": "SETTLE_MIN", "seq_gap_min": "GAP_MIN", "seq_start_spacing_min": "SPACING_MIN",
                  "seq_prep_lead_min": "PREP_LEAD_MIN", "seq_poll_sec": "POLL_SEC",
                  "seq_first_check_sec": "FIRST_CHECK_SEC", "seq_gone_count": "GONE_COUNT",
                  "seq_start_wait_min": "START_WAIT_MIN", "seq_poweron_wait_min": "POWERON_WAIT_MIN",
                  "seq_boot_wait_min": "BOOT_WAIT_MIN", "seq_shutdown_wait_min": "SHUTDOWN_WAIT_MIN"}


def run_mode(settings):
    return "time" if (settings or {}).get("run_mode") == "time" else "seq"


def jump_log_dir(settings):
    """점프 VM 로그 폴더. 비우면 ~/tcgen_log, 작업 폴더를 따로 적었으면 <작업 폴더>/log (홈 폴더를 쓰지 않게)."""
    d = ((settings or {}).get("jump_log_dir") or "").strip()
    if d:
        return d
    jd = ((settings or {}).get("jump_dir") or "").strip().rstrip("/")
    return (jd + "/log") if jd and jd != DEFAULT_JUMP_DIR else DEFAULT_JUMP_LOG


def jump_dir(settings):
    """점프 VM 작업 폴더(화면에 적은 글 그대로, 끝의 / 는 뗀다). 비우면 ~/.tcgen."""
    d = ((settings or {}).get("jump_dir") or "").strip() or DEFAULT_JUMP_DIR
    return d.rstrip("/") or d


def sh_path(path):
    """점프 VM 의 셸 글에 넣는 경로: ~/ 로 시작하면 $HOME/ 로 바꾼다."""
    if path == "~":
        return "$HOME"
    return "$HOME/" + path[2:] if path.startswith("~/") else path


def parse_esx_addr(text):
    """설정의 ESX 접속 주소('호스트이름=주소' 를 ; 로 구분)를 ({호스트 이름: 주소}, 오류 목록)으로 바꾼다."""
    out, errors = {}, []
    for part in (text or "").replace("\n", ";").split(";"):
        p = part.strip()
        if not p:
            continue
        name, eq, addr = p.partition("=")
        name, addr = name.strip(), addr.strip()
        if not eq or not HOST_RE.match(name) or not ADDR_RE.match(addr):
            errors.append("설정: ESX 접속 주소 '%s' 는 '호스트이름=주소' 형식이 아님 (여러 개는 ; 로 구분. 예: esx01=10.0.0.11;esx02=10.0.0.12)" % p)
            continue
        out[name] = addr
    return out, errors


def check_proc_name(name, label):
    """프로세스 이름 검증: ASCII 인쇄 문자, 작은따옴표와 ! 와 역슬래시 금지, 80자 이하."""
    errs = []
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in name):
        errs.append("%s: 프로세스 이름에는 ASCII 문자만 쓸 수 있음 (한글, 탭, 줄바꿈 불가)" % label)
    if any(ch in name for ch in "'!\\"):
        errs.append("%s: 프로세스 이름에 작은따옴표, !, 역슬래시를 쓸 수 없음" % label)
    if len(name) > PROC_MAX:
        errs.append("%s: 프로세스 이름은 %d자 이하 (지금 %d자)" % (label, PROC_MAX, len(name)))
    return errs


def proc_pattern(name):
    """pgrep -f 에 넘길 정규식. 프로세스 이름을 글자 그대로 찾도록 정규식 특수 문자 앞에 역슬래시를 넣는다."""
    return re.sub(r"([.\[*^$+?(){|])", r"\\\1", name)


def _has_job(v):
    return (v.get("job") or NONE_JOB).strip() not in ("", NONE_JOB)


def _one_line(text):
    return re.sub(r"[\r\n\t]+", " ", text or "").strip()


def _csv_line(fields):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)
    return buf.getvalue()


def build_seq(cases, run_cmds, mon_cmds, profiles, settings, now=None):
    """순서 실행의 검증과 케이스별 내용 계산. (케이스 목록, 오류, 경고, 공통 값) 반환. 적용 체크한 케이스는 뺀다.
    now: 시:분만 적은 시작 시각을 날짜가 있는 시각으로 바꾸고, 지난 시각인지 보는 기준(생성 시각)."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    errors, warnings, plan = [], [], []
    last_fixed = {}                     # ESX 호스트별로 앞에서 지정한 시작 시각 (순서가 거꾸로면 경고)
    prev_start, prev_name = {}, {}      # ESX 호스트별로 바로 앞 케이스의 시작 시각(자동이면 None)과 이름: '+간격' 의 기준
    fixed_at = {}                       # 지정한 시작 시각 -> 그 케이스. 모든 잡 시작은 서로 '잡 시작 최소 간격' 이상 떨어진다(ESX 호스트가 달라도)
    profiles = profiles or []
    try:
        lead = int(settings.get("mon_lead_min", DEFAULT_SETTINGS["mon_lead_min"]))
    except (TypeError, ValueError):
        lead = -1
    if lead < 0:
        errors.append("설정: 모니터링 시작 분은 0 이상의 정수")
        lead = DEFAULT_SETTINGS["mon_lead_min"]
    sets = [("MON_LEAD_MIN", lead)]
    for key, label, low in SEQ_INT_SETTINGS:
        try:
            val = int(settings.get(key, DEFAULT_SETTINGS[key]))
        except (TypeError, ValueError):
            val = None
        if val is None or val < low:
            errors.append("설정: %s 은(는) %d 이상의 정수" % (label, low))
            val = DEFAULT_SETTINGS[key]
        sets.append((SEQ_PLAN_NAMES[key], val))
    spacing = dict(sets).get("SPACING_MIN", DEFAULT_SETTINGS["seq_start_spacing_min"])
    logdir = jump_log_dir(settings)
    if not JUMP_DIR_RE.match(logdir):
        errors.append("설정: 점프 VM 로그 폴더는 / 또는 ~/ 로 시작하고 영문, 숫자, . _ - / + @ , : 만 사용 (공백과 한글 불가): '%s'" % logdir)
    jdir = jump_dir(settings)
    if not JUMP_DIR_RE.match(jdir) or jdir in ("/", "~") or "/../" in jdir + "/" or "/./" in jdir + "/":
        errors.append("설정: 점프 VM 작업 폴더는 / 또는 ~/ 로 시작하는 폴더 경로이고 영문, 숫자, . _ - / + @ , : 만 사용 "
                      "(공백과 한글 불가, . 이나 .. 경로 불가): '%s'" % jdir)
    addr, addr_errors = parse_esx_addr(settings.get("esx_addr"))
    errors += addr_errors
    user = (settings.get("vm_user") or "").strip()
    if user and not USER_RE.match(user):
        errors.append("설정: VM 접속 계정에는 영문, 숫자, . _ - 만 사용 ('%s')" % user)
    for label, rows in (("탭 1 실행 명령", run_cmds), ("탭 2 모니터링 명령", mon_cmds)):
        for n in _dup_names(rows):
            errors.append("%s: 명령어 명 '%s' 가 두 번 이상 있음" % (label, n))
        for r in rows:
            if (r.get("name") or "").strip() and not NAME_RE.match(r["name"].strip()):
                errors.append("%s: 명령어 명은 영문, 숫자, . _ - 만 사용 ('%s')" % (label, r["name"]))
    for n in _dup_names(profiles):
        errors.append("탭 3 설정 프로파일: 프로파일 이름 '%s' 가 두 번 이상 있음" % n)
    for p in profiles:
        if (p.get("name") or "").strip() and not NAME_RE.match(p["name"].strip()):
            errors.append("탭 3 설정 프로파일: 프로파일 이름은 영문, 숫자, . _ - 만 사용 ('%s')" % p["name"])
    common = {"sets": sets, "logdir": logdir, "addr": addr, "user": user, "lead": lead, "jdir": jdir}
    if errors:
        return plan, errors, warnings, common
    run_by = {r["name"].strip(): r for r in run_cmds if (r.get("name") or "").strip()}
    mon_by = {m["name"].strip(): m for m in mon_cmds if (m.get("name") or "").strip()}
    prof_by = {p["name"].strip(): p for p in profiles if (p.get("name") or "").strip()}
    names = [(c.get("name") or "").strip() for c in cases]
    checked, parsed, vm_chain = set(), {}, {}

    def cmd_errors(text, label):
        key = (label, text)
        if key in checked:
            return []
        checked.add(key)
        return check_command(text, label)

    def profile_items(pname):
        if pname not in parsed:
            p = prof_by[pname]
            items, perrs = parse_profile_text(p.get("text"), p.get("target"))
            for e in perrs:
                errors.append("탭 3 '%s': %s" % (pname, e))
            if not items and not perrs:
                warnings.append("탭 3 '%s': 설정 내용이 비어 있음" % pname)
            parsed[pname] = items
        return parsed[pname]

    for i, c in enumerate(cases):
        if c.get("applied"):
            continue
        row = "%d번째 케이스" % (i + 1)
        name = names[i]
        if not NAME_RE.match(name):
            errors.append("%s: 케이스 이름은 영문, 숫자, . _ - 만 사용 ('%s')" % (row, name))
            continue
        if names.count(name) > 1:
            errors.append("%s: 케이스 이름 중복 '%s' (적용 체크한 행 포함). 이름이 같으면 crontab 줄과 로그에서 케이스를 구별할 수 없음" % (row, name))
            continue
        label = "%s %s" % (row, name)
        esx_host = (c.get("esx") or "").strip()
        esx_mons = list(c.get("esx_monitors") or [])
        esx_profs = list(c.get("esx_profiles") or [])
        # ESX 호스트가 있는 케이스: 이름이 있는 VM 행은 모두 이 케이스의 VM 이다(전원을 맞춘다. 전원 칸이 끔이 아니면 켠다).
        # ESX 호스트가 없는 케이스: 전원을 다룰 수 없으므로 잡, 모니터링, 설정이 있는 VM 행만 쓴다.
        def _off(v):
            return (v.get("power") or "on") == "off"
        used = [v for v in (c.get("vms") or []) if _has_job(v) or v.get("monitors") or v.get("profiles") or _off(v)
                or (esx_host and (v.get("name") or "").strip())]
        jobs = [v for v in used if _has_job(v)]
        offs = [v for v in used if _off(v)]
        has_mon = bool(esx_mons) or any(v.get("monitors") for v in used)
        has_set = bool(esx_profs) or any(v.get("profiles") for v in used)
        if not jobs and not has_mon and not has_set and not offs:
            warnings.append("%s: 잡, 모니터링, 설정이 하나도 없고 전원을 끌 VM 도 없어 건너뜀" % label)
            continue
        before = len(errors)
        for v in offs:
            if _has_job(v) or v.get("monitors"):
                errors.append("%s: VM '%s' 은 전원 끔인데 잡이나 모니터링이 있음. 전원을 켬으로 바꾸거나 잡과 모니터링을 (없음)으로 할 것"
                              % (label, v.get("name") or "이름 없음"))
        if has_mon and not jobs:
            errors.append("%s: 모니터링만 있고 잡이 없음. 순서 실행은 잡의 프로세스가 끝난 것을 확인한 뒤 모니터링을 중지하므로 "
                          "잡이 하나 이상 있어야 함" % label)
        if (esx_mons or has_set or offs) and not HOST_RE.match(esx_host):
            errors.append("%s: ESX 호스트 이름이 비었거나 맞지 않음 ('%s'). 설정 적용, ESX 모니터링, VM 전원 끔에는 ESX 호스트가 있어야 하고, "
                          "이름은 영문이나 숫자로 시작해 영문, 숫자, . _ - 만 사용" % (label, esx_host))
        elif esx_host and not HOST_RE.match(esx_host):
            errors.append("%s: ESX 호스트 이름은 영문이나 숫자로 시작해 영문, 숫자, . _ - 만 사용 ('%s')" % (label, esx_host))
        bad = ["VM '%s'" % (v.get("name") or "이름 없음") for v in used if not HOST_RE.match(v.get("name") or "")]
        if bad:
            errors.append("%s: 이름이 비었거나 영문, 숫자, . _ - 밖의 문자가 있음 (영문이나 숫자로 시작해야 함): %s" % (label, ", ".join(bad)))
        vm_names = [v.get("name") or "" for v in used]
        dups = sorted(set(n for n in vm_names if n and vm_names.count(n) > 1))
        if dups:
            errors.append("%s: 같은 VM 이름이 두 번 이상 있음: %s" % (label, ", ".join(dups)))
        max_wait = 0
        try:
            td = parse_interval(c.get("max_wait"))
            max_wait = int(td.total_seconds() // 60) if td else 0
        except ValueError:
            errors.append("%s: 최대 대기는 시간:분 으로 입력 (예: 12:00, 비우면 제한 없음): '%s'" % (label, c.get("max_wait")))
        chain = esx_host or "-"
        start_at, start_off = None, None
        raw_start = (c.get("seq_start") or "").strip()
        try:
            start_off = parse_seq_offset(raw_start)
            if start_off is None:
                start_at = parse_seq_start(raw_start, now)
        except ValueError as e:
            errors.append("%s: 시작 시각 칸. %s" % (label, e))
        if (start_at is not None or start_off is not None) and not jobs:
            warnings.append("%s: 잡이 없는 케이스라 시작 시각 %s 을(를) 쓰지 않음" % (label, fmt_dt(start_at) if start_at else raw_start))
            start_at, start_off = None, None
        if start_off is not None:
            # '+간격': 이번 생성에 넣는 케이스 가운데 같은 ESX 호스트의 바로 앞 케이스 시작 시각이 기준이다
            if chain not in prev_start:
                errors.append("%s: 시작 시각 '%s' 은(는) 같은 ESX 호스트의 바로 앞 케이스 시작 시각에서 그만큼 뒤라는 뜻인데, 이번 생성에 넣은 "
                              "앞 케이스가 없음. 그 호스트의 첫 케이스에는 시각을 직접 적을 것(예: 10:00)" % (label, raw_start))
            elif prev_start[chain] is None:
                errors.append("%s: 시작 시각 '%s' 을(를) 계산할 수 없음. 같은 ESX 호스트의 바로 앞 케이스 %s 의 시작 시각이 자동이거나 "
                              "그 케이스에 잡이 없음. 앞 케이스에 시작 시각을 적거나 이 케이스에 시각을 직접 적을 것"
                              % (label, raw_start, prev_name[chain]))
            else:
                start_at = prev_start[chain] + start_off
        prev_start[chain], prev_name[chain] = start_at, name
        if start_at is not None:
            if start_at <= now:
                errors.append("%s: 시작 시각 %s 이(가) 이미 지났음 (지금 %s). 시각을 고칠 것"
                              % (label, fmt_dt(start_at), fmt_dt(now)))
            elif any(abs((start_at - t).total_seconds()) < spacing * 60 for t in fixed_at):
                t = min(fixed_at, key=lambda x: abs((start_at - x).total_seconds()))
                errors.append("%s: 시작 시각 %s 이(가) %s 의 시작 시각 %s 과 %d분 이상 떨어져 있지 않음. 모든 잡은 서로 %d분 이상 떨어져 "
                              "시작해야 함(ESX 호스트가 달라도. 설정 영역의 '잡 시작 최소 간격')"
                              % (label, fmt_dt(start_at), fixed_at[t], fmt_dt(t), spacing, spacing))
            elif chain in last_fixed and start_at < last_fixed[chain]:
                warnings.append("%s: 시작 시각 %s 이(가) 앞 케이스의 시작 시각 %s 보다 빠름. 앞 케이스가 끝난 뒤 가능한 "
                                "가장 이른 시각에 시작함" % (label, fmt_dt(start_at), fmt_dt(last_fixed[chain])))
            last_fixed[chain] = max(start_at, last_fixed.get(chain, start_at))
            fixed_at.setdefault(start_at, label)
        pc = {"index": i, "name": name, "esx": esx_host, "chain": chain, "vms": [], "host_opts": [],
              "ems": [], "emx": [], "epat": "", "max_wait": max_wait, "has_mon": has_mon, "start_at": start_at,
              "start_rel": fmt_seq_offset(start_off) if (start_off is not None and start_at is not None) else "",
              "nocheck": False, "handover": None,
              "group": _one_line(c.get("group")), "desc": _one_line(c.get("desc")),
              "report_vms": report_vms(c), "end_refs": report_end_refs(c), "entries": [], "case": c}

        def entry(role, host, kind, ename, app, desc, cmd, line):
            pc["entries"].append({"case": name, "role": role, "host": host, "kind": kind, "name": ename, "app": app,
                                  "desc": desc, "cmd": cmd, "line": line})

        def pick_profiles(pnames, want, where):
            lists = []
            for pn in pnames:
                p = prof_by.get(pn)
                if not p:
                    errors.append("%s: 설정 프로파일 '%s' 가 탭 3 에 없음 (%s)" % (label, pn, where))
                    continue
                got = "ESX" if (p.get("target") or "VM").strip().upper() == "ESX" else "VM"
                if got != want:
                    errors.append("%s: 설정 프로파일 '%s' 의 대상은 %s 인데 %s 에서 골랐음. %s" % (
                        label, pn, PROFILE_TARGET_LABELS[got], where,
                        "VM 행의 설정 칸에서 고를 것" if got == "VM" else "케이스 행의 설정 칸에서 고를 것"))
                    continue
                lists.append(profile_items(pn))
            return merge_profile_items(lists, vm=(want == "VM"))

        def monitor(mname, host, esx, where):
            mc = mon_by.get(mname)
            if not mc:
                errors.append("%s: 모니터링 명령 '%s' 가 탭 2 에 없음" % (label, mname))
                return None
            if ((mc.get("where") or "VM").strip().upper() == "ESX") != esx:
                errors.append("%s: 모니터링 '%s' 의 실행 위치는 %s 인데 %s 에서 골랐음. %s" % (
                    label, mname, "VM" if esx else "ESX", where,
                    "VM 행의 모니터링 칸에서 고를 것" if esx else "케이스 행의 모니터링 칸에서 고를 것"))
                return None
            for e in cmd_errors(mc.get("start"), "탭 2 '%s' 시작 명령" % mname):
                errors.append(e)
            stop_cmd = (mc.get("stop") or "").strip()
            if stop_cmd:
                for e in cmd_errors(stop_cmd, "탭 2 '%s' 중지 명령" % mname):
                    errors.append(e)
            role = "ESX" if esx else "VM"
            start_line = render_command(mc.get("start", ""), esx) + marker(name, host)
            entry(role, host, "모니터링 시작", mname, mc.get("app", ""), mc.get("desc", ""), (mc.get("start") or "").strip(), start_line)
            stop_line = ""
            if stop_cmd:
                stop_line = render_command(stop_cmd, esx) + marker(name, host)
                entry(role, host, "모니터링 중지", mname, mc.get("app", ""), mc.get("desc", ""), stop_cmd, stop_line)
            return start_line, stop_line

        pc["host_opts"] = pick_profiles(esx_profs, "ESX", "케이스 행 (ESX 호스트 설정)")
        for key, value in pc["host_opts"]:
            entry("ESX", esx_host, "설정", "+".join(esx_profs), "", "", "%s = %s" % (key, value), "")
        for v in used:
            vname = v.get("name") or ""
            vm = {"name": vname, "target": ("%s@%s" % (user, vname)) if user else vname, "ms": [], "mx": [], "job": "",
                  "proc": "", "proc_name": "", "job_name": "", "pat": "", "set": [], "monitors": list(v.get("monitors") or []),
                  "power": "off" if _off(v) else "on"}
            vm["set"] = pick_profiles(list(v.get("profiles") or []), "VM", "VM 행 (%s)" % vname)
            for key, value in vm["set"]:
                entry("VM", vname, "설정", "+".join(v.get("profiles") or []), "", "",
                      ('%s = "%s"' % (key, value)) if value != "" else "%s (줄 삭제)" % key, "")
            for mname in vm["monitors"]:
                got = monitor(mname, vname, False, "VM 행 (%s)" % vname)
                if got:
                    vm["ms"].append(got[0])
                    if got[1]:
                        vm["mx"].append(got[1])
            if _has_job(v):
                jn = v["job"].strip()
                rc = run_by.get(jn)
                if not rc:
                    errors.append("%s: VM '%s' 의 잡 '%s' 가 탭 1 에 없음" % (label, vname, jn))
                else:
                    for e in cmd_errors(rc.get("cmd"), "탭 1 '%s' 명령" % jn):
                        errors.append(e)
                    proc = (rc.get("proc") or "").strip()
                    first_time = ("proc", jn) not in checked
                    checked.add(("proc", jn))
                    if proc:
                        if first_time:
                            errors += check_proc_name(proc, "탭 1 '%s'" % jn)
                            if len(proc) < 3:
                                warnings.append("탭 1 '%s': 프로세스 이름 '%s' 가 3자보다 짧음. 다른 프로세스의 명령줄에도 같은 글자가 "
                                                "있으면 잡이 끝나지 않은 것으로 판정됨" % (jn, proc))
                        for mname in vm["monitors"]:
                            mc = mon_by.get(mname) or {}
                            if proc in (mc.get("start") or ""):
                                warnings.append("%s: VM '%s' 의 모니터링 '%s' 시작 명령에 프로세스 이름 '%s' 가 들어 있음. 모니터링이 "
                                                "실행되는 동안 잡이 끝나지 않은 것으로 판정될 수 있음" % (label, vname, mname, proc))
                    vm["job_name"] = jn
                    vm["job"] = render_command(rc.get("cmd", ""), False) + marker(name, vname)
                    vm["proc_name"] = proc
                    vm["proc"] = proc_pattern(proc) if proc else ""
                    entry("VM", vname, "잡", jn, rc.get("app", ""), rc.get("desc", ""), (rc.get("cmd") or "").strip(), vm["job"])
            if vm["ms"] or vm["mx"] or vm["job"]:
                vm["pat"] = _bre(marker(name, vname)) + "$"
            pc["vms"].append(vm)
            if vname:
                other = vm_chain.setdefault(vname, chain)
                if other != chain:
                    errors.append("%s: VM '%s' 가 ESX 호스트가 다른 케이스(%s, %s)에 모두 있음. ESX 호스트별 진행은 서로 기다리지 않아 "
                                  "같은 VM 을 동시에 쓰게 됨. ESX 호스트 칸을 확인할 것" % (
                                      label, vname, "ESX 호스트 없음" if other == "-" else other,
                                      "ESX 호스트 없음" if chain == "-" else chain))
        for mname in esx_mons:
            got = monitor(mname, esx_host, True, "케이스 행 (ESX 모니터링)")
            if got:
                pc["ems"].append(got[0])
                if got[1]:
                    pc["emx"].append(got[1])
        if pc["ems"] or pc["emx"]:
            pc["epat"] = _bre(marker(name, esx_host)) + "$"
        if len(errors) == before:
            plan.append(pc)
    for n, pc in enumerate(plan, 1):
        pc["seq"] = "%03d" % n
    # 잡 확인을 하는 케이스와 하지 않는 케이스 (사용자 결정 2026-11-17): 시작 시각을 적은 케이스(시각 또는 +간격)는 정해진 시각에
    # 진행하므로 잡 프로세스를 확인하지 않는다. 잡이 실행되는 동안 VM 에 접속하지 않기 위해서다. 다만 같은 ESX 호스트의 바로 다음
    # 케이스가 자동이면 그 케이스가 이 케이스의 잡 종료를 기다리므로 확인한다.
    prep = dict(sets).get("PREP_LEAD_MIN", DEFAULT_SETTINGS["seq_prep_lead_min"])
    by_chain, no_proc = {}, []
    for pc in plan:
        by_chain.setdefault(pc["chain"], []).append(pc)
    for pcs in by_chain.values():
        for k, pc in enumerate(pcs):
            label = "%d번째 케이스 %s" % (pc["index"] + 1, pc["name"])
            nxt = pcs[k + 1] if k + 1 < len(pcs) else None
            has_job = any(vm["job"] for vm in pc["vms"])
            if has_job and pc["start_at"] is not None and (nxt is None or nxt["start_at"] is not None):
                pc["nocheck"] = True
                if nxt is not None:
                    # 다음 케이스 준비를 시작하는 시각. 그 2분 전에 이 케이스의 모니터링을 중지한다
                    lead_n = max(prep, (lead if nxt["has_mon"] else 0) + 5)
                    pc["handover"] = nxt["start_at"] - dt.timedelta(minutes=lead_n)
                    if pc["handover"] - dt.timedelta(minutes=2) <= pc["start_at"] + dt.timedelta(minutes=1):
                        errors.append("%s: 다음 케이스 %s 의 준비 시작 %s 이(가) 이 케이스의 잡 시작 %s 보다 빠르거나 3분 안임. 시작 시각을 적은 "
                                      "케이스는 다음 케이스 준비 2분 전에 모니터링을 중지하고 넘어가므로 잡이 실행될 시간이 없음. 다음 케이스의 "
                                      "시작 시각을 늦추거나 설정 영역의 '시작 시각 준비' 를 줄일 것"
                                      % (label, nxt["name"], fmt_dt(pc["handover"]), fmt_dt(pc["start_at"])))
                elif not pc["max_wait"]:
                    warnings.append("%s: 이 ESX 호스트의 마지막 케이스이고 잡을 확인하지 않아 끝나는 시각이 없음. 모니터링을 중지하지 않고 "
                                    "실행을 끝냄. 최대 대기를 적으면 잡 시작 + 최대 대기에 모니터링을 중지함. 비워 두면 같은 ESX 호스트의 "
                                    "다음 실행이 준비를 시작할 때나 점프 VM 의 tcrun cancel 로 중지함" % label)
            elif has_job and pc["start_at"] is not None and any(vm["job"] for vm in nxt["vms"]):
                warnings.append("%s: 시작 시각을 적었지만 다음 케이스 %s 이(가) 자동이라 이 케이스의 잡 종료를 확인함(VM 접속). "
                                "확인을 없애려면 다음 케이스에도 시작 시각이나 +간격을 적을 것" % (label, nxt["name"]))
            elif has_job and pc["start_at"] is not None:
                warnings.append("%s: 시작 시각을 적었지만 다음 케이스 %s 이(가) 잡이 없는 케이스(설정과 VM 전원만 맞춤)라 이 케이스의 잡 종료를 "
                                "확인한 뒤에 진행함(VM 접속)" % (label, nxt["name"]))
            if has_job and not pc["nocheck"]:
                for vm in pc["vms"]:
                    if vm["job"] and not vm["proc"] and vm["job_name"] not in no_proc:
                        no_proc.append(vm["job_name"])
                        errors.append("탭 1 '%s': 프로세스 이름이 비어 있음. %s 은(는) 잡 종료를 확인하는 케이스라(시작 시각이 자동이거나 "
                                      "다음 케이스가 자동) 이 이름의 프로세스가 없어진 것으로 잡 종료를 판정함 (pgrep -f 로 찾으므로 실행 "
                                      "중인 명령줄에 들어 있는 글자를 입력)" % (vm["job_name"], label))
    return plan, errors, warnings, common


# 구성 스냅샷(config snapshot) csv 의 머리글과 구분 이름. tcrun 은 ASCII 글자만 쓰므로 한글이 든 글은 plan 의 CFGL 줄로 넘긴다.
# 열과 구분 이름은 config_snapshot 의 VM 표와 같게 둔다(구분, 항목, VM 이름 열).
SNAPSHOT_LABELS = [("h1", "구분"), ("h2", "항목"), ("time", "수집 시각"), ("basic", "VM 기본 정보"), ("adv", "vm advanced config"),
                   ("guest", "Guest OS"), ("aff", "CPU affinity"), ("ref_esx", "기준 시각 (ESX 호스트 조회)"),
                   ("ref_guest", "기준 시각 (게스트 조회)"), ("none", "(없음)")]


def plan_text(plan, common, run_id, now):
    """점프 VM 의 tcrun 이 읽는 plan. 줄 단위이고 첫 낱말이 종류다. tcrun 은 grep, sed, cut 으로만 읽는다."""
    out = [PLAN_FORMAT, "VER " + VERSION, "RUNID " + run_id, "LOGDIR " + common["logdir"], "GEN " + fmt_dt(now)]
    out += ["SET %s %d" % kv for kv in common["sets"]]
    chains = []
    for pc in plan:
        if pc["chain"] not in chains:
            chains.append(pc["chain"])
    out += ["ADDR %s %s" % (h, common["addr"][h]) for h in chains if h in common["addr"]]
    out += ["CHAIN " + h for h in chains]
    out.append("RPTH " + _csv_line(REPORT_HEADER))
    out += ["CFGL %s %s" % kv for kv in SNAPSHOT_LABELS]
    for pc in plan:
        s = pc["seq"]
        out.append("CASE %s %s %s" % (s, pc["chain"], pc["name"]))
        out.append("MAXWAIT %s %d" % (s, pc["max_wait"]))
        if pc.get("start_at"):          # 잡을 시작할 시각: 초(이 PC 의 시간대 기준 epoch)와 읽기 쉬운 글
            out.append("START %s %d %s" % (s, int(time.mktime(pc["start_at"].timetuple())), pc["start_at"].strftime("%Y-%m-%d %H:%M")))
        if pc.get("nocheck"):           # 잡을 확인하지 않는 케이스: 다음 케이스 준비를 시작하는 시각(없으면 0)
            h = pc.get("handover")
            out.append("NOCHK %s %d %s" % (s, int(time.mktime(h.timetuple())) if h else 0, h.strftime("%Y-%m-%d %H:%M") if h else "-"))
        out.append("RPTA %s %s" % (s, _csv_line([pc["group"], pc["name"], pc["desc"], pc["esx"]])))
        out.append("RPTB %s %s" % (s, _csv_line([";".join(pc["report_vms"]), ";".join(pc["end_refs"])])))
        for vm in pc["vms"]:
            out.append("VM %s %s %s" % (s, vm["name"], vm["target"]))
        out += ["OFF %s %s" % (s, vm["name"]) for vm in pc["vms"] if vm["power"] == "off"]
        out += ["AH %s %s=%s" % (s, k, v) for k, v in pc["host_opts"]]
        for vm in pc["vms"]:
            out += ["AV %s %s %s=%s" % (s, vm["name"], k, v) for k, v in vm["set"]]
        for vm in pc["vms"]:
            if vm["pat"]:
                out.append("PAT %s %s %s" % (s, vm["name"], vm["pat"]))
            out += ["MS %s %s %s" % (s, vm["name"], ln) for ln in vm["ms"]]
            if vm["job"]:
                out.append("JOB %s %s %s" % (s, vm["name"], vm["job"]))
                if vm["proc"]:
                    out.append("PROC %s %s %s" % (s, vm["name"], vm["proc"]))
            out += ["MX %s %s %s" % (s, vm["name"], ln) for ln in vm["mx"]]
        if pc["epat"]:
            out.append("EPAT %s %s" % (s, pc["epat"]))
        out += ["EMS %s %s" % (s, ln) for ln in pc["ems"]]
        out += ["EMX %s %s" % (s, ln) for ln in pc["emx"]]
        out.append("END %s" % s)
    return "\n".join(out) + "\n"


def plan_view(plan, common, run_id):
    """화면에 보이는 실행 계획. 공통 규칙을 맨 위에 한 번 적고, 케이스마다 그 케이스에만 해당하는 값을 항목별로 적는다."""
    sets = dict(common["sets"])
    lead, settle, gap = sets["MON_LEAD_MIN"], sets["SETTLE_MIN"], sets.get("GAP_MIN", 0)
    spacing, prep, shut = sets.get("SPACING_MIN", 5), sets.get("PREP_LEAD_MIN", 60), sets.get("SHUTDOWN_WAIT_MIN", 10)
    poll, first, gone = sets["POLL_SEC"], sets.get("FIRST_CHECK_SEC", 60), sets["GONE_COUNT"]
    every = ("%d분" % (poll // 60)) if poll % 60 == 0 else ("%d초" % poll)
    jdir = common.get("jdir") or DEFAULT_JUMP_DIR

    def jobs_in(pc):
        return any(vm["job"] for vm in pc["vms"])

    out = ["실행 ID %s" % run_id,
           "점프 VM 작업 폴더 %s, 로그 폴더 %s (진행 로그 tcrun-%s-<ESX 호스트>.log)" % (jdir, common["logdir"], run_id),
           "진행 확인 sh %s/tcrun status, 멈추기 sh %s/tcrun stop, 취소 sh %s/tcrun cancel" % (jdir, jdir, jdir),
           "",
           "[공통 규칙]",
           "- 순서: ESX 호스트마다 표의 순서대로 진행한다. 호스트끼리는 서로 기다리지 않는다.",
           "- 잡 시작 간격: 모든 잡 시작은 서로 %d분 이상 떨어진다(다른 ESX 호스트, 이 점프 VM 의 다른 실행 포함). "
           "가까우면 뒤에 정한 잡을 %d분 뒤로 옮긴다." % (spacing, spacing),
           "- 설정 적용: 값이 모두 같으면 VM 을 끄지 않는다. 하나라도 다르면 케이스의 VM 을 모두 끄고 바꾼다.",
           "- VM 전원: 끔인 VM 은 끈다(게스트 종료, %d분 안에 꺼지지 않으면 전원 끄기). 켬인 VM 은 표의 순서대로 하나씩 켠다. "
           "VM 마다 켜기 작업, 1GB page backing 완료(1GB page 를 쓰는 VM), ssh 로그인을 확인한 뒤 다음 VM 을 켠다." % shut]
    if settle > 0:
        out.append("- 켠 뒤 대기: VM 을 켠 케이스는 마지막 VM 의 준비가 끝나고 %d분 뒤에 잡을 시작한다. 켜져 있던 VM 만 쓰면 기다리지 않는다." % settle)
    else:
        out.append("- 켠 뒤 대기: 없음(설정값 0분).")
    out.append("- 구성 스냅샷: 잡이 있는 케이스마다 잡 시작 전에 그 케이스 VM 의 VM 설정(ESX 호스트에서 읽은 vmx 값)과 Guest OS 설정(THP "
               "등)을 로그 폴더의 tcrun-%s-<ESX 호스트>-config-<케이스 번호>-<케이스>.csv 에 기록한다. 케이스의 준비 때(%s) 설정 적용과 "
               "VM 전원 맞추기를 마친 뒤, 줄을 등록하기 전에 읽는다(켜져 있는 VM 마다 접속 1번. 읽기만 한다. 방금 켠 VM 이 있으면 마지막 "
               "VM 의 준비 60초 뒤)." % (run_id, ("시작 시각을 적은 케이스는 그 시각 %d분 전" % prep) if prep > 0
                                    else "시작 시각을 적은 케이스도 차례가 오는 대로"))
    out.append("- 모니터링: 잡 시작 %d분 전에 시작한다. 중지 명령은 잡을 확인하는 케이스는 잡 종료를 확인한 뒤에, 확인하지 않는 케이스는 "
               "케이스가 끝나는 시각에 실행한다." % lead)
    out.append("- 시작 시각: 시각이나 +간격(앞 케이스 시작 시각 기준)을 적은 케이스는 그 시각에 잡을 시작하고, 설정 적용과 VM 전원 맞추기는 "
               "%s. 자동인 케이스는 앞 케이스의 잡이 끝나고 %d분 뒤에 시작한다."
               % (("그 시각 %d분 전에 시작한다" % prep) if prep > 0 else "차례가 오는 대로 한다", gap))
    if any(pc.get("nocheck") for pc in plan):
        out.append("- 잡 확인 안 함: 시작 시각을 적은 케이스는 잡 프로세스를 확인하지 않는다. 잡이 실행되는 동안 VM 에 접속하지 않는다"
                   "(VM 접속은 준비 때 VM 마다 3~4번, 그 가운데 1번이 구성 스냅샷. 케이스가 끝난 뒤 1번). 케이스는 정해진 시각에 끝난다: 다음 케이스 준비 2분 전에 "
                   "모니터링을 중지하고 넘어간다. 최대 대기를 적었으면 잡 시작 + 최대 대기가 더 이를 때 그 시각에 끝낸다.")
    if any(jobs_in(pc) and not pc.get("nocheck") for pc in plan):
        out.append("- 잡 확인 함: 시작 시각이 자동인 케이스와 바로 다음 케이스가 자동인 케이스는 잡 종료를 알아야 하므로 확인한다. "
                   "잡 시작 %d초 뒤 한 번, 그 뒤 %s마다 VM 에서 프로세스를 찾는다. %d회 연속 없으면 종료로 본다." % (first, every, gone))
        if poll > 120:
            out.append("- 종료를 아는 시점은 실제 종료보다 최대 %s 늦다. 그래서 리포트용 CSV 의 잡 종료 시각은 비워 둔다." % every)
    chains = []
    for pc in plan:
        if pc["chain"] not in chains:
            chains.append(pc["chain"])

    def jobs_of(pc):
        return [vm for vm in pc["vms"] if vm["job"]]

    def items(label, rows, none="없음"):
        if not rows:
            out.append("   %s: %s" % (label, none))
            return
        out.append("   %s:" % label)
        out.extend("     - " + r for r in rows)

    for ch in chains:
        mine = [pc for pc in plan if pc["chain"] == ch]
        out.append("")
        if ch == "-":
            out.append("[ESX 호스트 없음] 케이스 %d개. VM 전원과 설정은 다루지 않는다. VM 이 켜져 있어야 한다." % len(mine))
        else:
            via = common["addr"].get(ch)
            out.append("[ESX 호스트 %s]%s 케이스 %d개" % (ch, (" 접속 주소 %s," % via) if via else "", len(mine)))
        for n, pc in enumerate(mine, 1):
            out.append("%d. %s" % (n, pc["name"]))
            jobs = jobs_of(pc)
            first_job = not any(jobs_of(q) for q in mine[:n - 1])
            if not jobs:
                start = "잡 없음. 설정과 VM 전원만 맞추고 다음 케이스로 넘어간다."
            elif pc.get("start_at"):
                start = "지정 %s" % pc["start_at"].strftime("%Y-%m-%d %H:%M")
                if pc.get("start_rel"):
                    start += " (앞 케이스 시작 %s)" % pc["start_rel"]
                if prep > 0:
                    start += ". 준비(설정, VM 전원)는 %s 부터" % (pc["start_at"] - dt.timedelta(minutes=prep)).strftime("%Y-%m-%d %H:%M")
            elif first_job:
                start = "자동. 이 ESX 호스트의 첫 잡이라 준비가 끝나는 대로"
            elif gap > 0:
                start = "자동. 앞 케이스의 잡이 끝나고 %d분 뒤" % gap
            else:
                start = "자동. 앞 케이스가 끝나면 바로"
            out.append("   시작: " + start)
            if ch != "-" and pc["vms"]:
                out.append("   VM 전원: " + ", ".join("%s %s" % (vm["name"], POWER_LABELS[vm["power"]]) for vm in pc["vms"]))
            sets_rows = ["ESX 호스트 %s = %s" % (k, v) for k, v in pc["host_opts"]]
            for vm in pc["vms"]:
                sets_rows += [('%s %s = "%s"' % (vm["name"], k, v)) if v != "" else "%s %s (이 키의 줄을 삭제)" % (vm["name"], k)
                              for k, v in vm["set"]]
            if ch != "-" or sets_rows:
                items("설정", sets_rows)
            if not jobs:
                continue
            by_kind = {}
            for e in pc["entries"]:
                by_kind.setdefault(e["kind"], []).append(e)
            items("모니터링", ["%s%s %s: %s" % (e["host"], " (ESX)" if e["role"] == "ESX" else "", e["name"], e["cmd"])
                              for e in by_kind.get("모니터링 시작", [])])
            items("잡 (잡이 있는 VM 은 같은 분에 시작)",
                  ["%s %s (프로세스 이름 %s): %s" % (vm["name"], vm["job_name"], vm["proc_name"], next(
                      e["cmd"] for e in by_kind.get("잡", []) if e["host"] == vm["name"])) for vm in jobs])
            if pc["has_mon"]:
                items("모니터링 중지", ["%s%s %s: %s" % (e["host"], " (ESX)" if e["role"] == "ESX" else "", e["name"], e["cmd"])
                                    for e in by_kind.get("모니터링 중지", [])], none="중지 명령 없음. crontab 줄만 지운다")
            if pc.get("nocheck"):
                ends = []
                if pc.get("handover"):
                    ends.append("%s 모니터링 중지, %s 부터 다음 케이스 준비" % (
                        (pc["handover"] - dt.timedelta(minutes=2)).strftime("%m-%d %H:%M"), pc["handover"].strftime("%H:%M")))
                if pc["max_wait"]:
                    ends.append("잡 시작 + 최대 대기 %s (%s)" % (fmt_interval(dt.timedelta(minutes=pc["max_wait"])), (
                        pc["start_at"] + dt.timedelta(minutes=pc["max_wait"])).strftime("%m-%d %H:%M")))
                if not ends:
                    end = ("정해지지 않음. 모니터링을 중지하지 않고 실행을 끝낸다. 같은 ESX 호스트의 다음 실행이 준비를 시작할 때나 "
                           "sh %s/tcrun cancel 로 중지한다" % jdir)
                else:
                    end = " 과 ".join(ends) + (" 가운데 이른 쪽" if len(ends) == 2 else "")
                out.append("   잡 확인: 안 함. 끝: %s" % end)
            else:
                out.append("   잡 확인: 함%s. 최대 대기 %s" % (
                    "(다음 케이스가 자동이라 잡 종료를 알아야 함)" if pc.get("start_at") else "",
                    fmt_interval(dt.timedelta(minutes=pc["max_wait"])) if pc["max_wait"] else "제한 없음"))
    return "\n".join(out) + "\n"


# 점프 VM 작업 폴더(기본 ~/.tcgen)의 esxssh: ESX 호스트에 키로 접속한다(비밀번호를 묻지 않음, 계정 root). 표준 입력은 그대로 넘긴다.
# LogLevel=ERROR: 로그인 배너와 'Permanently added' 안내를 출력하지 않고 오류만 출력한다. known_hosts 도 작업 폴더에 둔다.
def esxssh_lines(jdir=DEFAULT_JUMP_DIR):
    sh = sh_path(jdir)
    return [
        'O="-i %s/id_esx -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6"' % sh,
        'O="$O -o LogLevel=ERROR -o UserKnownHostsFile=%s/known_hosts"' % sh,
        "if ssh -G -o StrictHostKeyChecking=accept-new localhost >/dev/null 2>&1; then",
        '  O="$O -o StrictHostKeyChecking=accept-new"',
        "else",
        '  O="$O -o StrictHostKeyChecking=no"',
        "fi",
        'if [ $# -lt 1 ]; then echo "usage: sh %s/esxssh <esx host> [command]"; exit 2; fi' % jdir,
        'H="$1"; shift',
        'exec ssh $O -l root "$H" "$@"',
    ]


# 작업 폴더의 postsetup: A 블록의 마지막 줄이 실행한다. ESX 호스트에 키를 등록하는 한 줄을 보여 주고,
# VM 비밀번호가 아직 없을 때만 setpw 로 입력받는다(이미 있으면 바꾸는 방법만 알린다).
def postsetup_lines(jdir=DEFAULT_JUMP_DIR):
    return [
        "D=%s" % sh_path(jdir),
        "echo",
        "if [ -s $D/id_esx.pub ]; then",
        '  echo "== ESX key login. Paste this ONE line into the root SSH session of each ESX host (once per host):"',
        "  echo",
        "  echo \"echo '`cat $D/id_esx.pub`' >> /etc/ssh/keys-root/authorized_keys\"",
        "  echo",
        '  echo "== Then check from this jump VM. It must print the ESX host name without asking for a password:"',
        '  echo "sh %s/esxssh <esx host> hostname"' % jdir,
        "else",
        '  echo "==== no ssh key for ESX hosts ($D/id_esx.pub). Paste block A again."',
        "fi",
        "echo",
        "if [ -s $D/vmpw ]; then",
        '  echo "== VM password: already saved in $D/vmpw. To change it: sh %s/setpw"' % jdir,
        "else",
        "  sh $D/setpw",
        "fi",
    ]


ESXSSH_LINES = esxssh_lines()
POSTSETUP_LINES = postsetup_lines()


def vmssh_lines(jdir=DEFAULT_JUMP_DIR):
    """v1.4 의 vmssh 와 같은 글. 작업 폴더만 바꾼다(기본 폴더이면 v1.4 와 글자까지 같다)."""
    return [VMSSH_LINES[0].replace("$HOME/.tcgen", sh_path(jdir))] + VMSSH_LINES[1:]


def setpw_lines(jdir=DEFAULT_JUMP_DIR):
    """v1.4 의 setpw 와 같은 글. 작업 폴더만 바꾼다(기본 폴더이면 v1.4 와 글자까지 같다)."""
    return ["D=%s" % sh_path(jdir)] + SETPW_LINES[1:]


CK_LINES = [
    "ck() {",
    '  S=`(sha256sum "$D/$1.new" 2>/dev/null || shasum -a 256 "$D/$1.new" 2>/dev/null) | cut -c1-64`',
    '  if [ "$S" = "$2" ]; then mv -f "$D/$1.new" "$D/$1"; chmod $3 "$D/$1"; echo "OK      $D/$1"',
    '  else rm -f "$D/$1.new"; echo "FAILED  $D/$1 (sha256 differs. Paste block A again. Check the free space and quota of $D)"; fi',
    "}",
]

KEYGEN_LINES = [
    'if [ -f $D/id_esx ]; then echo "kept:    $D/id_esx (ssh key for ESX hosts)"',
    'elif ssh-keygen -t rsa -b 4096 -N "" -f $D/id_esx < /dev/null > /dev/null 2>&1; then echo "created: $D/id_esx (ssh key for ESX hosts)"',
    'else echo "==== ssh-keygen FAILED: no ssh key for ESX hosts"; fi',
]


B64_LINE = 120         # 블록에 넣는 base64 한 줄의 길이 (v1.4 블록의 긴 줄과 비슷한 길이)


def _gz_b64(data):
    """gzip 뒤 base64 로 바꿔 줄로 나눈다. mtime=0 이라 같은 내용이면 결과가 매번 같다."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as g:
        g.write(data)
    b = base64.b64encode(buf.getvalue()).decode("ascii")
    return [b[i:i + B64_LINE] for i in range(0, len(b), B64_LINE)]


def gz_b64_decode(lines):
    """_gz_b64 의 반대(시험용)."""
    return gzip.decompress(base64.b64decode("".join(lines)))


def jump_sources(jdir=DEFAULT_JUMP_DIR):
    """작업 폴더에 printf 줄로 만드는 짧은 파일들: (이름, 줄 목록)."""
    return (("vmssh", vmssh_lines(jdir)), ("setpw", setpw_lines(jdir)), ("esxssh", esxssh_lines(jdir)),
            ("postsetup", postsetup_lines(jdir)))


def jump_files(jdir=DEFAULT_JUMP_DIR):
    """순서 실행에서 점프 VM 작업 폴더(기본 ~/.tcgen)에 설치하는 파일의 이름과 내용.
    vmssh, askpass, setpw 는 v1.4 와 같은 글이고 폴더 경로만 작업 폴더를 따른다. tcrun 은 자기 위치를 작업 폴더로 쓴다."""
    import tcgen_payload as payload
    files = [("askpass", 'cat "%s/vmpw"\n' % sh_path(jdir))]
    for name, src in jump_sources(jdir):
        files.append((name, "".join(x + "\n" for x in src)))
    files += [("tcrun", payload.TCRUN_SH), ("tcgen_apply.py", payload.APPLY_PY)]
    return files


def seq_setup_block(jdir=DEFAULT_JUMP_DIR):
    """순서 실행의 A 블록(점프 VM 최초 1회): v1.4 의 askpass, vmssh, setpw 에 esxssh, postsetup, tcrun, tcgen_apply.py 를 더해
    작업 폴더(기본 ~/.tcgen)에 설치하고, ESX 접속용 ssh 키가 없으면 만든다. 큰 파일 둘은 gzip 뒤 base64 로 넣고 설치 뒤 sha256 을
    확인한다. 작업 폴더가 ~/.tcgen 이 아니면 ~/.tcgen 에 있던 ssh 키와 VM 비밀번호를 새 폴더로 복사한다(ESX 에 키를 다시 등록하지 않게)."""
    files = dict(jump_files(jdir))
    sh = sh_path(jdir)
    body = ["umask 077",
            "D=%s; mkdir -p $D/runs || { echo \"==== cannot create $D\"; exit 1; }" % sh]
    if jdir != DEFAULT_JUMP_DIR:
        body += ['for F in id_esx id_esx.pub vmpw; do',
                 '  if [ ! -s $D/$F ] && [ -s $HOME/.tcgen/$F ]; then cp -p $HOME/.tcgen/$F $D/$F && echo "copied:  $HOME/.tcgen/$F -> $D/$F"; fi',
                 'done']
    body += ["printf '%%s\\n' 'cat \"%s/vmpw\"' > $D/askpass" % sh]
    for name, src in jump_sources(jdir):
        body.append(": > $D/%s" % name)
        for v in src:
            body += _printf_lines(v, "$D/%s" % name)
    body += ["chmod 700 $D/askpass $D/vmssh $D/setpw $D/esxssh $D/postsetup",
             'echo "created: $D (askpass, vmssh, setpw, esxssh, postsetup)"']
    body += CK_LINES
    out = ["sh << TCSETUP"] + [_hd(x) for x in body]
    for name, mode in (("tcrun", "700"), ("tcgen_apply.py", "600")):
        data = files[name].encode("ascii")
        out.append(_hd("tr -d '\\r' << TCB64 | base64 --decode | gzip -dc > $D/%s.new" % name))
        out += _gz_b64(data)
        out.append("TCB64")
        out.append(_hd("ck %s %s %s" % (name, hashlib.sha256(data).hexdigest(), mode)))
    out += [_hd(x) for x in KEYGEN_LINES]
    out += ["TCSETUP", "sh %s/postsetup" % jdir]
    return "\n".join(out) + "\n"


def _seq_plan_block(plan_bytes, run_id, action, jdir=DEFAULT_JUMP_DIR):
    sha = hashlib.sha256(plan_bytes).hexdigest()
    head = ["D=%s; R=$D/runs/%s" % (sh_path(jdir), run_id),
            "V=`sh $D/tcrun version 2>/dev/null`",
            'if [ "$V" = "%s" ]; then' % VERSION,
            "  mkdir -p $R",
            "  tr -d '\\r' << TCB64 | base64 --decode | gzip -dc > $R/plan.new"]
    tail = ["  S=`(sha256sum $R/plan.new 2>/dev/null || shasum -a 256 $R/plan.new 2>/dev/null) | cut -c1-64`",
            '  if [ "$S" = "%s" ]; then' % sha,
            "    mv -f $R/plan.new $R/plan",
            "    sh $D/tcrun %s $R/plan < /dev/null" % action,
            "  else",
            '    echo "==== plan FAILED (sha256 differs). Nothing was started. Check the free space and quota of $D, then paste this block again."',
            "  fi",
            "else",
            '  echo "==== tcrun on this jump VM is ${V:-not installed}, tcgen is %s. Nothing was started. Paste block A again."' % VERSION,
            "fi"]
    out = ["sh << TCEOF"] + [_hd(x) for x in head] + _gz_b64(plan_bytes) + ["TCB64"] + [_hd(x) for x in tail] + ["TCEOF"]
    return "\n".join(out) + "\n"


def seq_check_block(plan_bytes, run_id, jdir=DEFAULT_JUMP_DIR):
    """점검 블록(P): plan 을 올리고 tcrun check 를 실행한다. 아무것도 바꾸지 않는다."""
    return _seq_plan_block(plan_bytes, run_id, "check", jdir)


def seq_run_block(plan_bytes, run_id, jdir=DEFAULT_JUMP_DIR):
    """실행 블록(S): plan 을 올리고 ESX 호스트별 진행을 백그라운드로 시작한다."""
    return _seq_plan_block(plan_bytes, run_id, "start", jdir)


RECORD_HEADER_SEQ = RECORD_HEADER + ["진행 방식", "설정", "실행 ID"]
LOG_HEADER_SEQ = LOG_HEADER + ["프로세스 이름", "설정", "실행 ID"]


def nocheck_end(pc):
    """잡을 확인하지 않는 케이스가 끝나는 시각(모니터링 중지 시각): 다음 케이스 준비 2분 전과 잡 시작 + 최대 대기 가운데 이른 쪽.
    정해지지 않으면(마지막 케이스이고 최대 대기 없음) None. tcrun 의 register 가 같은 식으로 계산한다."""
    ends = []
    if pc.get("handover"):
        ends.append(pc["handover"] - dt.timedelta(minutes=2))
    if pc.get("max_wait") and pc.get("start_at"):
        ends.append(pc["start_at"] + dt.timedelta(minutes=pc["max_wait"]))
    return min(ends) if ends else None


def write_records_seq(settings, plan, gen_time, run_id, backup_dir=None):
    """순서 실행의 기록: 테스트 기록 CSV 에 케이스마다 한 행, 로그 폴더에 testcaselogging-<시각>.csv.
    시작 시각을 적은 케이스는 계획한 시각을 적는다: 잡 시작, 모니터링 시작(잡 시작 - 모니터링 시작 분), 그리고 잡을 확인하지 않는
    케이스는 모니터링 중지(nocheck_end). 시작 시각이 자동인 케이스는 시각 칸을 비운다(실제 시각은 점프 VM 의 로그 폴더에 기록된다)."""
    gen_text = fmt_dt(gen_time)
    try:
        lead = int(settings.get("mon_lead_min", DEFAULT_SETTINGS["mon_lead_min"]))
    except (TypeError, ValueError):
        lead = DEFAULT_SETTINGS["mon_lead_min"]
    rec_rows, log_rows = [], []
    for pc in plan:
        c = pc["case"]
        has_mon = case_has_monitors(c)
        mons = format_case_monitors(c) if has_mon else ""
        start = pc.get("start_at")
        end = nocheck_end(pc) if pc.get("nocheck") else None
        t_job = fmt_dt(start) if start else ""
        t_mon = fmt_dt(start - dt.timedelta(minutes=lead)) if (start and has_mon) else ""
        t_stop = fmt_dt(end) if (end and has_mon) else ""
        when = {"잡": t_job, "모니터링 시작": t_mon, "모니터링 중지": t_stop}
        rec_rows.append([pc["name"], gen_text, c.get("esx", ""), format_vms(c), t_job, "", mons, t_mon, t_stop, c.get("desc", ""),
                         c.get("group", ""), "", ";".join(pc["end_refs"]), "순서 실행", format_case_profiles(c), run_id])
        procs = dict((vm["name"], vm["proc_name"]) for vm in pc["vms"])
        for e in pc["entries"]:
            log_rows.append([e["case"], gen_text, e["kind"], e["host"], e["role"], e["name"], e["app"], e.get("desc", ""),
                             when.get(e["kind"], ""), "순서 실행", e["cmd"], e["line"], c.get("esx", ""), format_vms(c), t_job, "", mons,
                             t_mon, t_stop, procs.get(e["host"], "") if e["kind"] == "잡" else "", format_case_profiles(c), run_id])
    added, keep = append_csv(settings["record_csv"], RECORD_HEADER_SEQ, rec_rows, backup_dir=backup_dir, now=gen_time)
    os.makedirs(settings["log_dir"], exist_ok=True)
    log_path = os.path.join(settings["log_dir"], "testcaselogging-%s.csv" % gen_time.strftime("%Y%m%d%H%M%S"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LOG_HEADER_SEQ)
    w.writerows(log_rows)
    with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    return settings["record_csv"], log_path, len(rec_rows), added, keep


def generate_seq(cases, run_cmds, mon_cmds, profiles, settings, now=None, write=True, backup_dir=None):
    """순서 실행의 [명령어 생성]. 오류가 있으면 블록과 기록을 만들지 않는다."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    plan, errors, warnings, common = build_seq(cases, run_cmds, mon_cmds, profiles, settings, now=now)
    run_id = now.strftime("%Y%m%d-%H%M%S")
    res = {"mode": "seq", "errors": errors, "warnings": warnings, "entries": [], "sched": [], "vm": "", "esx": {},
           "setup": seq_setup_block(common.get("jdir") or DEFAULT_JUMP_DIR), "check": "", "run": "", "plan": "", "plan_view": "",
           "run_id": run_id, "jump_dir": common.get("jdir") or DEFAULT_JUMP_DIR,
           "record": "", "log": "", "record_rows": 0, "record_added": [], "record_backup": None, "gen_time": now,
           "vm_wipe": "", "wipe_hosts": [], "wipe_conflicts": [], "chains": [], "cases": plan,
           "jump_log_dir": common["logdir"]}
    res["skipped"] = sum(1 for c in cases if c.get("applied"))
    if errors or not plan:
        if not plan and not errors:
            if cases and all(c.get("applied") for c in cases):
                res["errors"] = ["모든 케이스가 적용 체크되어 있어 생성할 케이스가 없음. 새로 실행할 케이스는 적용 체크를 끌 것"]
            else:
                res["errors"] = ["실행할 케이스가 없음. 적용 체크를 끈 케이스의 VM 에 잡을 지정하거나 설정 프로파일을 고를 것"]
        return res
    text = plan_text(plan, common, run_id, now)
    data = text.encode("utf-8")
    res["plan"] = text
    res["plan_view"] = plan_view(plan, common, run_id)
    res["check"] = seq_check_block(data, run_id, common["jdir"])
    res["run"] = seq_run_block(data, run_id, common["jdir"])
    res["entries"] = [e for pc in plan for e in pc["entries"]]
    for pc in plan:
        if pc["chain"] not in res["chains"]:
            res["chains"].append(pc["chain"])
        for vm in pc["vms"]:
            if vm["pat"] and vm["name"] not in res["wipe_hosts"]:
                res["wipe_hosts"].append(vm["name"])
    res["vm_wipe"] = "".join(wipe_block(h, common["user"], common["jdir"]) for h in res["wipe_hosts"])
    if write:
        res["record"], res["log"], res["record_rows"], res["record_added"], res["record_backup"] = write_records_seq(
            settings, plan, now, run_id, backup_dir=backup_dir)
    return res


def profile_warnings_time(cases):
    """시각 지정에서는 설정 프로파일을 쓰지 않는다. 고른 케이스가 있으면 경고만 한다."""
    out = []
    for i, c in enumerate(cases):
        if not c.get("applied") and (c.get("esx_profiles") or any(v.get("profiles") for v in (c.get("vms") or []))):
            out.append("%d번째 케이스 %s: 설정 프로파일은 순서 실행에서만 적용됨. 시각 지정에서는 설정을 적용하지 않음"
                       % (i + 1, (c.get("name") or "").strip()))
    return out


def generate(cases, run_cmds, mon_cmds, settings, now=None, write=True, backup_dir=None, profiles=None):
    """화면의 [명령어 생성] 한 번에 해당한다. 오류가 있으면 블록과 기록을 만들지 않는다.
    settings 의 record_csv, log_dir 는 resolve_paths 로 바꾼 절대 경로여야 한다. backup_dir: 기록 CSV 머리글을 바꾸기 전 원본 복사 위치.
    진행 방식(settings 의 run_mode)이 seq 이면 순서 실행(generate_seq), time 이면 v1.4 와 같은 시각 지정이다.
    profiles: 탭 3 설정 프로파일. 순서 실행에서만 쓴다."""
    if run_mode(settings) == "seq":
        return generate_seq(cases, run_cmds, mon_cmds, profiles, settings, now=now, write=write, backup_dir=backup_dir)
    now = (now or dt.datetime.now()).replace(microsecond=0)
    entries, errors, warnings, sched = build_entries(cases, run_cmds, mon_cmds, settings, now)
    warnings += profile_warnings_time(cases)
    res = {"mode": "time", "errors": errors, "warnings": warnings, "entries": entries, "sched": sched,
           "vm": "", "esx": {}, "setup": setup_block(), "record": "", "log": "", "record_rows": 0,
           "record_added": [], "record_backup": None, "gen_time": now,
           "vm_wipe": "", "wipe_hosts": [], "wipe_conflicts": []}
    res["skipped"] = sum(1 for c in cases if c.get("applied"))
    if errors or not entries:
        if not entries and not errors:
            if cases and all(c.get("applied") for c in cases):
                res["errors"] = ["모든 케이스가 적용 체크되어 있어 생성할 케이스가 없음. 새로 등록할 케이스는 적용 체크를 끌 것"]
            else:
                res["errors"] = ["등록할 줄이 없음. 적용 체크를 끈 케이스의 VM 에 잡이나 모니터링을 하나 이상 지정할 것"]
        return res
    res["vm"], res["esx"], res["vm_wipe"], res["wipe_hosts"] = build_blocks(entries, settings)
    res["wipe_conflicts"] = wipe_conflicts(cases, sched, res["wipe_hosts"], now)
    if write:
        res["record"], res["log"], res["record_rows"], res["record_added"], res["record_backup"] = write_records(
            settings, cases, sched, entries, run_cmds, now, backup_dir=backup_dir)
    return res
