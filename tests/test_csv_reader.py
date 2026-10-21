from orbitrows.services.csv.reader import first_rows, read_table

FLAT = "tests/fixtures/cases/flat/orders_flat.csv"


def test_read_table_with_header():
    t = read_table(FLAT)
    assert t["name"] == "orders_flat" and t["delimiter"] == "," and t["encoding"] == "utf-8"
    assert t["has_header"] and t["header"][0] == "order_id" and t["rows"][0][0] == "101"


def test_has_header_can_be_forced(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2\n")
    t = read_table(f, has_header=False)
    assert t["header"] == ["col_1", "col_2"] and t["rows"] == [["a", "b"], ["1", "2"]]


def test_semicolon_and_windows_1252(tmp_path):
    f = tmp_path / "t.csv"
    f.write_bytes("nom;prix\nCafé;3\nThé;4\n".encode("cp1252"))
    t = read_table(f, has_header=True)
    assert t["encoding"] == "cp1252" and t["delimiter"] == ";" and t["rows"][0] == ["Café", "3"]


def test_blank_rows_are_skipped(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b\n\n1,2\n , \n")
    assert read_table(f, has_header=True)["rows"] == [["1", "2"]]


def test_first_rows():
    r = first_rows(FLAT, n=2)
    assert len(r["rows"]) == 2 and r["rows"][0][0] == "order_id" and r["sniffer_header"] in (True, False, None)
