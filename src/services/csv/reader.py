"""Read a CSV file with the standard csv module: encoding (BOM → UTF-8/16/32, else UTF-8, else Windows-1252) and delimiter detected by
csv.Sniffer; the header decision is given by the caller (the header step asks Jev), else Sniffer's guess."""
import codecs
import csv
import io
from pathlib import Path


_BOMS = [(codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),     # before UTF-16: FF FE 00 00 starts both
         (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig")]


def _decode(raw):
    """(text, encoding name as Python writes it back: the BOM kept for utf-8-sig/utf-16/utf-32)."""
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return raw.decode(name), name
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252"), "cp1252"           # strict: the 5 bytes cp1252 leaves undefined refuse it
        except UnicodeDecodeError:
            raise ValueError("Unknown text encoding: expected UTF-8, UTF-16, UTF-32 or Windows-1252.") from None


def _parse(path):
    """(encoding, dialect, sample, rows) of the file. A blank line between records is an all-empty row; blank lines
    before the first record or after the last are not rows."""
    text, encoding = _decode(Path(path).read_bytes())
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
    except csv.Error:
        return True


def first_rows(path, n=5) -> dict:
    """{"rows": the first n non-empty rows as lists, "sniffer_header": Sniffer's guess, None when it fails}: evidence for
    the header step. (Sniffer raises ValueError on some single-column files; read_table keeps that behaviour.)"""
    _, _, sample, rows = _parse(path)
    try:
        hint = sniffer_header(sample)
    except ValueError:
        hint = None
    return {"rows": rows[:n], "sniffer_header": hint}


def read_table(path, has_header=None) -> dict:
    """{"name", "path", "encoding", "delimiter", "has_header", "header": [...], "rows": [[...], ...]}.
    has_header None → Sniffer's guess; a bool forces it. Without a header row, columns are named col_1, col_2, …"""
    encoding, dialect, sample, rows = _parse(path)
    if has_header is None:
        has_header = sniffer_header(sample)
    header = rows[0] if has_header else [f"col_{i}" for i in range(1, len(rows[0]) + 1)]
    return {"name": Path(path).stem, "path": str(path), "encoding": encoding, "delimiter": dialect.delimiter,
            "has_header": has_header, "header": header, "rows": rows[1:] if has_header else rows}
