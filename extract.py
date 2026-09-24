hello world test
line two"""
Evidence auto-read: pulls a suggested value, date and notes out of an
uploaded photo / PDF / document so the capture form can pre-fill itself.

Best-effort only â every result is meant to be reviewed by the person
before they save the entry, never trusted blind. Nothing here should ever
raise past extract_evidence(); failures degrade to "couldn't read it".
"""
import io
import re
from datetime import datetime


# ----------------------------------------------------------------- text pull

def _pdf_text(file_bytes):
    """Embedded text first; if the PDF looks scanned (little/no text),
    rasterize the first couple of pages and OCR them."""
    text = ""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc:
            text += page.get_text() + "\n"
        if len(text.strip()) < 50:
            try:
                import pytesseract
                from PIL import Image
                ocr_text = ""
                for page in doc[:3]:
                    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
                    img = Image.frombytes(
                        "RGB" if pix.n < 4 else "RGBA", [pix.width, pix.height], pix.samples
                    )
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    ocr_text += pytesseract.image_to_string(img) + "\n"
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
            except Exception:
                pass
        doc.close()
    except Exception:
        pass
    return text


def _image_text(file_bytes, ext):
    try:
        from PIL import Image
        import pytesseract
        if ext == "heic":
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except Exception:
                pass
        img = Image.open(io.BytesIO(file_bytes))
        img = img.convert("RGB")
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def _docx_text(file_bytes):
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" ".join(c.text for c in row.cells))
        return "\n".join(parts)
    except Exception:
        return ""


def _xlsx_text(file_bytes):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" ".join(cells))
        return "\n".join(out)
    except Exception:
        return ""


def _plain_text(file_bytes):
    try:
        return file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _eml_text(file_bytes):
    try:
        import email
        msg = email.message_from_bytes(file_bytes)
        parts = [f"Subject: {msg.get('Subject', '')}", f"Date: {msg.get('Date', '')}"]
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode("utf-8", errors="ignore"))
        return "\n".join(parts)
    except Exception:
        return ""


def extract_text(file_bytes, ext):
    ext = (ext or "").lower()
    if ext == "pdf":
        return _pdf_text(file_bytes)
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "heic"):
        return _image_text(file_bytes, ext)
    if ext in ("docx",):
        return _docx_text(file_bytes)
    if ext in ("xlsx", "xls"):
        return _xlsx_text(file_bytes)
    if ext in ("csv", "txt"):
        return _plain_text(file_bytes)
    if ext == "eml":
        return _eml_text(file_bytes)
    return ""  # doc / msg / unsupported binary formats: no reliable pure-python reader


# ----------------------------------------------------------------- date find

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
     lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    (re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"),
     lambda m: (int(m.group(3)), int(m.group(2)), int(m.group(1)))),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b"),
     lambda m: (int(m.group(3)), MONTHS.get(m.group(2).lower(), 0), int(m.group(1)))),
    (re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b"),
     lambda m: (int(m.group(3)), MONTHS.get(m.group(1).lower(), 0), int(m.group(2)))),
]

_DATE_KEYWORDS = ["date", "period", "billing", "invoice", "statement", "reading", "issued", "due"]


def find_date(text):
    found = []
    for pattern, parser in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                y, mo, d = parser(m)
                if not (1 <= mo <= 12) or not (1 <= d <= 31) or not (2000 <= y <= 2100):
                    continue
                dt = datetime(y, mo, d)
            except Exception:
                continue
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            context = text[line_start:line_end].lower()
            score = 1 + sum(1 for kw in _DATE_KEYWORDS if kw in context)
            found.append((score, m.start(), dt))
    if not found:
        return None
    found.sort(key=lambda x: (-x[0], x[1]))
    return found[0][2].strftime("%Y-%m-%d")


# ---------------------------------------------------------------- value find

_NUMBER = r"([-+]?\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
_VALUE_KEYWORDS = ["total", "consumption", "usage", "amount due", "amount", "net", "reading", "quantity", "volume"]


def find_value(text, unit_field):
    units = [u.strip() for u in re.split(r"[,/]", unit_field or "") if u.strip()]

    # 1. number directly touching a known unit token -> high confidence
    for unit in units:
        if unit.lower() in ("yes", "no"):
            continue
        u = re.escape(unit)
        m = re.search(_NUMBER + r"\s*" + u + r"\b", text, re.IGNORECASE)
        if m:
            return f"{m.group(1).strip()} {unit}", "high"
        m = re.search(u + r"\s*[:\-]?\s*" + _NUMBER, text, re.IGNORECASE)
        if m:
            return f"{m.group(1).strip()} {unit}", "high"

    # 2. percentages
    if any(u == "%" for u in units):
        m = re.search(_NUMBER + r"\s*%", text)
        if m:
            return f"{m.group(1).strip()}%", "high"

    # 3. a number next to a generic total/usage-style keyword -> medium confidence
    for kw in _VALUE_KEYWORDS:
        m = re.search(re.escape(kw) + r"[:\s]{1,15}" + _NUMBER, text, re.IGNORECASE)
        if m:
            hint = next((u for u in units if u.lower() not in ("yes", "no")), "")
            return f"{m.group(1).strip()}{(' ' + hint) if hint else ''}", "medium"

    return None, None


# --------------------------------------------------------------------- notes

def build_notes(text, filename):
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None
    snippet = cleaned[:280] + ("â¦" if len(cleaned) > 280 else "")
    return f"Auto-extracted from {filename}: â{snippet}â"


# --------------------------------------------------------------------- entry

def extract_evidence(file_bytes, filename, item):
    """item: dict-like checklist row (needs 'unit'), or None."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        text = extract_text(file_bytes, ext)
    except Exception:
        text = ""

    if not text or not text.strip():
        return {
            "ok": False,
            "message": "Couldn't read any text from this file â please fill in the fields manually.",
            "value_text": None,
            "value_confidence": None,
            "entry_date": None,
            "notes": None,
            "raw_text": "",
        }

    entry_date = find_date(text)
    unit_field = (item or {}).get("unit") if item else None
    value_text, confidence = find_value(text, unit_field)
    notes = build_notes(text, filename)

    return {
        "ok": True,
        "value_text": value_text,
        "value_confidence": confidence,
        "entry_date": entry_date,
        "notes": notes,
        "raw_text": text[:4000],
    }
