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
