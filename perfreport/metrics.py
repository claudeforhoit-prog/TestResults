"""지표 분류와 표시 순서: 데이터에서 읽은 지표 이름만 보고 정한다. 수집 지표는 시점·시험마다 달라도 된다.

분류 규칙 (위에서 먼저 맞는 것)
  1. OVERRIDES: 이름 앞부분과 상관없이 정하는 분류. CPU 캐시·TLB·IPC 계열은 CPU, 서버 기준 CPU 사용률은 CPU.
  2. 'CPU|Usage (%)' 처럼 '|' 가 있으면 '|' 앞부분이 분류 (VCF Operations 표기). 'Guest' 는 Guest 로 둔다.
     'Virtual Disk:Aggregate…' 처럼 ':' 뒤 수식어는 뗀다.
  3. '|' 가 없으면 이름의 낱말로 분류한다.
  4. 어디에도 맞지 않으면 '기타'.

표시 순서: 기존 성능 리포트에서 확정한 순서를 따른다 (중요도 높은 지표 먼저).
  CPU     : CPU 사용률(%) → 서버 기준 사용률 → 경합(Co-stop · Ready · Peak) → CPU GHz → TLB → 캐시 miss rate → IPC → Instructions
  Memory  : 메모리 사용률 → 사용량 → THP coverage → AnonHugePages → AnonPages → THP fault · collapse · fallback → swap
  Guest   : context switch → 사용 메모리 → 여유 메모리 → 잔여 swap → page in · page out
  Disk    : read · write 지연 → read · write IOPS → 처리량
  Network : 송신 · 수신 속도 → 패킷
  순서표에 없는 지표는 각 분류 끝에 이름순.
"""
import re

# 지표 역할: 성능평가방법론의 '지표 역할별 뜻과 예' 표 순서. 리포트 절 순서도 이 순서다.
ROLES = ['핵심 성능 지표', '원인 분석 지표', '세부 분석 지표', '상충 지표', '측정 조건 확인 항목']
ROLE_DESC = {
    '핵심 성능 지표': '기준 실행 대비 결과 비교에 사용하는 지표',
    '원인 분석 지표': '변경 설정의 영향을 직접 받는 커널 또는 PMU(Performance Monitoring Unit) 원본 카운터로, 핵심 성능 지표 차이의 원인을 자원 수준에서 설명하는 지표',
    '세부 분석 지표': '핵심 성능 지표 또는 원인 분석 지표가 예상과 다를 때 원인 범위를 한정하는 지표',
    '상충 지표': '핵심 성능 지표 또는 원인 분석 지표가 기대 방향으로 변할 때 반대 방향으로 변할 수 있는 다른 성능 지표',
    '측정 조건 확인 항목': '기준 실행과 비교 실행에서 같게 유지할 조건이 실제로 같았는지 확인하는 항목',
}

OVERRIDES = [   # (정규식, 분류): 대소문자 무시
    (r'cache[ _]*miss|cache[ _]*ref|llc|l1d|l2_|tlb|ipc|instructions?\b|cycles|branch', 'CPU'),
    (r'resource-utilization', 'CPU'),
]
KEYWORDS = [   # (분류, 이름에 들어 있으면 그 분류로 보는 낱말): '|' 가 없는 이름에만 쓴다
    ('CPU', ['cpu', 'ready', 'costop', 'co-stop', 'context_switch', 'contextswitch', 'load']),
    ('Memory', ['mem', 'swap', 'page', 'fault', 'huge', 'anon', 'thp', 'cache_buffer', 'shmem', 'numa', 'psi_mem', 'mapped', 'kernel']),
    ('Disk', ['disk', 'iops', 'latency', 'read', 'write', 'io_']),
    ('Network', ['net', 'packet', 'rx', 'tx', 'transmit', 'receive']),
]
ORDER = ['CPU', 'Memory', 'Guest', 'Disk', 'Virtual Disk', 'Network', 'Super Metrics']

PRIORITY = {   # 분류 안 표시 순서: 정규식, 위에서부터
    'CPU': [r'^cpu\|usage \(%\)', r'resource-utilization', r'co-?stop \(%\)', r'^cpu\|ready', r'peak.*co-?stop', r'peak.*ready',
            r'^cpu\|usage \(ghz\)', r'dtlb', r'itlb', r'cache[ _]*miss[ _]*rate|llc.*miss', r'l1d', r'ipc', r'^instructions',
            r'cache[ _]*miss', r'cache[ _]*ref'],
    'Memory': [r'^memory\|usage', r'mem_used|memory\|.*(used|consumed)', r'thp[ _]?coverage', r'anonhugepages', r'anonpages',
               r'thp.*fault.*alloc', r'thp.*colla?pa?se?.*alloc', r'thp.*(fallback|fail)', r'swap'],
    'Guest': [r'context switch', r'used memory', r'free memory', r'swap', r'page in', r'page out', r'peak.*page'],
    'Disk': [r'read latency', r'write latency', r'read iops', r'write iops', r'read throughput', r'write throughput'],
    'Virtual Disk': [r'read latency', r'write latency', r'read iops', r'write iops', r'read throughput', r'write throughput'],
    'Network': [r'transmit rate', r'receive rate', r'packets'],
}

# 남은 자원을 재는 지표: 요약 표에 최소를 적는다 (메모리 압박이 어디까지 갔는지는 최솟값에서 읽힌다)
REMAINING = [r'free memory', r'remaining swap', r'mem_?free', r'available']


def category(name):
    low = name.lower()
    for pat, cat in OVERRIDES:
        if re.search(pat, low):
            return cat
    if '|' in name:
        head = name.split('|', 1)[0].strip()
        return head.split(':', 1)[0].strip() or '기타'
    for cat, words in KEYWORDS:
        if any(w in low for w in words):
            return cat
    return '기타'


def rank(name, cat=None):
    cat = cat or category(name)
    low = name.lower()
    for i, pat in enumerate(PRIORITY.get(cat, [])):
        if re.search(pat, low):
            return i
    return len(PRIORITY.get(cat, []))


def is_remaining(name):
    return any(re.search(p, name.lower()) for p in REMAINING)


def ordered(names):
    """[(분류, [지표…])]: 분류는 ORDER 순서 다음 이름순, '기타' 는 마지막. 분류 안은 PRIORITY 순서 다음 이름순."""
    groups = {}
    for n in names:
        groups.setdefault(category(n), []).append(n)
    key = lambda c: (c == '기타', ORDER.index(c) if c in ORDER else len(ORDER), c)
    return [(c, sorted(groups[c], key=lambda n: (rank(n, c), n.lower()))) for c in sorted(groups, key=key)]


def flat(names):
    return [n for _, ns in ordered(names) for n in ns]
