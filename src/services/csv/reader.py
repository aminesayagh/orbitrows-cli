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
