"""Shared pipe drawing-type classification helpers.

Both ``workflow_handler`` and ``review.compliance`` need to classify a pipe
drawing (plan / riser / system_diagram / ...) from its title or surrounding
text. Keeping the patterns in one place prevents the two paths from drifting
out of sync.
"""

from __future__ import annotations

import re

PIPE_DRAWING_TYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "support_layout",
        re.compile(
            r"(?:행거|서포트|지지장치|내진\s*지지|hanger|support|restraint|brace)"
            r".*(?:도|plan|layout|detail|dwg|drawing)",
            re.IGNORECASE,
        ),
    ),
    (
        "shop_detail",
        re.compile(
            r"시공\s*상세|상세도|샵\s*드로잉|shop\s*drawing|shop\s*dwg|detail",
            re.IGNORECASE,
        ),
    ),
    ("isometric", re.compile(r"아이소|isometric|\biso\b", re.IGNORECASE)),
    ("riser", re.compile(r"입상도|라이저|riser|vertical", re.IGNORECASE)),
    (
        "system_diagram",
        re.compile(
            r"계통도|계통|schematic|diagram|p\s*&\s*id|\bpid\b",
            re.IGNORECASE,
        ),
    ),
    ("section", re.compile(r"단면도|단면|section|elevation", re.IGNORECASE)),
    ("schedule", re.compile(r"일람표|목록|schedule|list", re.IGNORECASE)),
    ("plan", re.compile(r"평면도|평면|floor\s*plan|\bplan\b", re.IGNORECASE)),
]


def pipe_drawing_type_from_text(text: str | None) -> str:
    """Return the canonical drawing type for a free-form text fragment."""
    haystack = text or ""
    for drawing_type, pattern in PIPE_DRAWING_TYPE_PATTERNS:
        if pattern.search(haystack):
            return drawing_type
    return "unknown"


def classify_pipe_drawing_type(
    drawing_title: str | None,
    text_keywords: list[str] | None = None,
) -> str:
    """Classify the drawing type from the title plus extracted text keywords."""
    text = " ".join([str(drawing_title or ""), *(str(x) for x in (text_keywords or []))])
    return pipe_drawing_type_from_text(text)
