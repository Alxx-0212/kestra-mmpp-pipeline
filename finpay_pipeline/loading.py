"""Load FinPay CSV/XLS/XLSX files and validate the input schema."""
import os

import pandas as pd
import pandera as pa
from pandera.errors import SchemaErrors

FINPAY_SCHEMA = pa.DataFrameSchema(
    columns={
        "No":               pa.Column(pa.Int64,    nullable=False),
        "Transaction Date": pa.Column(pa.DateTime, nullable=False),
        "Transaction ID":   pa.Column(pa.String,   nullable=False),
        "Saldo Awal":       pa.Column(pa.Int64,    nullable=False),
        "Kredit":           pa.Column(pa.Int64,    nullable=False),
        "Debet":            pa.Column(pa.Int64,    nullable=False),
        "Saldo Akhir":      pa.Column(pa.Int64,    nullable=False),
        "Transaction Type": pa.Column(pa.String,   nullable=False),
        "Transaction":      pa.Column(pa.String,   nullable=False),
        "Nomor RS":         pa.Column(pa.String,   nullable=False),
        "Remarks":          pa.Column(pa.String,   nullable=False),
    },
    strict=True,       # fail if the file has extra columns not in the schema
    ordered=False,     # don't require columns to be in this exact order
)

def _detect_file_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".xlsx", ".xls"):
        return ext
    with open(path, "rb") as f:
        header = f.read(8)
    if header[:4] == b"PK\x03\x04":
        return ".xlsx"
    if header[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".xls"
    return ".csv"

def _find_header_row(df_raw: pd.DataFrame) -> int:
    """
    Scan the first 10 rows of a raw (header=None) DataFrame and return the
    index of the row that looks like the column header.
    Detection: the row whose non-null values best overlap with known FINPAY
    column names.
    """
    KNOWN_COLS = {
        "No", "Transaction Date", "Transaction ID", "Saldo Awal",
        "Kredit", "Debet", "Saldo Akhir", "Transaction Type",
        "Transaction", "Nomor RS", "Remarks",
    }
    best_idx, best_score = 0, 0
    for i in range(min(10, len(df_raw))):
        row_vals = {str(v).strip() for v in df_raw.iloc[i].dropna()}
        score = len(row_vals & KNOWN_COLS)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


def _coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Coerce numeric and datetime columns so dtypes are consistent regardless of
    whether the source was CSV or Excel.
    """
    # integer-like numeric columns → int64
    for col in df.select_dtypes(include=["number"]).columns:
        if (df[col].dropna() % 1 == 0).all():
            df[col] = df[col].astype("int64")

    # string columns that are actually numbers (e.g. "543,174,458")
    for col in df.select_dtypes(include=["object"]).columns:
        stripped = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        try:
            converted = pd.to_numeric(stripped, errors="raise")
            if (converted.dropna() % 1 == 0).all():
                df[col] = converted.astype("int64")
            else:
                df[col] = converted
        except (ValueError, TypeError):
            pass

    # date parsing
    for col in df.select_dtypes(include=["object"]).columns:
        try:
            parsed = pd.to_datetime(df[col], format="mixed", dayfirst=True)
            if parsed.notna().sum() > len(df) * 0.5:
                df[col] = parsed
        except (ValueError, TypeError):
            pass

    return df


def _read_with_header_detect(
    read_fn,
    path: str,
    **kwargs,
) -> pd.DataFrame:
    """
    Generic reader that:
    1. Reads with header=None
    2. Auto-detects the real header row via _find_header_row
    3. Slices data from the row below the header
    4. Applies _coerce_dtypes for consistent output dtypes
    """
    df_raw = read_fn(path, header=None, **kwargs)
    header_row = _find_header_row(df_raw)
    df = df_raw.iloc[header_row + 1:].reset_index(drop=True)
    df.columns = [str(v).strip() for v in df_raw.iloc[header_row]]
    return _coerce_dtypes(df)


def load_file(path: str) -> pd.DataFrame:
    """
    Load a CSV or Excel file into a pandas DataFrame.
    Both formats use header=None + auto-detection of the real header row,
    then numeric and datetime coercion for consistent downstream typing.
    """
    ext = _detect_file_type(path)

    if ext in (".xlsx", ".xls"):
        return _read_with_header_detect(pd.read_excel, path)

    if ext == ".csv":
        # Try separators in priority order
        for sep in (";", ",", "|", "\t"):
            try:
                df = _read_with_header_detect(
                    pd.read_csv, path, sep=sep, dtype=str, encoding="utf-8",
                )
                if df.shape[1] > 1:
                    return df
            except Exception:
                continue
        raise ValueError(f"Could not parse CSV with any known separator: {path}")

    raise ValueError(f"Unsupported file format: {ext}. Expected .csv, .xlsx, or .xls")


def _transform_to_schema(df: pd.DataFrame) -> pd.DataFrame:
    """
    Coerce raw file output into the types expected by FINPAY_SCHEMA.
    - Strip whitespace from all string columns
    - Remove thousands-separator commas from numeric columns and cast to Int64
    - Parse 'Transaction Date' into a proper datetime
    """
    result = df.copy()

    INT_COLS = ["No", "Saldo Awal", "Kredit", "Debet", "Saldo Akhir"]
    for col in INT_COLS:
        if col in result.columns:
            result[col] = (
                result[col]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
                .astype("int64")
            )

    if "Transaction Date" in result.columns:
        result["Transaction Date"] = pd.to_datetime(
            result["Transaction Date"],
            format="%d-%m-%Y %H:%M:%S",
        )

    STR_COLS = ["Transaction ID", "Transaction Type", "Transaction",
                "Nomor RS", "Remarks"]
    for col in STR_COLS:
        if col in result.columns:
            result[col] = result[col].astype(str).str.strip()

    return result


def load_and_validate_schema(path: str) -> pd.DataFrame:
    df = load_file(path)
    df = _transform_to_schema(df)  # coerce types before validation

    try:
        FINPAY_SCHEMA.validate(df, lazy=True)
        print("Data consistent with FINPAY schema!")
    except SchemaErrors as e:
        raise ValueError(f"Schema validation failed:\n{e.failure_cases}") from e

    return df
