from reportlab.platypus import Paragraph, Spacer

import job_hunter.pdf as pdf_module
from job_hunter.pdf import render_cover_letter_pdf


def _capture_story(monkeypatch, tmp_path, text):
    captured = {}

    def capture_build(_doc, story):
        captured["story"] = list(story)

    monkeypatch.setattr(pdf_module.SimpleDocTemplate, "build", capture_build)
    render_cover_letter_pdf(text, "Acme", "Engineer", tmp_path)
    return captured["story"]


def _paragraphs(story):
    return [item for item in story if isinstance(item, Paragraph)]


def test_render_pdf_has_pdf_signature(tmp_path):
    path = render_cover_letter_pdf("Amit Baz\n\nDear Hiring Team,\nHello.", "Acme", "Senior Product Engineer", tmp_path)
    assert path.name == "Acme_Senior_Product_Engineer_Cover_Letter.pdf"
    assert path.read_bytes().startswith(b"%PDF")


def test_render_pdf_sanitizes_filename(tmp_path):
    path = render_cover_letter_pdf("Hello.", "Acme & Co.! ", "Staff / Engineer", tmp_path)
    assert path.name == "Acme_Co._Staff_Engineer_Cover_Letter.pdf"
    assert path.exists()


def test_render_pdf_escapes_special_characters(tmp_path):
    path = render_cover_letter_pdf("Dear <Team> & 'friends'", "Acme", "Engineer", tmp_path)
    assert path.read_bytes().startswith(b"%PDF")


def test_render_pdf_applies_business_letter_spacing_with_single_newlines(monkeypatch, tmp_path):
    text = "\n".join(
        [
            "Amit Baz",
            "Berlin, Germany",
            "amit@example.com",
            "September 4, 2026",
            "RELEX Solutions",
            "Helsinki, Finland",
            "Subject: Application for Senior Product Engineer",
            "Dear Hiring Team,",
            "First body paragraph with enough text to be a real paragraph.",
            "Second body paragraph with different content.",
            "Best regards,",
            "Amit Baz",
        ]
    )

    story = _capture_story(monkeypatch, tmp_path, text)
    paragraphs = _paragraphs(story)

    assert not any(isinstance(item, Spacer) for item in story)
    assert [p.text for p in paragraphs] == text.splitlines()

    # Sender/contact lines stay compact.
    assert [p.style.spaceAfter for p in paragraphs[:3]] == [0, 0, 0]

    # Date starts a new recipient/company block; its following lines remain compact.
    assert paragraphs[3].style.spaceBefore > 0
    assert paragraphs[4].style.spaceBefore == 0
    assert paragraphs[5].style.spaceBefore == 0

    # Subject, salutation, and body paragraphs have visible hierarchy/separation.
    assert paragraphs[6].style.spaceBefore > 0
    assert paragraphs[6].style.spaceAfter > 0
    assert paragraphs[7].style.spaceAfter > 0
    assert paragraphs[8].style.spaceAfter > 0
    assert paragraphs[9].style.spaceAfter > 0

    # Closing separates from the body, while signature lines remain compact.
    assert paragraphs[10].style.spaceBefore > 0
    assert paragraphs[10].style.spaceAfter == 0
    assert paragraphs[11].style.spaceAfter == 0


def test_render_pdf_normalizes_blank_lines_without_double_spacing(monkeypatch, tmp_path):
    single_newlines = "Amit Baz\nBerlin, Germany\namit@example.com\nSeptember 4, 2026\nAcme\nSubject: Engineer\nDear Hiring Team,\nBody one.\nBody two.\nBest regards,\nAmit Baz"
    blank_lines = "Amit Baz\n\nBerlin, Germany\n\namit@example.com\n\nSeptember 4, 2026\n\nAcme\n\nSubject: Engineer\n\nDear Hiring Team,\n\nBody one.\n\nBody two.\n\nBest regards,\n\nAmit Baz"

    single_story = _capture_story(monkeypatch, tmp_path, single_newlines)
    blank_story = _capture_story(monkeypatch, tmp_path, blank_lines)
    single_paragraphs = _paragraphs(single_story)
    blank_paragraphs = _paragraphs(blank_story)

    assert not any(isinstance(item, Spacer) for item in blank_story)
    assert [p.text for p in single_paragraphs] == [p.text for p in blank_paragraphs]
    assert [(p.style.spaceBefore, p.style.spaceAfter) for p in single_paragraphs] == [
        (p.style.spaceBefore, p.style.spaceAfter) for p in blank_paragraphs
    ]


def test_render_pdf_keeps_wrapped_body_text_in_one_paragraph(monkeypatch, tmp_path):
    long_body = "This is a deliberately long body paragraph " * 12
    story = _paragraphs(_capture_story(monkeypatch, tmp_path, f"Dear Hiring Team,\n{long_body}"))

    assert len(story) == 2
    body = story[1]
    _width, height = body.wrap(150, 1000)
    assert height > body.style.leading
    assert body.style.spaceAfter > 0
