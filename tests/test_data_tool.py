from pathlib import Path

import pytest

from backend.tools.data_tool import DataTool, infer_header_and_load

DATA = Path(__file__).resolve().parents[1] / "data" / "Inventory-Records-Sample-Data.xlsx"


@pytest.fixture(scope="module")
def tool() -> DataTool:
    return DataTool(DATA)


def test_loads_all_product_rows(tool):
    assert len(tool.df) == 46


def test_headers_are_normalized(tool):
    assert "Opening Stock" in tool.df.columns
    assert "Hand-In-Stock" in tool.df.columns
    assert not any("\n" in c for c in tool.df.columns)
    assert not any(" -" in c or " /" in c for c in tool.df.columns)


def test_generic_loader_is_dataset_agnostic(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["junk", 1])  # noise row above the header
    ws.append(["SKU", "Qty", "Price"])
    ws.append(["A1", 5, 9.99])
    ws.append(["A2", 2, 4.5])
    path = tmp_path / "other.xlsx"
    wb.save(path)
    df = infer_header_and_load(path)
    assert list(df.columns) == ["SKU", "Qty", "Price"]
    assert len(df) == 2


def test_audit_detects_integrity_violations(tool):
    assert tool.audit["integrity"] is not None
    assert tool.audit["integrity"]["violations"] == 12


def test_schema_card_contains_sample(tool):
    assert "rows: 46" in tool.schema_card
