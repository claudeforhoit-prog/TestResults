#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""저장소에 올리기 전에 이전 판과 지금 폴더를 비교해 빠지거나 생긴 항목을 보인다(DEVELOPER.md 7절).

2026-11-20 사용자 지적 "왜 이렇게 마음대로 기능을 뺴는 현상이 생기는거야?" 에 따른 재발 방지. v2.12 에서 탭 4 의 모니터링 중지 칸이
요청 없이 빠졌는데 시험도 문서도 그것을 알리지 않았다. 이 도구는 다음을 비교한다.
  - 탭 4 에 보이는 칸(순서 실행 화면), 탭 1~3 의 칸
  - 화면의 글자(단추, 칸 이름, 선택지)
  - 설정 키(DEFAULT_SETTINGS)
  - plan 의 줄 종류
  - DEVELOPER.md 0절의 사용자 결정 번호
빠진 항목이 사용자가 요청한 변경이 아니면, 올리기 전에 사용자에게 묻는다.

사용: python tools/release_diff.py <이전 태그> [저장소 폴더]
      저장소 폴더를 생략하면 이 폴더의 위 폴더(저장소의 tcgen_v2 폴더 안에서 실행할 때)를 쓴다.
      저장소 안의 경로는 tcgen_v2/<파일> 이다. 빠진 항목이 있으면 종료 코드 1."""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("tcgen_app.py", "tcgen_core.py", "DEVELOPER.md")


def old_text(repo, tag, name):
    r = subprocess.run(["git", "-C", repo, "show", "%s:tcgen_v2/%s" % (tag, name)], capture_output=True)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace")


def new_text(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def func_body(src, name):
    m = re.search(r"\n    def %s\(.*?(?=\n    def |\nclass |\Z)" % name, src, re.S)
    return m.group(0) if m else ""


def _hide(app, name):
    m = re.search(r"^%s = \(([^)]*)\)" % name, app, re.M)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def tab4_visible(app):
    """사용자가 고를 수 있는 화면 모두에서 탭 4 에 보이는 칸. 진행 방식 칸(v2.11 까지)이 있으면 순서 실행과 시각 지정 화면을
    합친다. v2.12 부터는 순서 실행 화면 하나다(시각 지정 화면은 코드에 남아 있지만 고를 수 없다)."""
    body = func_body(app, "build_case_tab")
    cols = re.findall(r'dict\(key="([^"]+)", title="([^"]+)"', body)
    hides = [_hide(app, "SEQ_HIDE")]
    if 'text="진행 방식"' in app:
        hides.append(_hide(app, "TIME_HIDE"))
    return ["%s (%s)" % (t, k) for k, t in cols if any(k not in h for h in hides)]


def tab_cols(app, name):
    return ["%s (%s)" % (t, k) for k, t in re.findall(r'dict\(key="([^"]+)", title="([^"]+)"', func_body(app, name))]


def ui_texts(app):
    return sorted(set(re.findall(r'text="([^"%{}]+)"', app)))


def settings_keys(core):
    m = re.search(r"^DEFAULT_SETTINGS = \{(.*?)^\}", core, re.S | re.M)
    return sorted(set(re.findall(r'^\s+"([a-z_0-9]+)":', m.group(1), re.M))) if m else []


def plan_kinds(core):
    m = re.search(r"\ndef plan_text\(.*?(?=\ndef )", core, re.S)
    body = m.group(0) if m else ""
    return sorted(set(re.findall(r'"([A-Z]{2,8}) ', body)) | ({"TCGEN-PLAN"} if "PLAN_FORMAT" in body else set()))


def decisions(dev):
    m = re.search(r"\n## 0\. .*?(?=\n## 1\. )", dev, re.S)
    return sorted(set(re.findall(r"^\| (D\d+) \|", m.group(0), re.M))) if m else []


def compare(title, old, new):
    gone = [x for x in old if x not in new]
    added = [x for x in new if x not in old]
    print("== %s: 이전 %d, 지금 %d" % (title, len(old), len(new)))
    for x in gone:
        print("   빠짐: %s" % x)
    for x in added:
        print("   생김: %s" % x)
    return gone


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    tag = argv[1]
    repo = argv[2] if len(argv) > 2 else os.path.dirname(HERE)
    old = dict((n, old_text(repo, tag, n)) for n in FILES)
    if not old["tcgen_app.py"]:
        print("이전 판을 읽지 못함: %s 의 %s:tcgen_v2/tcgen_app.py" % (repo, tag))
        return 2
    new = dict((n, new_text(n)) for n in FILES)
    gone = []
    gone += compare("탭 4 에 보이는 칸(고를 수 있는 화면 전체)", tab4_visible(old["tcgen_app.py"]), tab4_visible(new["tcgen_app.py"]))
    for name, title in (("build_run_tab", "탭 1 의 칸"), ("build_mon_tab", "탭 2 의 칸"), ("build_prof_tab", "탭 3 의 칸")):
        gone += compare(title, tab_cols(old["tcgen_app.py"], name), tab_cols(new["tcgen_app.py"], name))
    gone += compare("화면의 글자(단추, 칸 이름, 선택지)", ui_texts(old["tcgen_app.py"]), ui_texts(new["tcgen_app.py"]))
    gone += compare("설정 키", settings_keys(old["tcgen_core.py"]), settings_keys(new["tcgen_core.py"]))
    gone += compare("plan 의 줄 종류", plan_kinds(old["tcgen_core.py"]), plan_kinds(new["tcgen_core.py"]))
    gone += compare("사용자 결정 번호(DEVELOPER.md 0절)", decisions(old["DEVELOPER.md"]), decisions(new["DEVELOPER.md"]))
    if gone:
        print("빠진 항목 %d개. 사용자가 요청한 변경이 아니면 올리기 전에 사용자에게 묻는다." % len(gone))
        return 1
    print("빠진 항목 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
