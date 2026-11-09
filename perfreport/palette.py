"""색상·선 모양 규칙: 차트(docx)와 HTML 이 같은 규칙을 쓴다.

규칙
  - 색 = TC. 고정 순서 8색. 순서를 바꾸거나 9번째 색을 만들지 않는다.
    한 차트에 동시에 그리는 TC 는 3개까지 권장(색각 이상에서도 전 쌍 구별 가능),
    최대 8개.
  - 선 모양 + 마커 = VM 역할(role). 같은 역할은 모든 TC 에서 같은 모양이다.
  - 색은 TC 자체에 묶인다. 필터로 TC 수가 바뀌어도 남은 TC 의 색은 바뀌지 않는다.
"""

# 출처: dataviz 기본 팔레트(밝은 화면 / 어두운 화면). 인접 쌍 CVD ΔE ≥ 8 검증본.
TC_COLORS_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100',
                   '#e87ba4', '#008300', '#4a3aa7', '#e34948']
TC_COLORS_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500',
                  '#d55181', '#008300', '#9085e9', '#e66767']
MAX_TC = len(TC_COLORS_LIGHT)
RECOMMENDED_TC = 3

# VM 역할 → (matplotlib linestyle, matplotlib marker, plotly dash, plotly symbol)
ROLE_STYLES = [
    ('-', 'o', 'solid', 'circle'),
    ((0, (6, 3)), 's', 'dash', 'square'),
    ((0, (1.5, 2)), 'D', 'dot', 'diamond'),
    ((0, (6, 2, 1.5, 2)), '^', 'dashdot', 'triangle-up'),
    ((0, (10, 3)), 'v', 'longdash', 'triangle-down'),
    ((0, (10, 2, 1.5, 2)), 'x', 'longdashdot', 'x'),
]

SURFACE_LIGHT = '#fcfcfb'
SURFACE_DARK = '#1a1a19'
TEXT_PRIMARY = '#0b0b0b'
TEXT_SECONDARY = '#52514e'
GRID = '#e4e3df'


def tc_color(index, dark=False):
    pal = TC_COLORS_DARK if dark else TC_COLORS_LIGHT
    if index >= len(pal):
        raise ValueError(f'케이스는 한 차트에 최대 {MAX_TC}개까지 그린다 (요청 위치 {index + 1})')
    return pal[index]


def role_style(index):
    return ROLE_STYLES[index % len(ROLE_STYLES)]


# 케이스 · 문서 그래프 색상 팔레트 (사용자 지시 2026-11-09). HTML 과 케이스 문서가 같은 목록을 쓴다.
#  - HTML: 케이스마다 팔레트 하나(체크한 순서대로 자동 배정, 사용자가 바꿀 수 있음). VM 은 팔레트 색을 VM 순서대로.
#  - 케이스 문서: 리포트 생성기에서 고른 팔레트 하나(기본 1번 파랑 계열). VM 은 팔레트 색을 VM 순서대로.
#  - 팔레트 안의 색은 이웃한 VM 끼리 구별되도록 진하기를 번갈아 놓았다.
_BLUE = ['#1f5fbf', '#36a3d9', '#0d3a80', '#6f9ef0', '#1784a8', '#4552c9', '#7cc3e6', '#2b4c8c']
_ORANGE = ['#e4572e', '#f2a01c', '#a8261a', '#f08060', '#c25e00', '#d9293f', '#f5b860', '#7a2e10']
PALETTES = [
    ('파랑 계열', _BLUE),
    ('주황·빨강 계열', _ORANGE),
    # 1번과 2번 팔레트의 색을 번갈아 놓은 팔레트: 한 케이스 안에서 이웃한 VM 이 파랑과 주황으로 갈린다.
    # 1·2번과 같은 색을 쓰므로 HTML 의 자동 배정에서는 뺀다(사용자가 고를 때만 쓴다).
    ('파랑·주황 교차', [c for pair in zip(_BLUE[:4], _ORANGE[:4]) for c in pair]),
    ('초록 계열', ['#2e9e44', '#7bb33e', '#16602a', '#55c46d', '#56852b', '#33b58b', '#0f6b4a', '#9ccc65']),
    ('보라·분홍 계열', ['#7b3fbf', '#d6409f', '#4a1f8a', '#b07ae6', '#a3196f', '#8f5bd6', '#ef72b6', '#5c2d91']),
    ('갈색·노랑 계열', ['#9c5a2c', '#c99a12', '#5a3b12', '#d4a24c', '#8a6d1d', '#b8732e', '#6b3a1e', '#a88a2e']),
    ('회색 계열', ['#4d4d4d', '#8c8c8c', '#1f1f1f', '#b0b0b0', '#666666', '#3a3a3a', '#9e9e9e', '#2b2b2b']),
]
NO_AUTO = {2}            # HTML 에서 자동 배정하지 않는 팔레트 (0부터 센 번호)
DEFAULT_PALETTE = 0      # 케이스 문서의 기본 팔레트: 1번 파랑 계열


def palette_index(v):
    """팔레트 지정값(1부터 센 번호 또는 이름) → 0부터 센 번호. 비우면 기본 팔레트."""
    if v is None or str(v).strip() == '':
        return DEFAULT_PALETTE
    s = str(v).strip()
    if s.isdigit() and 1 <= int(s) <= len(PALETTES):
        return int(s) - 1
    for i, (name, _) in enumerate(PALETTES):
        if s.replace(' ', '') == name.replace(' ', ''):
            return i
    raise SystemExit(f'그래프 색 계열을 알 수 없음: {v} (쓸 수 있는 값: '
                     + ', '.join(f'{i + 1} {n}' for i, (n, _) in enumerate(PALETTES)) + ')')


def palette_colors(i):
    return PALETTES[i][1]
