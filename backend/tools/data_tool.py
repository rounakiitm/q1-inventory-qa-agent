"""Generic xlsx loader with zero-shot schema inference.

No dataset-specific hardcoding: the header row is detected by scoring rows
for string density, headers are normalized (newlines/whitespace collapse),
dtypes inferred, and an inventory-integrity audit runs when matching columns
are found. Any tabular xlsx can be dropped in.
"""

import hashlib
import re
from pathlib import Path

import pandas as pd

_MARKERS = {
    "opening": re.compile(r"opening", re.IGNORECASE),
    "stock_in": re.compile(r"stock.?in|purchase", re.IGNORECASE),
    "sold": re.compile(r"sold|sales", re.IGNORECASE),
    "hand_in": re.compile(r"hand.?in|closing|available", re.IGNORECASE),
}

_SUGGESTIONS = [
    "Which product has the highest {hand}?",
    "What is the total {cost} of all stock currently in hand?",
    "Which 5 products have the lowest {turnover}?",
    "How many products cost more than $500 per unit?",
]


def _clean_header(value) -> str:
    name = " ".join(str(value).split())
    name = re.sub(r"([/-])\s+", r"\1", name)  # 'Hand-In- Stock' -> 'Hand-In-Stock'
    name = re.sub(r"\s+([/-])", r"\1", name)  # 'FOO - BAR' -> 'FOO-BAR'
    return name


def _score_row(row) -> float:
    vals = [v for v in row if pd.notna(v)]
    if not vals:
        return 0.0
    strs = sum(1 for v in vals if isinstance(v, str) and str(v).strip())
    return strs + 0.1 * len(vals)


def infer_header_and_load(path: str | Path) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None)
    if raw.empty:
        raise ValueError("the uploaded file has no data rows")

    header_idx = max(range(min(25, len(raw))), key=lambda i: _score_row(raw.iloc[i]))
    header_vals = [_clean_header(v) for v in raw.iloc[header_idx]]

    columns: list[str] = []
    seen: dict[str, int] = {}
    for i, name in enumerate(header_vals):
        if not name or name.lower() in {"unnamed", "nan", "none"}:
            name = f"col_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)

    df = raw.iloc[header_idx + 1 :].copy()
    df.columns = columns[: df.shape[1]]
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if df.empty:
        raise ValueError("no data rows found below the detected header")
    return df


def _col_type(series: pd.Series) -> str:
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "string"


class DataTool:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.df = infer_header_and_load(self.path)
        self.fingerprint = hashlib.md5(self.path.read_bytes()).hexdigest()[:12]
        self.schema_card = self._schema_card()
        self.audit = self._audit()

    # ------------------------------------------------------------------ schema
    def _schema_card(self) -> str:
        lines = [
            f"- {col} ({_col_type(self.df[col])})" for col in self.df.columns
        ]
        sample = self.df.head(3).to_string(index=False)
        return (
            f"File: {self.path.name} | rows: {len(self.df)}\n"
            + "\n".join(lines)
            + "\nSample rows:\n"
            + sample
        )

    # ------------------------------------------------------------------- audit
    def _audit(self) -> dict:
        audit: dict = {
            "rows": len(self.df),
            "columns": int(self.df.shape[1]),
            "null_cells": int(self.df.isna().sum().sum()),
            "duplicate_rows": int(self.df.duplicated().sum()),
            "notes": [],
            "integrity": None,
            "suggested_questions": [],
        }

        # outliers on numeric columns (z-score > 4 to avoid noise on tiny data)
        numeric = self.df.select_dtypes("number")
        outliers = {}
        for col in numeric.columns:
            s = numeric[col].dropna()
            if len(s) < 5 or s.std() == 0:
                continue
            z = ((s - s.mean()) / s.std()).abs()
            hits = int((z > 4).sum())
            if hits:
                outliers[col] = hits
        if outliers:
            audit["notes"].append(f"possible outliers (z>4): {outliers}")

        # inventory integrity: opening + stock_in - sold == hand_in
        cols = {key: self._find_col(rx) for key, rx in _MARKERS.items()}
        if all(cols.values()):
            opening, stock_in, sold, hand = (self.df[c] for c in cols.values())
            expected = opening + stock_in - sold
            diff = hand - expected
            bad = self.df[diff.abs() > 1e-9]
            audit["integrity"] = {
                "rule": "opening + stock_in - sold == hand_in",
                "columns": cols,
                "violations": len(bad),
                "example_rows": bad.head(3).to_dict(orient="records"),
            }
            audit["notes"].append(
                f"{len(bad)} rows violate the stock-integrity equation"
                " (opening + stock_in - sold != hand_in)"
            )
            audit["suggested_questions"] = [
                s.format(
                    hand=cols["hand_in"],
                    cost="cost price total",
                    turnover=f"units sold / opening stock ({cols['opening']})",
                )
                for s in _SUGGESTIONS
            ]
        else:
            audit["notes"].append(
                "dataset does not look like inventory stock-flow data;"
                " integrity check skipped"
            )
        return audit

    def _find_col(self, regex: re.Pattern) -> str | None:
        for col in self.df.columns:
            if regex.search(col):
                return col
        return None

    def find_col(self, key: str) -> str | None:
        return self._find_col(_MARKERS[key]) if key in _MARKERS else None
