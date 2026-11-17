# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Typed XLSX/DOCX rendering, invisible OOXML properties and signed packages."""

import base64
import io
import json
import re
import uuid
import zipfile
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

from .delivery_catalog import DELIVERABLES
from .delivery_store import digest
from .store import canonical

PROPERTY_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/custom-properties'
VALUE_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
TYPE_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'


def text(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def stamped(content, properties):
    """Add custom properties before computing the final file digest."""
    props = ET.Element('Properties', xmlns=PROPERTY_NS)
    for index, (name, value) in enumerate(properties.items(), 2):
        prop = ET.SubElement(props, 'property', fmtid='{D5CDD505-2E9C-101B-9397-08002B2CF9AE}',
                             pid=str(index), name='DBPulse.' + name)
        ET.SubElement(prop, '{' + VALUE_NS + '}lpwstr').text = str(value)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        rels = ET.fromstring(source.read('_rels/.rels'))
        for node in list(rels):
            if node.get('Type', '').endswith('/custom-properties'):
                rels.remove(node)
        existing = {node.get('Id') for node in rels}
        relation_id = 'rIdDBPulseCustom'
        while relation_id in existing:
            relation_id += '1'
        ET.SubElement(rels, '{' + REL_NS + '}Relationship', Id=relation_id,
                      Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties',
                      Target='docProps/custom.xml')
        types = ET.fromstring(source.read('[Content_Types].xml'))
        if not any(n.get('PartName') == '/docProps/custom.xml' for n in types):
            ET.SubElement(types, '{' + TYPE_NS + '}Override', PartName='/docProps/custom.xml',
                          ContentType='application/vnd.openxmlformats-officedocument.custom-properties+xml')
        for name in source.namelist():
            if name not in {'docProps/custom.xml', '_rels/.rels', '[Content_Types].xml'}:
                target.writestr(name, source.read(name))
        target.writestr('docProps/custom.xml', ET.tostring(props, encoding='utf-8', xml_declaration=True))
        target.writestr('_rels/.rels', ET.tostring(rels, encoding='utf-8', xml_declaration=True))
        target.writestr('[Content_Types].xml', ET.tostring(types, encoding='utf-8', xml_declaration=True))
    return output.getvalue()


def watermark(content):
    try:
        with bounded_zip(content) as archive:
            if 'docProps/custom.xml' not in archive.namelist():
                return {}
            root = ET.fromstring(archive.read('docProps/custom.xml'))
            return {p.get('name', '')[8:]: next(iter(p)).text for p in root
                    if p.get('name', '').startswith('DBPulse.') and len(p)}
    except (ValueError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return {}


def bounded_zip(content):
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ValueError('文件不是有效的ZIP或Office文档') from error
    entries = archive.infolist()
    if len(entries) > 20000 or len({e.filename for e in entries}) != len(entries) or \
            sum(e.file_size for e in entries) > 128 * 1024 * 1024 or any(e.file_size > 32 * 1024 * 1024 for e in entries):
        archive.close()
        raise ValueError('压缩文件展开大小、成员数量或成员名称异常')
    return archive


def xlsx(title, sections, description):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    workbook = Workbook()
    workbook.remove(workbook.active)
    overflow = []
    all_sections = [('报告说明', [{'项目': k, '内容': v} for k, v in description.items()]), *sections.items()]
    used = set()
    for name, rows in all_sections:
        # Split before Excel's row limit, including the title and header rows.
        chunks = [rows[i:i + 1_048_572] for i in range(0, len(rows), 1_048_572)] or [[]]
        for part, chunk in enumerate(chunks, 1):
            page_name = re.sub(r'[\\/*?:\[\]]', '_', name)[:26]
            suffix = '' if len(chunks) == 1 else '_' + str(part)
            while page_name + suffix in used:
                suffix += '_'
            page_name = (page_name + suffix)[:31]
            used.add(page_name)
            page = workbook.create_sheet(page_name)
            headers = list(dict.fromkeys(k for row in chunk for k in row)) or ['状态', '说明']
            page.append([title])
            page.append(headers)
            if not chunk:
                chunk = [{'状态': '无可用记录', '说明': '本范围无相关事件或证据，请核对报告说明中的采集覆盖'}]
            for row in chunk:
                page.append([row.get(k) if isinstance(row.get(k), (int, float, bool)) else text(row.get(k)) for k in headers])
            for row in page:
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.value = ILLEGAL_CHARACTERS_RE.sub('', cell.value)
                        if len(cell.value) > 32767:
                            value = cell.value
                            reference = page_name + '!' + cell.coordinate
                            for offset in range(0, len(value), 30000):
                                overflow.append([reference, offset // 30000 + 1, value[offset:offset + 30000]])
                            cell.value = '完整内容见长文本明细，位置 ' + reference
                        cell.data_type = 's'
                    cell.font = Font(name='Microsoft YaHei', size=10)
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
            for cell in page[2]:
                cell.fill = PatternFill('solid', fgColor='234F77')
                cell.font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF', size=10)
            page['A1'].font = Font(name='Microsoft YaHei', size=15, bold=True, color='17365D')
            page.row_dimensions[1].height = 30
            page.row_dimensions[2].height = 32
            for i in range(1, len(headers) + 1):
                page.column_dimensions[get_column_letter(i)].width = 24
            page.freeze_panes = 'A3'
            page.auto_filter.ref = f'A2:{get_column_letter(len(headers))}{page.max_row}'
            page.print_title_rows = '1:2'
            page.page_setup.orientation = 'landscape'
            page.page_setup.paperSize = page.PAPERSIZE_A3
            page.page_setup.fitToWidth = 1
            page.page_setup.fitToHeight = 0
    if overflow:
        page = workbook.create_sheet('长文本明细')
        page.append(['来源位置', '片段序号', '内容'])
        for row in overflow:
            page.append(row)
            for cell in page[page.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        page.freeze_panes = 'A2'
        page.column_dimensions['A'].width = 35
        page.column_dimensions['B'].width = 15
        page.column_dimensions['C'].width = 100
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def docx(title, sections, description):
    from docx import Document
    from docx.shared import Pt
    from .report import sanitize_document
    document = Document()
    document.styles['Normal'].font.name = 'Microsoft YaHei'
    document.styles['Normal'].font.size = Pt(10)
    document.add_heading(title, 0)
    for key, value in description.items():
        document.add_paragraph(key + ' ' + text(value))
    for name, rows in sections.items():
        document.add_heading(name, 1)
        if not rows:
            document.add_paragraph('本范围无可用记录，需结合采集覆盖核对事件和证据。')
        for index, row in enumerate(rows, 1):
            document.add_heading(str(index) + ' ' + text(row.get('标题') or row.get('资产名称') or name), 2)
            table = document.add_table(rows=0, cols=2)
            table.style = 'Table Grid'
            for key, value in row.items():
                cells = table.add_row().cells
                cells[0].text, cells[1].text = key, text(value)
    sanitize_document(document, preserve_network=True)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def render_job(job, published=False):
    snapshot, request = job['snapshot'], job['request']
    rendered = []
    combined = request.get('layout') == 'combined' and 'xlsx' in request['formats']
    units = [(kind, format_name) for kind in request['deliverables'] for format_name in request['formats']
             if not (combined and format_name == 'xlsx')]
    if combined:
        units.insert(0, ('COMBINED', 'xlsx'))
    for kind, format_name in units:
        if kind == 'COMBINED':
            title = '数据库运维综合交付报告'
            names = list(dict.fromkeys(name for key in request['deliverables'] for name in DELIVERABLES[key][2]))
        else:
            title, _, names = DELIVERABLES[kind]
        sections = {name: snapshot['sheets'].get(name, []) for name in names
                    if not request['sheets'] or name in request['sheets']}
        if kind == 'D17':
            sections = {name: [r for r in rows if r.get('重大故障') or name in {'故障时间线', '改进闭环'}]
                        for name, rows in sections.items()}
        content_sha256 = digest(canonical(sections).encode())
        description = {'报告编号': job['report_id'], '版本': job['revision'],
                       '状态': '已发布' if published else '草稿', '统计开始': snapshot['start'],
                       '统计结束（不含）': snapshot['end'], '数据截止': request['cutoff'],
                       '资产范围': [a['payload'].get('asset_name') for a in snapshot['assets']],
                       '覆盖情况': snapshot['coverage'], '模板版本': 'delivery-v1',
                       '历史映射与缺口': snapshot.get('history_notes'),
                       '内容SHA256': content_sha256, '来源清单SHA256': snapshot['source_manifest_sha256']}
        if combined and kind == 'COMBINED':
            description['交付物目录'] = {DELIVERABLES[key][0]: DELIVERABLES[key][2] for key in request['deliverables']}
        artifact_id = uuid.uuid4().hex
        metadata = {'artifact_id': artifact_id, 'report_id': job['report_id'], 'revision': job['revision'],
                    'deliverable': kind, 'format': format_name, 'content_sha256': content_sha256,
                    'source_manifest_sha256': snapshot['source_manifest_sha256'], 'watermark_version': '1'}
        raw = xlsx(title, sections, description) if format_name == 'xlsx' else docx(title, sections, description)
        content = stamped(raw, metadata)
        filename = f'{kind}_{title}_{request["date"]}_v{job["revision"]}.{format_name}'
        rendered.append((filename, content, metadata))
    return rendered


def package(job, archive, actor):
    """Deterministic ZIP, with no circular hash of the ZIP inside itself."""
    files = {}
    for artifact in job['artifacts']:
        info, content = archive.artifact(actor, job['id'], artifact['artifact_id'], audit=False)
        files[info['name']] = content
    files['manifest.json'] = canonical(job['manifest']).encode()
    files['manifest.sig'] = job['signature'].encode()
    files['public-key.pem'] = job['public_key'].encode()
    files['checksums.sha256'] = ''.join(a['sha256'] + '  ' + a['name'] + '\n' for a in job['artifacts']).encode()
    files['校验说明.txt'] = ('先通过报告中心或独立交付渠道确认公钥SHA-256指纹，再验证manifest.sig和文件摘要。\n'
                            '公钥指纹 ' + job['manifest']['key_id'] + '\n'
                            '离线命令 python scripts/verify_delivery_package.py <交付包.zip> --fingerprint <可信公钥指纹>\n').encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, content)
    return output.getvalue()


def verify_package(content, fingerprint):
    with bounded_zip(content) as source:
        if not {'manifest.json', 'manifest.sig', 'public-key.pem', 'checksums.sha256'} <= set(source.namelist()):
            raise ValueError('交付包缺少签名清单、公钥或摘要文件')
        manifest_bytes = source.read('manifest.json')
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get('schema') != 'dbpulse-report-manifest-v1' or \
                not isinstance(manifest.get('artifacts'), list) or not manifest['artifacts']:
            raise ValueError('签名清单结构无效')
        for entry in manifest['artifacts']:
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or \
                    not isinstance(entry.get('size'), int) or not isinstance(entry.get('sha256'), str):
                raise ValueError('签名清单文件条目无效')
            if entry['name'] not in source.namelist():
                raise ValueError('交付包缺少已签发文件')
        public_bytes = source.read('public-key.pem')
        if digest(public_bytes) != fingerprint or manifest.get('key_id') != fingerprint:
            raise ValueError('公钥指纹与可信基准不匹配')
        if manifest.get('algorithm') != 'ECDSA-P256-SHA256':
            raise ValueError('签名算法无效')
        public = serialization.load_pem_public_key(public_bytes)
        if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
            raise ValueError('签名公钥类型无效')
        try:
            public.verify(base64.b64decode(source.read('manifest.sig'), validate=True),
                          manifest_bytes, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as error:
            raise ValueError('校验清单签名验证失败') from error
        for entry in manifest['artifacts']:
            value = source.read(entry['name'])
            if len(value) != entry['size'] or digest(value) != entry['sha256']:
                raise ValueError('文件与签名清单不一致 ' + entry['name'])
        checksums = ''.join(entry['sha256'] + '  ' + entry['name'] + '\n' for entry in manifest['artifacts']).encode()
        if source.read('checksums.sha256') != checksums:
            raise ValueError('摘要文件与签名清单不一致')
        extras = set(source.namelist()) - {a['name'] for a in manifest['artifacts']} - {
            'manifest.json', 'manifest.sig', 'public-key.pem', 'checksums.sha256', '校验说明.txt'}
        if extras:
            raise ValueError('交付包存在未签发的附加文件')
        return {'signature_valid': True, 'files_valid': True, 'key_id': fingerprint,
                'report_id': manifest['report_id'], 'revision': manifest['revision'], 'state': manifest['state']}


def content_diff(original, changed, format_name):
    """Bounded value/formula or paragraph differences, with explicit truncation."""
    def cells(content):
        with bounded_zip(content):
            pass
        if format_name == 'xlsx':
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            result = {}
            try:
                for sheet in workbook:
                    for row in sheet:
                        for cell in row:
                            if cell.value is not None:
                                if len(result) >= 100000:
                                    raise ValueError('差异比对超出10万单元格预算，请下载原件核对')
                                result[sheet.title + '!' + cell.coordinate] = cell.value
            finally:
                workbook.close()
            return result
        from docx import Document
        document = Document(io.BytesIO(content))
        return {str(i): p.text for i, p in enumerate(document.element.body.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))}
    left, right = cells(original), cells(changed)
    keys = sorted(set(left) | set(right))
    differences = [{'位置': k, '原值': left.get(k), '当前值': right.get(k)} for k in keys if left.get(k) != right.get(k)]
    return {'differences': differences[:100], 'total': len(differences), 'truncated': len(differences) > 100}
