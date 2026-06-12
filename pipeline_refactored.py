"""
pipeline.py  —  pure functions only, no orchestration logic.
Kestra owns the flow; this module owns the data work.
"""
import polars as pl
import pandas as pd
import pandera as pa
from pandera.errors import SchemaErrors
from datetime import datetime
import warnings
import gspread
from google.oauth2.service_account import Credentials
import os


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1  load & validate schema
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2  debit/Kredit integrity
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_add_amount(df: pd.DataFrame) -> pd.DataFrame:
    """
    Guarantee mutual exclusivity of Debet/Kredit per row, then add Amount column.
    Raises ValueError on any violation.
    """
    invalid_mask = (df["Kredit"] != 0) & (df["Debet"] != 0)
    invalid_rows = df.loc[invalid_mask]

    if not invalid_rows.empty:
        raise ValueError(
            f"Data integrity error: {len(invalid_rows)} row(s) have both "
            f"Debet and Kredit non-zero:\n{invalid_rows}"
        )

    result = df.copy()
    result["Amount"] = result["Kredit"] - result["Debet"]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3  summarise
# ─────────────────────────────────────────────────────────────────────────────

def summarize_by_transaction(df: pd.DataFrame, transaction: str | None = None) -> pd.DataFrame:
    if transaction is not None:
        df = df[df["Transaction"] == transaction]

    summary = (
        df.groupby("Transaction", as_index=False)
        .agg(
            Sum_of_Kredit=("Kredit", "sum"),
            Sum_of_Debet=("Debet", "sum"),
            Transaction_Date=(
                "Transaction Date",
                lambda s: pd.to_datetime(s).dt.strftime("%Y-%m-%d").max(),
            ),
        )
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4a  initialise sheet (first-run only)
# ─────────────────────────────────────────────────────────────────────────────

def setup_initial_headers_and_saldo(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    starting_date_str: str,
    starting_balance: int,
) -> None:
    sh = gspread_client.open(target_spreadsheet)
    ws = sh.worksheet(target_worksheet)

    # Column widths
    widths = {0: 100, 1: 240, 2: 140, 3: 140, 4: 140}
    sh.batch_update({"requests": [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        }
        for i, px in widths.items()
    ]})

    date_display = pd.to_datetime(starting_date_str).strftime("%d/%m/%Y")
    ws.update("A1:E2", [
        ["TANGGAL", "KETERANGAN", "DEBET", "KREDIT", "SALDO"],
        [date_display, f"SALDO {date_display}", "", "", starting_balance],
    ], value_input_option="USER_ENTERED")

    ws.format("A1:E1", {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"})
    ws.format("E2", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}})


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4b  append daily block
# ─────────────────────────────────────────────────────────────────────────────

def append_daily_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    summary_df: pd.DataFrame,
) -> tuple[int | None, int | None]:
    """
    Returns (insert_row, footer_end) on success, (None, None) if duplicate.
    """
    target_date_str = summary_df["Transaction_Date"].max()
    formatted_date  = datetime.strptime(target_date_str, "%Y-%m-%d").strftime("%d/%m/%Y")

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=5):
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    COL        = {"red": 0.741, "green": 0.843, "blue": 0.933}

    sh = gspread_client.open(target_spreadsheet)
    ws = sh.worksheet(target_worksheet)

    # Duplicate guard — col A stores dates as DD/MM/YYYY, so compare using formatted_date
    if formatted_date in ws.col_values(1):
        print(f"⚠️  {formatted_date} already exists in sheet — skipping.")
        return None, None

    # Build value map
    val_map = {
        str(row["Transaction"]).strip(): {
            "debet":  float(row.get("Sum_of_Debet",  0) or 0),
            "kredit": float(row.get("Sum_of_Kredit", 0) or 0),
        }
        for _, row in summary_df.iterrows()
    }

    insert_row = len(ws.get_all_values()) + 1
    r = insert_row
    rows_to_append = []

    KETERANGAN = [
        ("TRANSFER MASUK DARI FINPAY", "CASHOUT APOLLO"),
        ("QRISDUWIT",                  "QRISDUWIT"),
        ("DISBURSEMENT",               "DISBURSEMENT"),
        ("PPOB",                       "FeeTransaksi"),
        ("NGRS",                       "RECHARGE"),
        ("BIAYA FEE NGRS",             "RECHARGEFEE"),
        ("RESERVAL",                   "Reversal"),
        ("ST",                         "SELLTHRU"),
        ("BIAYA FEE ST",               "SELLTHRUFEE"),
        ("BIAYA FEE BAR A. ST",        "SELLTHRUSALESFEE"),
    ]

    first = True
    for label, key in KETERANGAN:
        d = val_map.get(key, {})
        debet  = float(d.get("debet",  0))
        kredit = float(d.get("kredit", 0))
        if first:
            prev   = f"E$1:E{r - 1}"
            e_fmla = f"=IFERROR(INDEX({prev},MATCH(9.99E+307,{prev})),0)+C{r}-D{r}"
            rows_to_append.append([formatted_date, label, kredit, debet, e_fmla])
            first = False
        else:
            rows_to_append.append(["", label, kredit, debet, f"=E{r - 1}+C{r}-D{r}"])
        r += 1

    def _net(*keys):
        return sum(
            float(val_map.get(k, {}).get("kredit", 0)) -
            float(val_map.get(k, {}).get("debet",  0))
            for k in keys
        )

    footer_values = [
        _net("RECHARGE", "RECHARGEFEE", "Reversal"),
        _net("FeeTransaksi"),
        _net("SELLTHRU", "SELLTHRUFEE", "SELLTHRUSALESFEE"),
        _net("DISBURSEMENT"),
        _net("QRISDUWIT")
    ]
    footer_values.append(sum(footer_values))

    FOOTER_LABELS = ["NGRS", "PPOB", "ST","DISBURSEMENT", "QRISDUWIT", "Total"]
    for i, f_label in enumerate(FOOTER_LABELS):
        rows_to_append.append([formatted_date if i == 0 else "", f_label, footer_values[i], "", ""])
        r += 1

    ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    # Formatting
    data_start   = insert_row
    data_end     = insert_row + len(KETERANGAN)
    footer_start = data_end
    footer_end   = footer_start + len(FOOTER_LABELS) - 1
    IDR = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    ws.format(_range(data_start, data_end),    {"backgroundColor": COL_WHITE})
    ws.format(f"C{data_start}:E{data_end}",    {"numberFormat": IDR})
    ws.format(_range(footer_start, footer_end, ec=3), {
        "backgroundColor": COL,
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER},
    })
    ws.format(f"C{footer_start}:D{footer_end}", {"numberFormat": IDR})
    ws.format(_range(footer_start, footer_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })
    ws.format(f"A{insert_row}", {
        "textFormat": {"bold": True, "foregroundColor": COL_HEADER, "fontSize": 10}
    })
    ws.format(f"C{footer_end}", {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })

    return insert_row, footer_end


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4  coordinator  (used by the upload task)
# ─────────────────────────────────────────────────────────────────────────────

def process_daily_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    summary_df: pd.DataFrame,
    starting_balance_date: str,
    default_starting_balance: int,
    gspread_client,
) -> tuple[int | None, int | None]:
    sh = gspread_client.open(target_spreadsheet)

    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=1000, cols=50)

    if not ws.acell("A1").value:
        setup_initial_headers_and_saldo(
            gspread_client, target_spreadsheet, target_worksheet,
            starting_date_str=starting_balance_date,
            starting_balance=default_starting_balance,
        )

    return append_daily_to_gsheet(
        gspread_client, target_spreadsheet, target_worksheet, summary_df
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build an authenticated gspread client from a service-account key
# ─────────────────────────────────────────────────────────────────────────────

def make_gspread_client(sa_key_path: str):
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    return gspread.authorize(creds)
