# -*- coding: utf-8 -*-
"""标准库 OOXML(XLSX) 导出：zipfile + 手写 XML，不依赖 openpyxl。

只支持单个工作表、inlineStr 文本与数值单元格；足够导出扫描结果。
大结果集使用流式写临时文件再打包，避免一次性占用大量内存。
"""

from __future__ import absolute_import

import os
import tempfile
import zipfile
from xml.sax.saxutils import escape, quoteattr

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>')

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>')

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '</Relationships>')


def _column_name(index):
    name = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _cell_xml(reference, value):
    if value is None:
        return '<c r="%s"/>' % reference
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (reference, value)
    text = escape(str(value))
    return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
            % (reference, text))


def _row_xml(row_number, values):
    cells = []
    for index, value in enumerate(values):
        cells.append(_cell_xml(_column_name(index) + str(row_number), value))
    return '<row r="%d">%s</row>' % (row_number, "".join(cells))


def write_xlsx(destination, header, rows, sheet_name=u"重复文件",
               column_widths=None):
    """把表头与数据行写成最小可用 XLSX。

    header: 列名列表；rows: 可迭代的列表（按列顺序）。
    """
    directory = os.path.dirname(os.path.abspath(destination)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, sheet_path = tempfile.mkstemp(prefix=".xlsx-sheet-", suffix=".xml",
                                      dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
            handle.write(('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                          '<sheetData>').encode("utf-8"))
            handle.write(_row_xml(1, header).encode("utf-8"))
            row_number = 1
            for values in rows:
                row_number += 1
                handle.write(_row_xml(row_number, values).encode("utf-8"))
            handle.write(b"</sheetData></worksheet>")
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name=%s sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>' % quoteattr(str(sheet_name)))
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("_rels/.rels", _ROOT_RELS)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
            archive.write(sheet_path, "xl/worksheets/sheet1.xml")
        return destination
    finally:
        try:
            os.unlink(sheet_path)
        except OSError:
            pass
