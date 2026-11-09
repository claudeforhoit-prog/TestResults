"""docx 리포트. 문장은 명사형 종결, 그림·표 제목은 번호 없이 '그림. 대상' · '표. 대상'."""
import os

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from .analyze import fmt
from .docx_base import new_document

FONT = 'Malgun Gothic'


def _style(doc):
    st = doc.styles['Normal']
    st.font.name = FONT
    st.font.size = Pt(10)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), FONT)
    for name in ('Heading 1', 'Heading 2', 'Title'):
        s = doc.styles[name]
        s.font.name = FONT
        s.element.rPr.rFonts.set(qn('w:eastAsia'), FONT)


def _caption(doc, text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(9.5)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return p


def _table(doc, header, rows, header2=None):
    """표. header2 를 주면 머리글을 두 줄로 만든다 (병합 칸은 쓰지 않는다: 컨플루언스로 가져올 때 깨질 수 있어서)."""
    t = doc.add_table(rows=1, cols=len(header))
    t.style = 'Table Grid'

    def put(cell, text, bold=False):
        r = cell.paragraphs[0].add_run(str(text))
        r.font.size = Pt(8.5)
        if bold:
            r.bold = True

    for c, h in zip(t.rows[0].cells, header):
        put(c, h, bold=True)
    if header2:
        for c, h in zip(t.add_row().cells, header2):
            put(c, h, bold=True)
    for row in rows:
        for c, v in zip(t.add_row().cells, row):
            put(c, v)
    return t


def build(path, ctx):
    """ctx: title, baseline, cases[{id,label,host,start,end,entities}], metrics[{name, unit, charts, sentences,
    stat_rows, source_system}], sources[{case, system, file, n}], note"""
    doc = new_document()
    _style(doc)
    doc.add_heading(ctx['title'], level=0)
    doc.add_paragraph(f'작성일: {ctx["date"]} · 기준 케이스: {ctx["baseline"]}')

    doc.add_heading('1. 시험 개요', level=1)
    _caption(doc, '표. 대상 케이스')
    _table(doc, ['케이스', '설명', '호스트', '실행 구간 (시작~종료 시각)', '경과 (s)', '대상 VM'],
           [[c['id'], c.get('label', ''), c.get('host') or '-', f'{c["start"]:%Y-%m-%d %H:%M:%S} ~ {c["end"]:%Y-%m-%d %H:%M:%S}',
             f'{(c["end"] - c["start"]).total_seconds():,.0f}', c['entities']] for c in ctx['cases']])
    doc.add_paragraph('그래프의 가로축은 각 케이스의 잡 시작 시각을 0초로 둔 경과 시간. '
                      '그래프는 케이스마다 패널 하나이고, 패널 안의 선은 그 케이스 호스트의 VM. 선 색은 VM 을 구별하고, VM 이 8대를 넘으면 색과 선 모양을 조합. 케이스가 4개를 넘으면 여러 장으로 나눠 수록.'
                      + (f' 그래프의 선은 {ctx["bin_s"]}초 구간 평균값. 표의 통계는 구간 평균 전의 원 데이터 기준.' if ctx.get('bin_s') else ''))

    doc.add_heading('2. 요약', level=1)
    for m in ctx['metrics']:
        if m['sentences']:
            doc.add_paragraph(f'{m["name"]}: {m["sentences"][0]}', style='List Bullet')

    doc.add_heading('3. 지표별 결과', level=1)
    for m in ctx['metrics']:
        doc.add_heading(m['name'], level=2)
        doc.add_paragraph(f'데이터: {m["source_system"]}')
        for png, part in m['charts']:
            who = f', {part[0]} ~ {part[-1]}' if len(m['charts']) > 1 else ''
            _caption(doc, f'그림. {m["name"]} 케이스별 VM 추이{who}')
            doc.add_picture(png, width=Inches(6.3))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for s in m['sentences']:
            doc.add_paragraph(s, style='List Bullet')
        _caption(doc, f'표. {m["name"]} 통계 ({m["unit"] or "단위 없음"})')
        _table(doc, ['VM', '케이스', '호스트', '표본 수', '평균', 'p95', '최댓값', f'기준 대비 평균 차이'],
               [[r['entity'] + (f' ({r["role"]})' if not r['entity'].endswith(r['role']) else ''), r['case'], r['host'], int(r['n']), fmt(r['mean']), fmt(r['p95']),
                 fmt(r['max']), _diff(r, m['unit'])] for r in m['stat_rows']])

    doc.add_heading('Appendix. 데이터 출처', level=1)
    _table(doc, ['케이스', '수집 시스템', '파일', '읽은 값 개수'],
           [[s['case'], s['system'], s['file'], f'{s["n"]:,}'] for s in ctx['sources']])
    doc.add_paragraph(ctx['note'])
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    doc.save(path)
    return path


def _diff(r, unit):
    if r['case'] == r.get('baseline_case'):
        return '기준'
    if r['diff'] is None or r['diff'] != r['diff']:
        return '기준 케이스에 없는 VM'
    sign = '+' if r['diff'] >= 0 else '-'
    if unit == '%':
        return f'{sign}{fmt(abs(r["diff"]))}%p'
    if r['diff_pct'] is None or r['diff_pct'] != r['diff_pct']:
        return f'{sign}{fmt(abs(r["diff"]))}'
    return f'{r["diff_pct"]:+.1f}%'
