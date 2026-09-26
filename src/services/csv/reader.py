import csv
from pathlib import Path


def load_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV with a header row, sniffing the delimiter. Raises ValueError if unreadable or empty."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            dialect = csv.Sniffer().sniff(f.read(4096), delimiters=",;\t|")
            f.seek(0)
            rows = list(csv.DictReader(f, dialect=dialect))
    except (csv.Error, UnicodeDecodeError, OSError) as e:
        raise ValueError(f"{path.name}: {e}") from e
    if not rows:
        raise ValueError(f"{path.name}: no data rows")
    return rows
