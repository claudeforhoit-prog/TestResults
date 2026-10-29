"""PyInstaller 진입점. 인자 없이 실행하면 창 화면을 연다."""
import sys

from perfreport.__main__ import main

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:] or ['gui']))
