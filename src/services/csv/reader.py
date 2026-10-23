"""Read a CSV with the standard csv module. A BOM or valid UTF-8 identifies the encoding;
otherwise the caller must choose it. Delimiters are detected by csv.Sniffer."""
import codecs
import csv
import io
from pathlib import Path

from charset_normalizer import from_bytes


_BOMS = [(codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),     # before UTF-16: FF FE 00 00 starts both
         (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig")]
COMMON = ("cp1252", "cp1250", "cp1251", "cp1254", "mac_roman", "latin-1")


class EncodingNeeded(ValueError):
    """The bytes have no certain encoding; options contain actual decoded lines for a user to inspect."""

    def __init__(self, options: list[dict]):
        super().__init__("The file's encoding isn't certain")
        self.options = options


def encoding_options(raw: bytes) -> list[dict]:
    """Common export encodings first, then the detector's suggestions. Its ranking is not treated as certainty."""
    candidates = list(COMMON)
    candidates.extend(match.encoding for match in from_bytes(raw))
    if b"\x00" in raw:                                      # UTF-16/32 without a BOM can look like valid UTF-8
        candidates = ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", *candidates]
    options, seen = [], set()
    for name in candidates:
        try:
            canonical = codecs.lookup(name).name
            if canonical in seen:
                continue
            decoded = raw.decode(canonical)
        except (LookupError, UnicodeError):
            continue
        seen.add(canonical)
        lines = [line for line in decoded.splitlines() if line.strip()]
        line = next((line for line in lines if any(ord(c) > 127 for c in line)), lines[0] if lines else "")
        preview = "".join(c if c.isprintable() else "�" for c in line[:120])
        options.append({"answer": name, "preview": preview})
        if len(options) == 12:
            break
    return options


def _decode(raw, encoding=None):
    """(text, encoding name as Python writes it back: the BOM kept for utf-8-sig/utf-16/utf-32)."""
    if encoding is not None:
        try:
            name = codecs.lookup(encoding).name
            return raw.decode(name), name
        except LookupError:
            raise ValueError(f"Unknown encoding: {encoding}") from None
        except UnicodeDecodeError:
            raise ValueError(f"The file cannot be read as {encoding}.") from None
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return raw.decode(name), name
    try:
        text = raw.decode("utf-8")
        if "\x00" not in text:
            return text, "utf-8"
    except UnicodeDecodeError:
        pass
    raise EncodingNeeded(encoding_options(raw))


def _parse(path, encoding=None):
    """(encoding, dialect, sample, rows) of the file. A blank line between records is an all-empty row; blank lines
    before the first record or after the last are not rows."""
    text, encoding = _decode(Path(path).read_bytes(), encoding)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text, newline=""), dialect))          # newline="": line breaks in cells survive
    while rows and not rows[-1]:
        rows.pop()
    while rows and not rows[0]:
        rows.pop(0)
    return encoding, dialect, sample, [r or [""] * len(rows[0]) for r in rows]


def sniffer_header(sample) -> bool:
    """csv.Sniffer's per-column vote: misses headers as long as their values and all-text tables (a hint only)."""
    try:
        return csv.Sniffer().has_header(sample)
    except (csv.Error, ValueError):
        return True


def first_rows(path, n=5, encoding=None) -> dict:
    """{"rows": the first n non-empty rows as lists, "sniffer_header": Sniffer's guess, None when it fails}: evidence for
    the header step. (Sniffer raises ValueError on some single-column files; read_table keeps that behaviour.)"""
    _, _, sample, rows = _parse(path, encoding)
    try:
        hint = sniffer_header(sample)
    except ValueError:
        hint = None
    return {"rows": rows[:n], "sniffer_header": hint}


def read_table(path, has_header=None, encoding=None) -> dict:
    """{"name", "path", "encoding", "delimiter", "has_header", "header": [...], "rows": [[...], ...]}.
    has_header None → Sniffer's guess; a bool forces it. Without a header row, columns are named col_1, col_2, …"""
    encoding, dialect, sample, rows = _parse(path, encoding)
    if has_header is None:
        has_header = sniffer_header(sample)
    header = rows[0] if has_header else [f"col_{i}" for i in range(1, len(rows[0]) + 1)]
    return {"name": Path(path).stem, "path": str(path), "encoding": encoding, "delimiter": dialect.delimiter,
            "has_header": has_header, "header": header, "rows": rows[1:] if has_header else rows}
