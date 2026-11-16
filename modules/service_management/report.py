# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Service delivery report rendering (without requirement coverage appendix)."""



import io
import json
from html import escape
from modules.core.brand import PRODUCT_FULL_NAME
from modules.core.data_security import redact
from .labels import FIELD_LABELS


def sanitize_document(document, *, preserve_network=False):
    from docx.oxml.ns import qn
    for part in document.part.package.parts:
        element = getattr(part,'_element',None)
        if element is None:
            continue
        for paragraph in element.iter(qn('w:p')):
            nodes = list(paragraph.iter(qn('w:t')))
            original = ''.join(node.text or '' for node in nodes)
            clean = redact(original, mask_identity=True, preserve_network=preserve_network)
            if clean != original and nodes:
                nodes[0].text = clean
                for node in nodes[1:]:
                    node.text = ''
    for table in document.tables:
        for row in table.rows:
            for index,cell in enumerate(row.cells[:-1]):
                label = cell.text.strip().lower()
                if label in {'password','pwd','口令','密码','ssh密码','私钥','api key','token'}:
                    row.cells[index+1].text = '[REDACTED]'
    return document


def _table(document,headers,widths=None):
    from docx.shared import Cm
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    table=document.add_table(rows=1,cols=len(headers))
    table.style='Table Grid'
    table.autofit=False
    if widths:
        for column,width in zip(table.columns,widths):column.width=Cm(width)
        for cell,width in zip(table.rows[0].cells,widths):cell.width=Cm(width)
    repeat=OxmlElement('w:tblHeader');table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for cell,text in zip(table.rows[0].cells,headers):
        cell.text=text
        shade=OxmlElement('w:shd');shade.set(qn('w:fill'),'E7EEF6');cell._tc.get_or_add_tcPr().append(shade)
        for run in cell.paragraphs[0].runs:run.bold=True
    return table


def _display(value,key=None):
    if isinstance(value,dict):return '\n'.join(FIELD_LABELS.get(k,k)+'：'+_display(v,k) for k,v in value.items())
    if isinstance(value,list):return '\n'.join(_display(v) for v in value) or '未采集'
    if value in (None,''):return '未采集'
    values={'active':'在用','planned':'计划中','maintenance':'维护中','offline':'已下线','draft':'草稿','submitted':'待审批','approved':'已批准','executing':'执行中','verified':'待闭环复核','closed':'已闭环','oracle':'Oracle','sqlserver':'SQL Server','dm':'DM8','gbase8a':'GBase 8a'}
    return values.get(str(value),str(value)) if key in {'state','status','lifecycle','db_type'} else str(value)


def render_docx(catalog, overview, title):
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(1.8)
    normal = document.styles['Normal']
    normal.font.name = 'Microsoft YaHei'
    normal.font.size = Pt(10)
    normal.element.rPr.rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
    document.add_heading(title,level=0)
    document.add_paragraph(PRODUCT_FULL_NAME)
    document.add_paragraph('报告周期 '+overview['period_start']+' 至 '+overview['period_end'])
    document.add_paragraph('资产 '+str(overview['asset_count'])+'，应检项 '+str(overview['required_checks']))
    document.add_paragraph('各项数量 '+json.dumps(overview['counts'],ensure_ascii=False))
    document.add_paragraph('健康评分 '+str(overview['health_score'] if overview['health_score'] is not None else '待完成有效覆盖及数据安全门槛验证'))
    document.add_paragraph('评分口径 '+overview['health_score_policy'])
    document.add_paragraph('风险发现率 '+overview['risk_discovery_reason'])
    document.add_paragraph('服务响应时限 '+json.dumps(overview.get('response_sla',[]),ensure_ascii=False))
    document.add_paragraph('故障时限 '+json.dumps(overview.get('incident_sla',[]),ensure_ascii=False))
    safe = redact(overview,mask_identity=True)
    document.add_heading('资产台账与参数明细',level=1)
    for asset in safe.get('asset_register',[]):
        document.add_heading(str(asset.get('asset_name','资产')),level=2)
        table = _table(document,['配置项','登记值'],[4.0,13.4])
        for key,value in asset.items():
            cells = table.add_row().cells
            cells[0].text = FIELD_LABELS.get(key,key)
            cells[1].text = _display(value)
    document.add_heading('台账变更追溯',level=1)
    document.add_paragraph(_display(safe.get('asset_change_history',{})))
    document.add_heading('服务执行与闭环记录',level=1)
    for record in safe.get('service_evidence',[]):
        document.add_heading(record['item_id']+' '+record['id'],level=2)
        document.add_paragraph(_display(record))
    sanitize_document(document)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def render_html(catalog, overview, title):
    safe = redact(overview,mask_identity=True)
    blocks = ['<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>'+escape(title)+'</title></head><body>',
              '<h1>'+escape(title)+'</h1><p>'+escape(PRODUCT_FULL_NAME)+'</p>',
              '<p>报告周期 '+escape(safe['period_start'])+' 至 '+escape(safe['period_end'])+'</p>']
    blocks.append('<p>健康评分 '+escape(str(safe.get('health_score') if safe.get('health_score') is not None else '待完成有效覆盖及数据安全门槛验证'))+'；'+escape(safe['health_score_policy'])+'</p>')
    blocks.append('<p>风险发现率 '+escape(safe['risk_discovery_reason'])+'</p>')
    for label,key in [('服务响应时限','response_sla'),('故障时限','incident_sla'),('台账变更追溯','asset_change_history')]:
        blocks.append('<h2>'+label+'</h2><pre>'+escape(_display(safe.get(key)))+'</pre>')
    blocks.append('<h2>资产台账与参数明细</h2><pre>'+escape(json.dumps(safe.get('asset_register',[]),ensure_ascii=False,indent=2))+'</pre>')
    blocks.append('<h2>服务执行与闭环记录</h2><pre>'+escape(json.dumps(safe.get('service_evidence',[]),ensure_ascii=False,indent=2))+'</pre>')
    blocks.append('</body></html>')
    return ''.join(blocks)
