"""Tests for the tolerant product-catalog CSV parser."""
from app.csv_import import parse_products


def test_parse_basic():
    text = "name,category,exw_price,supplier\nRebar,metals,590,Isfahan Steel\n"
    rows, errors = parse_products(text)
    assert errors == []
    assert rows[0]["name"] == "Rebar"
    assert rows[0]["exw_price"] == 590.0
    assert rows[0]["supplier"] == "Isfahan Steel"


def test_column_aliases_and_numeric():
    text = "product,price,moq,hs\nCement,55,100,2523\n"
    rows, _ = parse_products(text)
    assert rows[0]["name"] == "Cement"          # 'product' -> name
    assert rows[0]["exw_price"] == 55.0          # 'price' -> exw_price
    assert rows[0]["min_order_qty"] == 100.0     # 'moq' -> min_order_qty
    assert rows[0]["hs_code"] == "2523"          # 'hs' -> hs_code


def test_missing_name_column_is_an_error():
    rows, errors = parse_products("category,price\nmetals,590\n")
    assert rows == [] and errors


def test_bad_number_reported_not_crashing():
    rows, errors = parse_products("name,exw_price\nRebar,abc\n")
    assert rows[0]["exw_price"] == 0.0
    assert any("not a number" in e for e in errors)


def test_row_missing_name_is_skipped():
    rows, _ = parse_products("name,category\n,metals\nRebar,metals\n")
    assert len(rows) == 1 and rows[0]["name"] == "Rebar"
