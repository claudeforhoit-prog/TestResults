# -*- coding: utf-8 -*-
"""
make_payload.py - jump/tcrun.sh 와 jump/tcgen_apply.py 의 내용을 tcgen_payload.py 로 옮긴다.

tcgen 은 점프 VM 에 설치할 두 파일의 내용을 A 블록에 넣는다. exe 로 빌드해도 그 내용이 함께 들어가도록
파이썬 모듈(tcgen_payload.py)의 문자열 상수로 둔다. jump/ 의 파일을 고친 뒤에는 이 스크립트를 실행한다.

    python tools/make_payload.py           tcgen_payload.py 를 다시 만든다
    python tools/make_payload.py --check   tcgen_payload.py 가 jump/ 의 파일과 같은지만 확인한다 (다르면 종료 코드 1)
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = (("TCRUN_SH", os.path.join("jump", "tcrun.sh")), ("APPLY_PY", os.path.join("jump", "tcgen_apply.py")))
HEAD = '''# -*- coding: utf-8 -*-
"""
tcgen_payload.py - 점프 VM 에 설치하는 파일의 내용 (자동 생성. 직접 고치지 않는다)

원본은 jump/tcrun.sh 와 jump/tcgen_apply.py 이다. 원본을 고친 뒤 python tools/make_payload.py 로 다시 만든다.
tests/test_seq.py 가 이 파일과 원본이 같은지 확인한다.
"""
'''


def build():
    out = [HEAD]
    for name, rel in SOURCES:
        with io.open(os.path.join(BASE, rel), encoding="ascii", newline="") as f:
            text = f.read()
        if "\r" in text:
            raise SystemExit("%s: CR 문자가 있음. LF 로 저장할 것" % rel)
        out.append("\n%s = \"\".join([\n" % name)
        for line in text.splitlines(True):
            out.append("    %r,\n" % line)
        out.append("])\n")
    return "".join(out)


def main(argv):
    target = os.path.join(BASE, "tcgen_payload.py")
    text = build()
    if "--check" in argv:
        try:
            with io.open(target, encoding="utf-8", newline="") as f:
                same = f.read() == text
        except IOError:
            same = False
        print("tcgen_payload.py: %s" % ("jump/ 의 파일과 같음" if same else "jump/ 의 파일과 다름. python tools/make_payload.py 실행 필요"))
        return 0 if same else 1
    with io.open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("tcgen_payload.py 를 만들었음 (%d 줄)" % text.count("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
