#!/usr/bin/env python3
"""生成软件著作权登记用的两份鉴别材料（.docx）。

用法：python docs/software-copyright/gen_copyright_docs.py

产物：
  output/源代码文档-V1.0.docx   源程序前 30 页 + 后 30 页，每页 50 行
  output/软件说明书-V1.0.docx   02-软件说明书.md 转成的正式文档（含封面）

只依赖 python-docx：pip install python-docx
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.enum.text import (
        WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT,
    )
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
except ImportError:  # pragma: no cover - 环境缺少依赖时的友好提示
    sys.exit("缺少依赖 python-docx，请先执行: pip install python-docx")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                 # docs/software-copyright -> 仓库根目录
OUTPUT = HERE / "output"
MANUAL_MD = HERE / "02-软件说明书.md"

SOFTWARE_NAME = "Facet 模型路由网关软件"
VERSION = "V1.0"
COPYRIGHT_HOLDER = "【著作权人】"

SOURCE_DIRS = ("src/planrouter", "src/facet")
SOURCE_EXCLUDE = {"src/facet/_version.py"}

# 页面与排版参数（与 03-源代码文档规范.md 保持一致）
PAGE_W, PAGE_H = Cm(21), Cm(29.7)
MARGIN_TOP = MARGIN_BOTTOM = Cm(2.0)
MARGIN_LEFT = Cm(2.0)
MARGIN_RIGHT = Cm(1.8)
HEADER_DIST = Cm(1.0)
TEXT_WIDTH_CM = 17.2

LINES_PER_PAGE = 50
HALF_PAGES = 30
CODE_FONT_SIZE = 9
CODE_LINE_SPACING = 11

HEADER_SOURCE = f"{SOFTWARE_NAME} {VERSION} 源程序"
HEADER_MANUAL = f"{SOFTWARE_NAME} {VERSION} 软件说明书"


# --------------------------------------------------------------------------- #
# 通用排版工具
# --------------------------------------------------------------------------- #
def _font(run, size: float, *, ascii_font: str = "宋体", east_font: str = "宋体",
          bold: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = ascii_font
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), east_font)


def _setup_page(section) -> None:
    section.page_width = PAGE_W
    section.page_height = PAGE_H
    section.top_margin = MARGIN_TOP
    section.bottom_margin = MARGIN_BOTTOM
    section.left_margin = MARGIN_LEFT
    section.right_margin = MARGIN_RIGHT
    section.header_distance = HEADER_DIST
    section.footer_distance = Cm(1.0)


def _add_field(paragraph, instruction: str, size: float) -> None:
    """插入 Word 域（这里是 PAGE），打开/打印时自动计算页码。"""
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), f" {instruction} ")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rfonts = OxmlElement("w:rFonts")
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), "宋体")
    rpr.append(rfonts)
    half_points = OxmlElement("w:sz")
    half_points.set(qn("w:val"), str(int(size * 2)))
    rpr.append(half_points)
    run.append(rpr)
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    fld.append(run)
    paragraph._p.append(fld)


def _build_header(section, left_text: str, size: float = 9) -> None:
    header = section.header
    paragraph = header.paragraphs[0]
    paragraph.text = ""
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.tab_stops.add_tab_stop(Cm(TEXT_WIDTH_CM), WD_TAB_ALIGNMENT.RIGHT)
    run = paragraph.add_run(left_text)
    _font(run, size)
    paragraph.add_run("\t")
    run = paragraph.add_run("第 ")
    _font(run, size)
    _add_field(paragraph, "PAGE", size)
    run = paragraph.add_run(" 页")
    _font(run, size)


def _break_before(paragraph) -> None:
    """下一页从这里开始（比在段尾插分页符更干净，不会多出空行）。"""
    paragraph.paragraph_format.page_break_before = True


# --------------------------------------------------------------------------- #
# 源程序鉴别材料
# --------------------------------------------------------------------------- #
def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in SOURCE_DIRS:
        base = ROOT / directory
        for path in sorted(base.glob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SOURCE_EXCLUDE:
                continue
            files.append(path)
    return files


def collect_source_lines() -> list[str]:
    lines: list[str] = []
    for path in _source_files():
        rel = path.relative_to(ROOT).as_posix()
        lines.append(f"# ── 文件：{rel} ──")
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue                      # 空行不占页
            lines.append(raw.rstrip())
    return lines


def split_pages(lines: list[str]) -> list[list[str]]:
    capacity = LINES_PER_PAGE * HALF_PAGES          # 前/后各 30 页的行数
    if len(lines) <= capacity * 2:
        selected = lines
    else:
        note = "# ……（源程序中段已省略，以下为末尾部分）……"
        selected = lines[:capacity] + [note] + lines[-(capacity - 1):]
    return [selected[i:i + LINES_PER_PAGE]
            for i in range(0, len(selected), LINES_PER_PAGE)]


def build_source_docx(out_path: Path) -> tuple[int, int]:
    doc = Document()
    section = doc.sections[0]
    _setup_page(section)
    _build_header(section, HEADER_SOURCE)

    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(CODE_FONT_SIZE)

    lines = collect_source_lines()
    pages = split_pages(lines)
    longest = max((sum(2 if ord(ch) > 127 else 1 for ch in line)
                   for line in lines), default=0)

    for index, page in enumerate(pages):
        for lineno, text in enumerate(page):
            paragraph = doc.add_paragraph()
            fmt = paragraph.paragraph_format
            fmt.space_before = Pt(0)
            fmt.space_after = Pt(0)
            fmt.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            fmt.line_spacing = Pt(CODE_LINE_SPACING)
            fmt.keep_together = True
            if lineno == 0 and index:
                _break_before(paragraph)
            run = paragraph.add_run(text)
            _font(run, CODE_FONT_SIZE, ascii_font="Courier New", east_font="宋体")

    doc.core_properties.title = f"{SOFTWARE_NAME} {VERSION} 源程序"
    doc.core_properties.subject = "软件著作权登记 源程序鉴别材料"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return len(pages), longest


# --------------------------------------------------------------------------- #
# 文档鉴别材料（说明书）
# --------------------------------------------------------------------------- #
INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _add_inline(paragraph, text: str, size: float, *,
                bold: bool = False, color: RGBColor | None = None) -> None:
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            _font(run, size, bold=True, color=color)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            _font(run, size, ascii_font="Courier New", east_font="宋体",
                  bold=bold, color=color)
        else:
            run = paragraph.add_run(piece)
            _font(run, size, bold=bold, color=color)


def _cover(doc) -> None:
    for _ in range(5):
        doc.add_paragraph()
    for text, size, bold in ((SOFTWARE_NAME, 26, True), (VERSION, 18, False)):
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        _font(run, size, bold=bold)
    doc.add_paragraph()
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("软 件 说 明 书")
    _font(run, 22, bold=True)

    for _ in range(7):
        doc.add_paragraph()
    for text in (f"著作权人：{COPYRIGHT_HOLDER}", "编制日期：2026 年 11 月"):
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        _font(run, 14)


def _heading(factory, text: str, level: int, first: bool = False) -> None:
    paragraph = factory()
    fmt = paragraph.paragraph_format
    fmt.keep_with_next = True
    if level == 1:
        fmt.space_before = Pt(0 if first else 14)
        fmt.space_after = Pt(8)
    else:
        fmt.space_before = Pt(10)
        fmt.space_after = Pt(4)
    run = paragraph.add_run(text)
    _font(run, 16 if level == 1 else 13, bold=True)


def build_manual_docx(md_path: Path, out_path: Path) -> int:
    if not md_path.is_file():
        sys.exit(f"找不到说明书底稿：{md_path}")

    doc = Document()
    section = doc.sections[0]
    _setup_page(section)
    section.different_first_page_header_footer = True   # 封面不显示页眉
    _build_header(section, HEADER_MANUAL)

    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(12)

    _cover(doc)

    # 正文第一个段落另起一页（封面与正文不共享页面，也不会多出空行）
    pending = [True]

    def new_paragraph(style: str | None = None):
        paragraph = doc.add_paragraph(style=style) if style else doc.add_paragraph()
        if pending[0]:
            _break_before(paragraph)
            pending[0] = False
        return paragraph

    in_code = False
    section_no = 0
    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            paragraph = new_paragraph()
            fmt = paragraph.paragraph_format
            fmt.space_before = fmt.space_after = Pt(0)
            fmt.left_indent = Cm(0.6)
            fmt.line_spacing_rule = WD_LINE_SPACING.SINGLE
            run = paragraph.add_run(line)
            _font(run, 9, ascii_font="Courier New", east_font="宋体")
            shading = OxmlElement("w:shd")
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:color"), "auto")
            shading.set(qn("w:fill"), "F4F4F4")
            paragraph._p.get_or_add_pPr().append(shading)
            continue
        if not line.strip():
            continue
        if line.startswith("# "):
            continue                                  # 标题已放在封面
        if line.startswith("## "):
            section_no += 1
            _heading(new_paragraph, line[3:].strip(), 1, first=section_no == 1)
            continue
        if line.startswith("### "):
            _heading(new_paragraph, line[4:].strip(), 2)
            continue
        if line.startswith("〔截图"):
            paragraph = new_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            fmt = paragraph.paragraph_format
            fmt.space_before = fmt.space_after = Pt(4)
            run = paragraph.add_run(line.strip())
            _font(run, 10.5, color=RGBColor(0x80, 0x80, 0x80))
            run.font.italic = True
            continue
        if line.startswith("- ") or line.startswith("* "):
            paragraph = new_paragraph("List Bullet")
            fmt = paragraph.paragraph_format
            fmt.space_before = Pt(0)
            fmt.space_after = Pt(3)
            fmt.line_spacing = 1.4
            _add_inline(paragraph, line[2:].strip(), 12)
            continue
        numbered = re.match(r"^(\d+)\.\s+(.*)$", line)
        if numbered:
            paragraph = new_paragraph()
            fmt = paragraph.paragraph_format
            fmt.left_indent = Cm(0.85)
            fmt.first_line_indent = Cm(-0.85)
            fmt.space_before = Pt(0)
            fmt.space_after = Pt(3)
            fmt.line_spacing = 1.4
            _add_inline(paragraph, f"{numbered.group(1)}. {numbered.group(2)}", 12)
            continue

        paragraph = new_paragraph()
        fmt = paragraph.paragraph_format
        fmt.first_line_indent = Cm(0.85)              # 首行缩进 2 字符
        fmt.space_before = Pt(0)
        fmt.space_after = Pt(6)
        fmt.line_spacing = 1.5
        _add_inline(paragraph, line.strip(), 12)

    doc.core_properties.title = f"{SOFTWARE_NAME} {VERSION} 软件说明书"
    doc.core_properties.subject = "软件著作权登记 文档鉴别材料"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return len(doc.paragraphs)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source_out = OUTPUT / f"源代码文档-{VERSION}.docx"
    manual_out = OUTPUT / f"软件说明书-{VERSION}.docx"

    pages, longest = build_source_docx(source_out)
    total = pages * LINES_PER_PAGE
    print(f"[1/2] {source_out.name}: {pages} 页, 每页 {LINES_PER_PAGE} 行, "
          f"约 {total} 行, 最长行宽 {longest} 字符")
    if pages < HALF_PAGES * 2:
        print(f"      提示：源程序不足 60 页，按登记口径全部提交")

    paragraphs = build_manual_docx(MANUAL_MD, manual_out)
    print(f"[2/2] {manual_out.name}: {paragraphs} 个段落（页数由 Word 渲染确定）")
    print("完成，请按 03-源代码文档规范.md 的清单人工复核页眉、页码与占位内容。")


if __name__ == "__main__":
    main()
