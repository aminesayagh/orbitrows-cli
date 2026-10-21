"""Read a CSV file with the standard csv module: encoding (UTF-8, else Windows-1252) and delimiter detected by
csv.Sniffer; the header decision is given by the caller (the header step asks Jev), else Sniffer's guess."""
import csv
from pathlib import Path


def load_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a CSV with a header row, sniffing the delimiter. Returns (header, rows).

    Rows are lists, not dicts: headers can be empty or duplicated. Raises ValueError if unreadable or empty.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            dialect = csv.Sniffer().sniff(f.read(4096), delimiters=",;\t|")
            f.seek(0)
            header, *rows = csv.reader(f, dialect=dialect)
    except (csv.Error, UnicodeDecodeError, OSError) as e:
        raise ValueError(f"{path.name}: {e}") from e
    except ValueError:  # nothing to unpack: empty file
        raise ValueError(f"{path.name}: empty file") from None
    for n, row in enumerate(rows, 2):
        if len(row) > len(header):
            raise ValueError(f"{path.name}: line {n} has more fields than the header")
    rows = [row for row in rows if any(row)]  # skip blank lines
    if not rows:
        raise ValueError(f"{path.name}: no data rows")
    return header, rows


def _parse(path):
    """(encoding, dialect, sample, non-empty rows) of the file."""
    raw = Path(path).read_bytes()
    try:
        text, encoding = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("cp1252"), "cp1252"           # ponytail: two encodings cover the cases
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return encoding, dialect, sample, [r for r in csv.reader(text.splitlines(), dialect) if any(c.strip() for c in r)]


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
