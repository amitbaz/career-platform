from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate

_INVALID_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_DATE_LINE = re.compile(
    rf"^(?:\d{{4}}-\d{{2}}-\d{{2}}|(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}|"
    rf"\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}})$",
    re.IGNORECASE,
)
_SUBJECT_LINE = re.compile(r"^(?:(?:subject|re)\s*:|application\s+for\b)", re.IGNORECASE)
_SALUTATION_LINE = re.compile(r"^(?:dear|hello|hi)\b", re.IGNORECASE)
_CLOSING_LINE = re.compile(
    r"^(?:best(?: regards)?|kind regards|warm regards|regards|sincerely|"
    r"yours sincerely|yours faithfully|thank you|thanks)[,!]?$",
    re.IGNORECASE,
)


def _sanitize_filename_part(value: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", value.strip()).strip("_")
    return cleaned or "Unknown"


def render_cover_letter_pdf(text: str, company: str, role: str, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{_sanitize_filename_part(company)}_{_sanitize_filename_part(role)}_Cover_Letter.pdf"
    path = out_dir / filename

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    base_style = ParagraphStyle(
        "CoverLetterBase",
        parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica",
        fontSize=11,
        leading=15,
    )
    compact_style = ParagraphStyle(
        "CoverLetterCompact",
        parent=base_style,
        spaceBefore=0,
        spaceAfter=0,
    )
    section_style = ParagraphStyle(
        "CoverLetterSection",
        parent=compact_style,
        spaceBefore=9,
    )
    subject_style = ParagraphStyle(
        "CoverLetterSubject",
        parent=base_style,
        fontName="Helvetica-Bold",
        spaceBefore=9,
        spaceAfter=7,
    )
    salutation_style = ParagraphStyle(
        "CoverLetterSalutation",
        parent=base_style,
        spaceBefore=0,
        spaceAfter=7,
    )
    body_style = ParagraphStyle(
        "CoverLetterBody",
        parent=base_style,
        spaceBefore=0,
        spaceAfter=7,
    )
    closing_style = ParagraphStyle(
        "CoverLetterClosing",
        parent=compact_style,
        spaceBefore=3,
    )

    story = []
    phase = "header"
    seen_content = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        is_subject = bool(_SUBJECT_LINE.match(line))
        is_salutation = bool(_SALUTATION_LINE.match(line))
        is_closing = bool(_CLOSING_LINE.match(line))

        if phase == "header":
            if is_subject:
                style = subject_style
                phase = "pre_body"
            elif is_salutation:
                style = salutation_style
                phase = "body"
            elif seen_content and _DATE_LINE.match(line):
                style = section_style
            else:
                style = compact_style
        elif phase == "pre_body":
            if is_salutation:
                style = salutation_style
                phase = "body"
            else:
                style = compact_style
        elif phase == "body":
            if is_closing:
                style = closing_style
                phase = "signature"
            else:
                style = body_style
        else:
            style = compact_style

        story.append(Paragraph(escape(line), style))
        seen_content = True

    doc.build(story)
    return path
