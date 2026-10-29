"""견본 데이터 생성: 실제 시스템과 무관한 합성 데이터.

  python samples/make_sample_data.py

결과
  samples/sample_cases.csv                    케이스 정보 CSV (3건)
  samples/sample_data/esx01_vm_metrics.csv    VCF Operations VM 지표 export 형식 (호스트 esx01, VM 4대)
  samples/sample_data/node7_vm_metrics.csv    같은 형식 (호스트 hv-node7, VM 4대: VM 이름이 호스트 이름으로 시작하지 않는 예)
  samples/sample_data/logs/node7_guest.log    Key:Value 텍스트 로그 형식 (hv-node7 VM 의 게스트 메모리)
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'sample_data')
DAY = '2026-01-05'
# 케이스: (이름, 설명, 호스트, 시작, 종료, 잡 실행 중 CPU 사용률 기준값)
CASES = [
    ('CASE-A', '기본 설정 (기준)', 'esx01', f'{DAY} 10:00:00', f'{DAY} 14:00:00', 72),
    ('CASE-B', '튜닝 설정 적용', 'esx01', f'{DAY} 15:00:00', f'{DAY} 19:00:00', 63),
    ('CASE-C', '다른 호스트 · 기본 설정', 'hv-node7', f'{DAY} 10:00:00', f'{DAY} 14:30:00', 75),
]
VMS = {'esx01': ['esx01-vm01', 'esx01-vm02', 'esx01-vm03', 'esx01-vm04'],
       'hv-node7': ['app-a', 'app-b', 'db-main', 'batch']}       # 호스트마다 VM 이름 규칙이 다르다
VM_PATTERN = {'esx01': '', 'hv-node7': 'app-*;db-main;batch'}  # 케이스 정보 CSV 의 VM 열 (이름 규칙이 호스트와 무관할 때)
FILE = {'esx01': 'esx01', 'hv-node7': 'node7'}
GROUP = {'CASE-A': 'tuning', 'CASE-B': 'tuning', 'CASE-C': 'host-compare'}   # 테스트 그룹: 그룹마다 HTML 1개


def level(t, host):
    """시각별 부하 수준 (0~1) 과 CPU 기준값. 잡이 없는 시간은 유휴."""
    for _, _, h, s, e, base in CASES:
        if h == host and pd.Timestamp(s) <= t <= pd.Timestamp(e):
            frac = (t - pd.Timestamp(s)) / (pd.Timestamp(e) - pd.Timestamp(s))
            phase = 0.6 if frac < 0.15 else (1.0 if frac < 0.85 else 0.5)     # 준비 · 본 계산 · 마무리
            return phase, base
    return 0.0, 0


def make_host(host, rng):
    times = pd.date_range(f'{DAY} 09:00:00', f'{DAY} 20:00:00', freq='60s')
    rows = []
    for i, vm in enumerate(VMS[host]):
        weight = [1.0, 0.85, 0.6, 0.15][i]                  # VM 마다 부하 비중이 다르다
        for t in times:
            ph, base = level(t, host)
            cpu = max(0.5, ph * base * weight + rng.normal(3, 2))
            rows.append({
                'Name': vm,
                'timestamp': t.strftime('%d %b %Y, %H:%M:%S'),
                'CPU|Usage (%)': round(min(cpu, 100), 2),
                'CPU|Ready (%)': round(max(0, 0.05 + ph * weight * rng.normal(0.8, 0.3)), 3),
                'CPU|Co-stop (%)': round(max(0, ph * weight * rng.normal(0.3, 0.15)), 3),
                'Memory|Usage (%)': round(min(100, 20 + ph * 55 * weight + rng.normal(0, 1.5)), 2),
                'Guest|Used Memory (GB)': round(4 + ph * 48 * weight + rng.normal(0, 0.4), 2),
                'Guest|CPU Context Switch Rate': f'{max(50, ph * 9000 * weight + rng.normal(400, 150)):,.2f}',
                'Virtual Disk:Aggregate of all instances|Read Latency (ms)': round(max(0, 0.4 + ph * rng.normal(1.2, 0.4)), 2),
                'Virtual Disk:Aggregate of all instances|Write Latency (ms)': round(max(0, 0.6 + ph * rng.normal(1.8, 0.5)), 2),
            })
    pd.DataFrame(rows).to_csv(os.path.join(OUT, f'{FILE[host]}_vm_metrics.csv'), index=False)


def make_log(host, rng):
    os.makedirs(os.path.join(OUT, 'logs'), exist_ok=True)
    times = pd.date_range(f'{DAY} 09:00:00', f'{DAY} 20:00:00', freq='30s')
    with open(os.path.join(OUT, 'logs', f'{FILE[host]}_guest.log'), 'w', encoding='utf-8') as f:
        for t in times:
            ph, _ = level(t, host)
            for i, vm in enumerate(VMS[host][:2]):
                used = 3000 + ph * 40000 * [1.0, 0.8][i] + rng.normal(0, 300)
                f.write(f'{t:%Y-%m-%dT%H:%M:%S}+09:00 guest-metrics Timestamp:{t:%Y-%m-%d %H:%M:%S}, Hostname:{vm}, '
                        f'Mem_Used_MB:{used:.0f}, Swap_Used_MB:0, PgFault_Delta:{max(0, ph * 50000 + rng.normal(2000, 800)):.0f}\n')


def main():
    os.makedirs(OUT, exist_ok=True)
    rng = np.random.default_rng(7)
    for f in os.listdir(OUT) if os.path.isdir(OUT) else []:
        if f.endswith('.csv'):
            os.remove(os.path.join(OUT, f))
    for h in ('esx01', 'hv-node7'):
        make_host(h, rng)
    make_log('hv-node7', rng)
    pd.DataFrame([{'테스트 그룹': GROUP[c[0]], '케이스명': c[0], '설명': c[1], '호스트 이름': c[2], '잡 시작 시각': c[3], '잡 종료 시각': c[4], 'VM': VM_PATTERN[c[2]]}
                  for c in CASES]).to_csv(os.path.join(HERE, 'sample_cases.csv'), index=False, encoding='utf-8-sig')
    print('완료:', OUT)


if __name__ == '__main__':
    main()
