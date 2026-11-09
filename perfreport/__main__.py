"""명령줄 실행.

  perfreport run <케이스정보.csv> <데이터폴더> [--out 폴더] [--title 제목] [--baseline 케이스명] [--core]
                 [--roles 지표역할.csv] [--no-unassigned] [--split 정리폴더]
                 [--palette 1~7] [--chart-width cm] [--workers N] [--selection HTML선택파일.json]
                 [--to-gb] [--to-gbps]
                                        케이스 정보 CSV 로 리포트 생성 (기본 방식)
  perfreport split <케이스정보.csv> <원본폴더> <정리폴더>
                                        원본에서 TC 별 구간만 잘라 정리폴더/<케이스명>/ 에 저장 (리포트 없이)
  perfreport build <작업파일.yaml>      작업 파일로 리포트 생성
  perfreport gui                        창 화면으로 실행
  perfreport inspect <파일> [프로파일]   입력 파일에서 읽히는 지표·개체·시각 범위 확인
"""
import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog='perfreport')
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run'); r.add_argument('cases'); r.add_argument('data')
    r.add_argument('--out'); r.add_argument('--title'); r.add_argument('--baseline')
    r.add_argument('--split', help='TC 별 정리 폴더 (지정하면 원본에서 TC 구간만 잘라 저장)')
    r.add_argument('--roles', help='지표 역할 파일 (metric_roles.csv). 없으면 기본 리포트 양식')
    r.add_argument('--no-unassigned', action='store_true', help='역할을 지정하지 않은 지표를 문서·HTML 에 넣지 않음')
    r.add_argument('--palette', help='문서 그래프 색 계열: 1 파랑(기본) · 2 주황·빨강 · 3 파랑·주황 교차 · 4 초록 · 5 보라·분홍 · 6 갈색·노랑 · 7 회색')
    r.add_argument('--chart-width', type=float, help='문서 그래프 폭 cm (기본 16.5, 최대 31.5). 16.5 를 넘으면 가로 방향 페이지, '
                                                     '컨플루언스로 가져올 문서는 31.5 권장')
    r.add_argument('--workers', type=int, help='그래프를 나눠 그릴 프로세스 수 (기본: CPU 수 - 1, 최대 6. 1 이면 순서대로)')
    r.add_argument('--selection', help="HTML 의 '선택 내보내기' 파일. 그 케이스 · VM · 지표만으로 만든다")
    r.add_argument('--to-gb', action='store_true', help='크기 단위(Bytes · KB · MB 등)를 GB 로 변환 (1 GB = 1024 MB)')
    r.add_argument('--to-gbps', action='store_true', help='네트워크 속도(KBps · Mbps 등)를 Gbps 로 변환 (1 Gbps = 1000 Mbps)')
    sp = sub.add_parser('split'); sp.add_argument('cases'); sp.add_argument('data'); sp.add_argument('out')
    r.add_argument('--core', action='store_true', help='VM 핵심 지표만 (profiles/analysis/vm_core.yaml)')
    b = sub.add_parser('build'); b.add_argument('job')
    sub.add_parser('gui')
    i = sub.add_parser('inspect'); i.add_argument('file'); i.add_argument('profile', nargs='?')
    a = ap.parse_args(argv)
    if a.cmd == 'run':
        from .build import run_csv
        run_csv(a.cases, a.data, a.out, a.title, a.baseline,
                'profiles/analysis/vm_core.yaml' if a.core else None, split_dir=a.split,
                roles=a.roles, include_unassigned=not a.no_unassigned,
                palette=a.palette, chart_width_cm=a.chart_width, workers=a.workers, selection=a.selection,
                to_gb=a.to_gb, to_gbps=a.to_gbps)
    elif a.cmd == 'split':
        from .build import run_csv
        run_csv(a.cases, a.data, split_dir=a.out, report=False)
    elif a.cmd == 'build':
        from .build import run
        run(a.job)
    elif a.cmd == 'inspect':
        from .build import APP_DIR, load_profiles
        from .ingest import load_source
        profs = load_profiles(os.path.join(APP_DIR, 'profiles', 'inputs'))
        names = [a.profile] if a.profile else list(profs)
        for n in names:
            try:
                df = load_source({'path': a.file, 'profile': n}, profs[n])
            except Exception as e:
                print(f'[{n}] 읽기 실패: {type(e).__name__}: {e}')
                continue
            if df.empty:
                print(f'[{n}] 읽힌 값 없음'); continue
            print(f'[{n}] 값 {len(df):,}개 · 시각 {df.t.min()} ~ {df.t.max()}')
            print('  개체:', ', '.join(sorted(df.entity.unique())))
            for m, u in df[['metric', 'unit']].drop_duplicates().values:
                print(f'  지표: {m}' + (f'  [{u}]' if u else ''))
    else:
        from .gui import main as gui_main
        gui_main()


if __name__ == '__main__':
    sys.exit(main())
