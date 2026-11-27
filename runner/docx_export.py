"""零外部依赖的 Markdown -> Word (.docx / OOXML) 导出器。

仅使用 Python 标准库（io / re / zipfile / xml.sax.saxutils），将汇总报告或单项目
Markdown 报告转换为结构规范、样式美观的 Office Open XML (.docx) 文档：
- 自动根据表格最大列数选择 A4 横向（>= 10 列，适配 17 列项目明细表）或 A4 纵向；
- 标题层级（H1-H4）、深蓝表头 + 跨页重复表头 + 斑马纹表格 + 单元格禁跨页截断；
- 引用块（>）渲染为蓝边浅底提示框（对应短板聚焦/读表/口径说明）；
- 支持加粗、行内代码、Markdown 链接文本提取、无序/有序列表、围栏代码块。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

import report_validate

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOC_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

STYLES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="微软雅黑" w:cs="Calibri"/>
        <w:color w:val="1F2937"/>
        <w:sz w:val="20"/>
        <w:szCs w:val="20"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:before="40" w:after="100" w:line="300" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="240" w:after="160"/>
      <w:pBdr><w:bottom w:val="single" w:sz="12" w:space="4" w:color="1F4E79"/></w:pBdr>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="1F4E79"/>
      <w:sz w:val="36"/>
      <w:szCs w:val="36"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="240" w:after="120"/>
      <w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3" w:color="BFDBFE"/></w:pBdr>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="1F4E79"/>
      <w:sz w:val="28"/>
      <w:szCs w:val="28"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="180" w:after="80"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="1E3A8A"/>
      <w:sz w:val="24"/>
      <w:szCs w:val="24"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading4">
    <w:name w:val="heading 4"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="140" w:after="60"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="334155"/>
      <w:sz w:val="21"/>
      <w:szCs w:val="21"/>
    </w:rPr>
  </w:style>
</w:styles>
"""

INLINE_TOKEN_RE = re.compile(
    r"(\*\*[^*\n]+?\*\*|`[^`\n]+?`|\[[^\]]+\]\([^)]+\))"
)


def _xml_text(text: str) -> str:
    # 剔除 XML 1.0 非法控制字符
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return escape(cleaned)


def _run_xml(text: str, *, bold: bool = False, color: str | None = None,
             size_half_pt: int | None = None, code: bool = False) -> str:
    if not text:
        return ""
    rpr_parts: list[str] = []
    if code:
        rpr_parts.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="微软雅黑"/>')
        rpr_parts.append('<w:shd w:val="clear" w:color="auto" w:fill="F3F4F6"/>')
    if bold:
        rpr_parts.append("<w:b/>")
    if color:
        rpr_parts.append(f'<w:color w:val="{color}"/>')
    if size_half_pt is not None:
        rpr_parts.append(f'<w:sz w:val="{size_half_pt}"/><w:szCs w:val="{size_half_pt}"/>')
    rpr = f"<w:rPr>{''.join(rpr_parts)}</w:rPr>" if rpr_parts else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r>'


def _inline_runs(text: str, *, base_bold: bool = False, base_color: str | None = None,
                 size_half_pt: int | None = None) -> str:
    parts: list[str] = []
    pos = 0
    for match in INLINE_TOKEN_RE.finditer(text):
        if match.start() > pos:
            parts.append(_run_xml(
                text[pos:match.start()], bold=base_bold, color=base_color, size_half_pt=size_half_pt
            ))
        tok = match.group(0)
        if tok.startswith("**") and tok.endswith("**"):
            parts.append(_run_xml(
                tok[2:-2], bold=True, color=base_color, size_half_pt=size_half_pt
            ))
        elif tok.startswith("`") and tok.endswith("`"):
            parts.append(_run_xml(
                tok[1:-1], bold=base_bold, color=base_color or "1E293B",
                size_half_pt=(size_half_pt - 2 if size_half_pt and size_half_pt > 14 else size_half_pt),
                code=True,
            ))
        elif tok.startswith("["):
            m_link = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", tok)
            label = m_link.group(1) if m_link else tok
            parts.append(_run_xml(
                label, bold=base_bold, color=base_color or "1D4ED8", size_half_pt=size_half_pt
            ))
        pos = match.end()
    if pos < len(text):
        parts.append(_run_xml(text[pos:], bold=base_bold, color=base_color, size_half_pt=size_half_pt))
    return "".join(parts) or _run_xml("", size_half_pt=size_half_pt)


def _paragraph_xml(text: str, *, style: str | None = None, indent_left: int = 0,
                   callout: bool = False, code_block: bool = False) -> str:
    ppr_parts: list[str] = []
    if style:
        ppr_parts.append(f'<w:pStyle w:val="{style}"/>')
    if indent_left > 0:
        ppr_parts.append(f'<w:ind w:left="{indent_left}"/>')
    if callout:
        ppr_parts.append('<w:shd w:val="clear" w:color="auto" w:fill="EFF6FF"/>')
        ppr_parts.append(
            '<w:pBdr><w:left w:val="single" w:sz="24" w:space="8" w:color="2563EB"/>'
            '<w:top w:val="single" w:sz="4" w:space="4" w:color="DBEAFE"/>'
            '<w:bottom w:val="single" w:sz="4" w:space="4" w:color="DBEAFE"/>'
            '<w:right w:val="single" w:sz="4" w:space="8" w:color="DBEAFE"/></w:pBdr>'
        )
        ppr_parts.append('<w:spacing w:before="80" w:after="100" w:line="280" w:lineRule="auto"/>')
    elif code_block:
        ppr_parts.append('<w:shd w:val="clear" w:color="auto" w:fill="F3F4F6"/>')
        ppr_parts.append('<w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/>')
    ppr = f"<w:pPr>{''.join(ppr_parts)}</w:pPr>" if ppr_parts else ""
    if code_block:
        runs = _run_xml(text, size_half_pt=17, code=True)
    elif style:
        # 标题样式自带字号/颜色/加粗，去掉多余 inline markdown 符号即可
        clean_title = re.sub(r"[*_`]", "", text).strip()
        runs = _run_xml(clean_title)
    else:
        runs = _inline_runs(text, size_half_pt=19 if callout else None)
    return f"<w:p>{ppr}{runs}</w:p>"


def _is_table_sep(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r"[:\- ]+", c) for c in cells if c)


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_xml(rows: list[list[str]], usable_twips: int) -> str:
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    if ncols <= 0:
        return ""
    # 按列数自适应字号（17 列项目明细表用 7.5pt，中表 8.5pt，小表 9.5pt）
    font_half_pt = 15 if ncols >= 12 else (17 if ncols >= 8 else 19)

    # 简单估算列宽比例：文字较长列获得更宽份额，数值列紧凑显示
    col_weights = [1.0] * ncols
    for r in rows[:30]:
        for i in range(min(len(r), ncols)):
            plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", r[i])
            plain = re.sub(r"[*_`]", "", plain).strip()
            eff = min(len(plain), 28)
            col_weights[i] = max(col_weights[i], max(1.0, eff * 0.45))
    total_w = sum(col_weights) or 1.0
    col_widths = [max(420, int(usable_twips * (w / total_w))) for w in col_weights]
    # 轻微修正总宽对齐
    diff = usable_twips - sum(col_widths)
    if col_widths and diff != 0:
        max_i = col_widths.index(max(col_widths))
        col_widths[max_i] = max(420, col_widths[max_i] + diff)

    grid_xml = "".join(f'<w:gridCol w:w="{w}"/>' for w in col_widths)
    tr_list: list[str] = []
    for row_idx, raw_cells in enumerate(rows):
        cells = list(raw_cells) + [""] * (ncols - len(raw_cells))
        is_header = row_idx == 0
        tr_pr = "<w:trPr><w:cantSplit/>" + ("<w:tblHeader/>" if is_header else "") + "</w:trPr>"
        fill = "1F4E79" if is_header else ("F8FAFC" if row_idx % 2 == 0 else "FFFFFF")
        tc_list: list[str] = []
        for col_idx, cell_text in enumerate(cells[:ncols]):
            w = col_widths[col_idx]
            text_color: str | None = "FFFFFF" if is_header else None
            text_bold = is_header
            plain_cell = re.sub(r"[*_`]", "", cell_text).strip()
            if not is_header:
                if plain_cell in ("高风险", "P0 致命"):
                    text_color = "DC2626"
                    text_bold = True
                elif plain_cell in ("明显风险", "存在明显风险", "P1 严重"):
                    text_color = "D97706"
                    text_bold = True
                elif plain_cell in ("良好", "优秀"):
                    text_color = "16A34A"
                    text_bold = True
            runs = _inline_runs(
                cell_text,
                base_bold=text_bold,
                base_color=text_color,
                size_half_pt=font_half_pt,
            )
            tc_list.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>'
                f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'
                f'<w:vAlign w:val="center"/></w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:before="30" w:after="30" w:line="220" w:lineRule="auto"/></w:pPr>'
                f'{runs}</w:p></w:tc>'
            )
        tr_list.append(f"<w:tr>{tr_pr}{''.join(tc_list)}</w:tr>")

    tbl_pr = (
        "<w:tblPr>"
        f'<w:tblW w:w="{usable_twips}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        "</w:tblBorders>"
        "<w:tblCellMar>"
        '<w:top w:w="55" w:type="dxa"/><w:left w:w="75" w:type="dxa"/>'
        '<w:bottom w:w="55" w:type="dxa"/><w:right w:w="75" w:type="dxa"/>'
        "</w:tblCellMar>"
        "</w:tblPr>"
    )
    return f"<w:tbl>{tbl_pr}<w:tblGrid>{grid_xml}</w:tblGrid>{''.join(tr_list)}</w:tbl>"


def markdown_to_docx_bytes(markdown_text: str, title: str = "代码检查分析报告汇总") -> bytes:
    """将 Markdown 文本转换为完整的 .docx 二进制字节流。"""
    source = report_validate.unwrap_outer_markdown_fence(markdown_text or "")
    lines = source.splitlines()

    # 先扫描最大表格列数，决定 A4 横向（适配 10+ 列汇总明细表）还是纵向
    max_cols = 0
    for line in lines:
        if line.strip().startswith("|") and not _is_table_sep(line):
            max_cols = max(max_cols, len(_split_table_row(line)))
    landscape = max_cols >= 10
    if landscape:
        page_w, page_h = 16838, 11906
        margin_x, margin_y = 720, 720
        sect_pg = f'<w:pgSz w:w="{page_w}" w:h="{page_h}" w:orient="landscape"/>'
    else:
        page_w, page_h = 11906, 16838
        margin_x, margin_y = 1080, 1080
        sect_pg = f'<w:pgSz w:w="{page_w}" w:h="{page_h}"/>'
    usable_twips = page_w - 2 * margin_x

    body_blocks: list[str] = []
    i = 0
    n = len(lines)
    in_code = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            body_blocks.append(_paragraph_xml(line, code_block=True))
            i += 1
            continue
        if not stripped:
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            body_blocks.append(
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="CBD5E1"/></w:pBdr></w:pPr></w:p>'
            )
            i += 1
            continue
        # 标题
        m_h = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if m_h:
            level = len(m_h.group(1))
            body_blocks.append(_paragraph_xml(m_h.group(2).strip(), style=f"Heading{level}"))
            i += 1
            continue
        # Markdown 表格
        if stripped.startswith("|"):
            tbl_rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                if not _is_table_sep(lines[i]):
                    tbl_rows.append(_split_table_row(lines[i]))
                i += 1
            if tbl_rows:
                body_blocks.append(_table_xml(tbl_rows, usable_twips))
                body_blocks.append('<w:p><w:pPr><w:spacing w:before="0" w:after="60"/></w:pPr></w:p>')
            continue
        # 引用块（合并连续 > 行）
        if stripped.startswith(">"):
            q_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                q_lines.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            body_blocks.append(_paragraph_xml(" ".join(q_lines), callout=True))
            continue
        # 无序列表
        m_ul = re.match(r"^[-*+]\s+(.*)", stripped)
        if m_ul:
            body_blocks.append(_paragraph_xml("• " + m_ul.group(1), indent_left=320))
            i += 1
            continue
        # 有序列表
        m_ol = re.match(r"^(\d+[.)])\s+(.*)", stripped)
        if m_ol:
            body_blocks.append(_paragraph_xml(f"{m_ol.group(1)} {m_ol.group(2)}", indent_left=320))
            i += 1
            continue
        # 普通段落
        body_blocks.append(_paragraph_xml(stripped))
        i += 1

    if not body_blocks:
        body_blocks.append(_paragraph_xml(title, style="Heading1"))

    sect_pr = (
        f"<w:sectPr>{sect_pg}"
        f'<w:pgMar w:top="{margin_y}" w:right="{margin_x}" w:bottom="{margin_y}" '
        f'w:left="{margin_x}" w:header="450" w:footer="450" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{"".join(body_blocks)}{sect_pr}</w:body></w:document>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", ROOT_RELS_XML)
        zf.writestr("word/_rels/document.xml.rels", DOC_RELS_XML)
        zf.writestr("word/styles.xml", STYLES_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()
