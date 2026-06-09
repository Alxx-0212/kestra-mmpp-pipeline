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


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1  load & validate schema
# ─────────────────────────────────────────────────────────────────────────────

def load_and_validate_schema(path: str) -> pd.DataFrame:
    """
    Read the CSV, infer a pandera schema from the full data, then validate
    against it. Raises ValueError on inconsistencies, returns a pandas DataFrame.
    Tries common delimiters automatically if the default ';' yields a single column.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*is an inferred schema that hasn't been modified.*",
            category=UserWarning,
        )
        try:
            df_polars = pl.read_csv(
                path,
                infer_schema_length=None,
                try_parse_dates=True,
                separator=";",
            )
        except Exception:
            df_polars = pl.read_csv(
                path,
                infer_schema_length=None,
                try_parse_dates=True,
                separator=";",
                ignore_errors=True,
            )
        if df_polars.shape[1] <= 1:
            for sep in (",", "|", "\t"):
                try:
                    df_polars = pl.read_csv(
                        path,
                        infer_schema_length=None,
                        try_parse_dates=True,
                        separator=sep,
                    )
                except Exception:
                    continue
                if df_polars.shape[1] > 1:
                    break
        df = df_polars.to_pandas()
        inferred_schema = pa.infer_schema(df)

    try:
        inferred_schema.validate(df, lazy=True)
    except SchemaErrors as e:
        raise ValueError(f"Schema inconsistencies found:\n{e.failure_cases}") from e

    return df


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2  debit/credit integrity
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_add_amount(df: pd.DataFrame) -> pd.DataFrame:
    """
    Guarantee mutual exclusivity of Debet/Credit per row, then add Amount column.
    Raises ValueError on any violation.
    Column lookup is case-insensitive and whitespace-insensitive.
    """
    col_map = {str(c).strip(): str(c).strip() for c in df.columns}
    # Case-insensitive lookup, preferring exact match first
    def _resolve(name: str) -> str:
        if name in col_map:
            return col_map[name]
        lowered = name.lower()
        for orig in df.columns:
            if str(orig).strip().lower() == lowered:
                return str(orig).strip()
        raise KeyError(name)

    debit_col = _resolve("Debet")
    credit_col = _resolve("Credit")

    invalid_mask = (df[credit_col] != 0) & (df[debit_col] != 0)
    invalid_rows = df.loc[invalid_mask]
    if not invalid_rows.empty:
        raise ValueError(
            f"Data Integrity Error! {len(invalid_rows)} row(s) have both "
            f"Debet and Credit non-zero:\n{invalid_rows}"
        )
    result = df.copy()
    result["Amount"] = result[credit_col] - result[debit_col]
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
            Sum_of_Credit=("Credit", "sum"),
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

    # Duplicate guard
    if target_date_str in ws.col_values(1):
        return None, None

    # Build value map
    val_map = {
        str(row["Transaction"]).strip(): {
            "debet":  float(row.get("Sum_of_Debet",  0) or 0),
            "kredit": float(row.get("Sum_of_Credit", 0) or 0),
        }
        for _, row in summary_df.iterrows()
    }

    insert_row = len(ws.get_all_values()) + 1
    r = insert_row
    rows_to_append = []

    KETERANGAN = [
        ("TRANSFER MASUK DARI FINPAY", "CASHOUT APOLLO"),
        ("QRISDUWIT",                  "QRISDUWIT"),
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
    ]
    footer_values.append(sum(footer_values))

    FOOTER_LABELS = ["NGRS", "PPOB", "ST", "Total"]
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
