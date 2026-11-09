"""PyInstaller 진입점. 인자 없이 실행하면 창 화면을 연다.

병렬 처리(그래프를 여러 프로세스로 나눠 그림) 규칙: PyInstaller 문서 'Common Issues and Pitfalls' 와 이슈 #4067.
  - Windows 는 spawn 방식이라 병렬 처리용 프로세스마다 이 실행 파일을 다시 시작한다.
    multiprocessing.freeze_support() 를 무거운 import 보다 먼저 불러야, 그 프로세스가 맡은 일만 하고 끝난다.
  - 콘솔 없는 실행 파일에서는 sys.stdout · sys.stderr 가 None 이다. 출력하다 멈추지 않게 빈 출력으로 바꾼다.
"""

if __name__ == '__main__':
    import os
    import sys
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    import multiprocessing
    multiprocessing.freeze_support()   # 병렬 처리용 프로세스는 여기서 맡은 그래프를 그리고 끝난다

    from perfreport.__main__ import main
    sys.exit(main(sys.argv[1:] or ['gui']))
