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
import re


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

def validate_debit_credit_integrity(df: pd.DataFrame) -> pd.DataFrame:
    """
    Guarantee mutual exclusivity of Debet/Kredit per row.
    Raises ValueError on any violation.
    """
    invalid_mask = (df["Kredit"] != 0) & (df["Debet"] != 0)
    invalid_rows = df.loc[invalid_mask]

    if not invalid_rows.empty:
        raise ValueError(
            f"Data integrity error: {len(invalid_rows)} row(s) have both "
            f"Debet and Kredit non-zero:\n{invalid_rows}"
        )

    return df.copy()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2a  preprocessing: drop duplicate rows
# ─────────────────────────────────────────────────────────────────────────────

DUPLICATE_UNUSUAL_REASON = "duplicate row removed from calculation"


def _minute_level_dedup_key(df: pd.DataFrame) -> pd.DataFrame:
    dedup_key = df.copy()
    if "No" in dedup_key.columns:
        dedup_key = dedup_key.drop(columns="No")
    dedup_key["Transaction Date"] = (
        pd.to_datetime(dedup_key["Transaction Date"]).dt.floor("min")
    )
    return dedup_key


def _duplicate_unusual_reason(
    duplicate_row: pd.Series,
    kept_row: pd.Series,
    duplicate_minute,
) -> str:
    minute_display = ""
    if not pd.isna(duplicate_minute):
        minute_display = pd.to_datetime(duplicate_minute).strftime("%Y-%m-%d %H:%M")

    return (
        f"{DUPLICATE_UNUSUAL_REASON}"
        f"; duplicate of No={kept_row.get('No', '')}"
        f"; duplicate key minute={minute_display}"
        f"; duplicate Transaction ID={duplicate_row.get('Transaction ID', '')}"
    )


def deduplicate_rows_by_minute_with_report(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split rows into calculation-ready rows and duplicate rows for unusual logging.

    Duplicate detection uses all columns except No as the comparison key.
    Transaction Date is compared at minute precision only.
    The original Transaction Date value is preserved in both returned DataFrames.
    """
    result = df.copy()
    dedup_key = _minute_level_dedup_key(result)

    duplicate_mask = dedup_key.duplicated(keep="first")
    dropped_rows = int(duplicate_mask.sum())
    print(f"Duplicate rows dropped using minute-level datetime: {dropped_rows}")

    first_index_by_key = {}
    duplicate_of_by_index = {}
    for idx, row in dedup_key.iterrows():
        key = tuple(row.tolist())
        if key in first_index_by_key:
            duplicate_of_by_index[idx] = first_index_by_key[key]
        else:
            first_index_by_key[key] = idx

    deduplicated = result.loc[~duplicate_mask].reset_index(drop=True)
    duplicate_unusual = result.loc[duplicate_mask].copy()

    if duplicate_unusual.empty:
        duplicate_unusual["base_id"] = pd.Series(dtype="object")
        duplicate_unusual["unusual_reason"] = pd.Series(dtype="object")
        return deduplicated, duplicate_unusual

    duplicate_unusual["base_id"] = (
        duplicate_unusual["Transaction ID"]
        .astype(str)
        .str.replace(r"(SLSFEE|SALESFEE|FEE)$", "", regex=True)
    )
    duplicate_unusual["unusual_reason"] = [
        _duplicate_unusual_reason(
            row,
            result.loc[duplicate_of_by_index[idx]],
            dedup_key.loc[idx, "Transaction Date"],
        )
        for idx, row in duplicate_unusual.iterrows()
    ]

    return deduplicated, duplicate_unusual.reset_index(drop=True)


def drop_duplicate_rows_by_minute(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop duplicate rows using all columns except No as the comparison key.
    Transaction Date is compared at minute precision only.
    The original Transaction Date value is preserved in the returned rows.
    """
    deduplicated, _ = deduplicate_rows_by_minute_with_report(df)
    return deduplicated


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
        ("RECHARGE OUT CLUSTER",       "RECHARGE OUT CLUSTER"),
        ("RECHARGE OUT CLUSTER FEE",   "RECHARGE OUT CLUSTER FEE"),
        ("REVERSAL NGRS",              REVERSAL_NGRS_CATEGORY),
        ("REVERSAL NGRS FEE",          REVERSAL_NGRS_FEE_CATEGORY),
        ("REVERSAL ST",                REVERSAL_ST_CATEGORY),
        ("REVERSAL ST SELLTHRUFEE",    REVERSAL_ST_SELLTHRU_FEE_CATEGORY),
        ("REVERSAL ST SELLTHRUSALESFEE", REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY),
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

    # Row offsets from insert_row (matches KETERANGAN order):
    #  +1 QRISDUWIT  +2 DISBURSEMENT  +3 FeeTransaksi
    #  +4 RECHARGE   +5 RECHARGEFEE   +6 RECHARGE OUT CLUSTER
    #  +7 RECHARGE OUT CLUSTER FEE    +8 Reversal NGRS
    #  +9 Reversal NGRS FEE  +10 Reversal ST
    #  +11 Reversal ST SELLTHRUFEE  +12 Reversal ST SELLTHRUSALESFEE
    #  +13 SELLTHRU  +14 SELLTHRUFEE  +15 SELLTHRUSALESFEE
    ir = insert_row
    n  = len(KETERANGAN)   # footer rows start at ir + n
    footer_formulas = [
        # NGRS = net(RECHARGE family only)
        f"=C{ir+4}-D{ir+4}+C{ir+5}-D{ir+5}+C{ir+6}-D{ir+6}+C{ir+7}-D{ir+7}",
        # Reversal - NGRS = net(Reversal NGRS - Reversal NGRS fee)
        f"=(C{ir+8}-D{ir+8})+(C{ir+9}-D{ir+9})",
        # PPOB  = net(FeeTransaksi)
        f"=C{ir+3}-D{ir+3}",
        # ST = net(SELLTHRU family only)
        f"=C{ir+13}-D{ir+13}+C{ir+14}-D{ir+14}+C{ir+15}-D{ir+15}",
        # Reversal - ST = net(Reversal ST - reversal sellthru fees)
        f"=(C{ir+10}-D{ir+10})+(C{ir+11}-D{ir+11})+(C{ir+12}-D{ir+12})",
        # DISBURSEMENT
        f"=C{ir+2}-D{ir+2}",
        # QRISDUWIT
        f"=C{ir+1}-D{ir+1}",
    ]
    # Total = sum of the footer C-cells above
    footer_formulas.append(
        "=" + "+".join(f"C{ir + n + j}" for j in range(len(footer_formulas)))
    )

    FOOTER_LABELS = [
        "NGRS",
        "Reversal - NGRS",
        "PPOB",
        "ST",
        "Reversal - ST",
        "DISBURSEMENT",
        "QRISDUWIT",
        "Total",
    ]
    for i, f_label in enumerate(FOOTER_LABELS):
        rows_to_append.append([formatted_date if i == 0 else "", f_label, footer_formulas[i], "", ""])
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


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2b  preprocessing: relabel transaction categories
# ─────────────────────────────────────────────────────────────────────────────

SUMMARY_OUT_CLUSTER_REMARK = 'fee pembelian recharge out cluster'
UNUSUAL_RECHARGE_EXEMPT_REMARK = 'biaya pembelian recharge out cluster'

REVERSAL_TRANSACTION = 'REVERSAL'
REVERSAL_NGRS_CATEGORY = 'Reversal - NGRS'
REVERSAL_NGRS_FEE_CATEGORY = 'Reversal - NGRS FEE'
REVERSAL_ST_CATEGORY = 'Reversal - ST'
REVERSAL_ST_SELLTHRU_FEE_CATEGORY = 'Reversal - ST SELLTHRUFEE'
REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY = 'Reversal - ST SELLTHRUSALESFEE'
REVERSAL_NGRS_MAIN_REMARK = 'biaya pembelian recharge'
REVERSAL_NGRS_OUT_CLUSTER_FEE_REMARK = SUMMARY_OUT_CLUSTER_REMARK
REVERSAL_NGRS_PLATFORM_FEE_REMARK = 'platform fee recharge rp. 20,-'
REVERSAL_ST_MAIN_REMARK = 'sellthru sales fee'
REVERSAL_ST_PLATFORM_FEE_REMARK = 'platform fee sellthru rp. 100,-'
REVERSAL_ST_TRANSACTION_FEE_REMARK = 'fee transaksi sellthru sejumlah 100 rupiah'
REVERSAL_ST_SALES_HOLD_REMARK = 'sales hold transaksi sellthru'

# Transaction group validation rules. A group is keyed by Transaction ID with
# fee suffixes removed, then the main transaction determines the required rows.
TRANSACTION_GROUP_RULES = {
    'RECHARGE': {
        'required': {
            'RECHARGEFEE': {
                'column': 'Debet',
                'equals': 20,
            },
        },
        'exempt_remark': UNUSUAL_RECHARGE_EXEMPT_REMARK,
    },
    'SELLTHRU': {
        'required': {
            'SELLTHRUFEE': {
                'column': 'Debet',
                'equals': 100,
            },
            'SELLTHRUSALESFEE': {
                'presence_only': True,
            },
        },
    },
    REVERSAL_NGRS_CATEGORY: {
        'required': {
            REVERSAL_NGRS_FEE_CATEGORY: {
                'column': 'Kredit',
                'equals': 20,
                'missing_reason': (
                    'missing reversal platform fee remark: '
                    f'{REVERSAL_NGRS_PLATFORM_FEE_REMARK}'
                ),
                'amount_label': 'reversal NGRS platform fee',
            },
        },
        'exempt_remark': UNUSUAL_RECHARGE_EXEMPT_REMARK,
    },
    REVERSAL_ST_CATEGORY: {
        'required': {
            REVERSAL_ST_SELLTHRU_FEE_CATEGORY: {
                'column': 'Kredit',
                'equals': 100,
                'missing_reason': (
                    'missing reversal platform fee remark: '
                    f'{REVERSAL_ST_PLATFORM_FEE_REMARK} '
                    f'or {REVERSAL_ST_TRANSACTION_FEE_REMARK}'
                ),
                'amount_label': 'reversal ST platform fee',
            },
            REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY: {
                'column': 'Kredit',
                'greater_than': 0,
                'missing_reason': (
                    'missing reversal SLSFEE remark: '
                    f'{REVERSAL_ST_SALES_HOLD_REMARK}'
                ),
                'amount_label': 'reversal ST SLSFEE',
                'expected_text': 'non-zero',
            },
        },
    },
}

REVERSAL_CATEGORY_TO_MAIN = {
    REVERSAL_NGRS_CATEGORY: REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY: REVERSAL_NGRS_CATEGORY,
    REVERSAL_ST_CATEGORY: REVERSAL_ST_CATEGORY,
    REVERSAL_ST_SELLTHRU_FEE_CATEGORY: REVERSAL_ST_CATEGORY,
    REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY: REVERSAL_ST_CATEGORY,
}
REVERSAL_MAIN_MISSING_REASONS = {
    REVERSAL_NGRS_CATEGORY: (
        f'missing reversal remark: {REVERSAL_NGRS_MAIN_REMARK} '
        f'or {REVERSAL_NGRS_OUT_CLUSTER_FEE_REMARK}'
    ),
    REVERSAL_ST_CATEGORY: f'missing reversal remark: {REVERSAL_ST_MAIN_REMARK}',
}


def _remarks_contain(remarks: pd.Series, phrase: str) -> pd.Series:
    normalized = remarks.fillna('').astype(str).str.lower()
    return normalized.str.contains(phrase, regex=False)


def _base_id_from_transaction_id(transaction_id: pd.Series) -> pd.Series:
    return (
        transaction_id.astype(str)
        .str.replace(r'(SLSFEE|SALESFEE|FEE)$', '', regex=True)
    )


def relabel_out_cluster_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Relabels RECHARGE / RECHARGEFEE rows that belong to out-cluster groups:
      RECHARGE    -> RECHARGE OUT CLUSTER
      RECHARGEFEE -> RECHARGE OUT CLUSTER FEE
    Out-cluster groups are identified by the RECHARGE row's Remarks containing
    SUMMARY_OUT_CLUSTER_REMARK (case-insensitive).
    """
    df = df.copy()
    df['_base_id'] = _base_id_from_transaction_id(df['Transaction ID'])
    out_cluster_mask = (
        (df['Transaction'] == 'RECHARGE')
        & _remarks_contain(df['Remarks'], SUMMARY_OUT_CLUSTER_REMARK)
    )
    out_cluster_base_ids = set(df.loc[out_cluster_mask, '_base_id'])
    in_group = df['_base_id'].isin(out_cluster_base_ids)
    df.loc[in_group & (df['Transaction'] == 'RECHARGE'),    'Transaction'] = 'RECHARGE OUT CLUSTER'
    df.loc[in_group & (df['Transaction'] == 'RECHARGEFEE'), 'Transaction'] = 'RECHARGE OUT CLUSTER FEE'
    print(f'Out-cluster groups relabeled: {len(out_cluster_base_ids)}')
    return df.drop(columns='_base_id')


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2c  preprocessing: classify reversal rows for summary
# ─────────────────────────────────────────────────────────────────────────────

def _numeric_sum(rows: pd.DataFrame, column: str) -> int | float:
    if rows.empty:
        return 0
    total = pd.to_numeric(rows[column], errors='coerce').fillna(0).sum()
    if float(total).is_integer():
        return int(total)
    return float(total)


def _format_expected_text(rule: dict) -> str:
    if 'expected_text' in rule:
        return str(rule['expected_text'])
    if 'equals' in rule:
        return str(rule['equals'])
    if 'greater_than' in rule:
        return f"> {rule['greater_than']}"
    return 'present'


def _validate_transaction_group_rules(
    group: pd.DataFrame,
    main_transaction: str,
) -> list[str]:
    """
    Validate required companion rows for one transaction group.

    Rules cover the original fee checks and the relabeled reversal categories.
    """
    config = TRANSACTION_GROUP_RULES.get(main_transaction)
    if not config:
        return []

    main_rows = group[group['Transaction'] == main_transaction]
    exempt_remark = config.get('exempt_remark')
    if (
        exempt_remark
        and not main_rows.empty
        and _remarks_contain(main_rows['Remarks'], str(exempt_remark)).any()
    ):
        return []

    reasons = []
    for required_transaction, rule in config.get('required', {}).items():
        required_rows = group[group['Transaction'] == required_transaction]
        if required_rows.empty:
            reasons.append(rule.get('missing_reason', f'missing {required_transaction}'))
            continue

        if rule.get('presence_only'):
            continue

        column = rule.get('column')
        if not column:
            continue

        actual = _numeric_sum(required_rows, str(column))
        label = rule.get('amount_label', required_transaction)

        if 'equals' in rule and actual != rule['equals']:
            expected = _format_expected_text(rule)
            reasons.append(f'{label} {column}={actual} (expected {expected})')
        elif 'greater_than' in rule and not actual > rule['greater_than']:
            expected = _format_expected_text(rule)
            reasons.append(f'{label} {column}={actual} (expected {expected})')

    return reasons


def relabel_reversal_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Relabel source REVERSAL rows into reversal summary/detail categories.

    Rows whose remarks cannot be classified remain as Reversal so validation can
    flag them as unusual and exclude them from summary calculations.
    """
    result = df.copy()
    transaction = result['Transaction'].fillna('').astype(str).str.strip().str.upper()
    reversal_mask = transaction == REVERSAL_TRANSACTION
    if not reversal_mask.any():
        print('Reversal rows relabeled: {}')
        return result

    remarks = result.loc[reversal_mask, 'Remarks']
    category_masks = {
        REVERSAL_NGRS_CATEGORY: (
            _remarks_contain(remarks, REVERSAL_NGRS_MAIN_REMARK)
            | _remarks_contain(remarks, REVERSAL_NGRS_OUT_CLUSTER_FEE_REMARK)
        ),
        REVERSAL_NGRS_FEE_CATEGORY: _remarks_contain(
            remarks,
            REVERSAL_NGRS_PLATFORM_FEE_REMARK,
        ),
        REVERSAL_ST_CATEGORY: _remarks_contain(remarks, REVERSAL_ST_MAIN_REMARK),
        REVERSAL_ST_SELLTHRU_FEE_CATEGORY: _remarks_contain(
            remarks,
            REVERSAL_ST_PLATFORM_FEE_REMARK,
        ) | _remarks_contain(remarks, REVERSAL_ST_TRANSACTION_FEE_REMARK),
        REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY: _remarks_contain(
            remarks,
            REVERSAL_ST_SALES_HOLD_REMARK,
        ),
    }

    match_counts = pd.Series(0, index=remarks.index)
    for mask in category_masks.values():
        match_counts = match_counts.add(mask.astype(int), fill_value=0)

    relabeled_counts: dict[str, int] = {}
    single_match = match_counts == 1
    for category, mask in category_masks.items():
        relabel_mask = reversal_mask.copy()
        relabel_mask.loc[:] = False
        relabel_mask.loc[remarks.index] = mask & single_match
        result.loc[relabel_mask, 'Transaction'] = category
        count = int(relabel_mask.sum())
        if count:
            relabeled_counts[category] = count

    ambiguous_rows = int((match_counts > 1).sum())
    unclassified_rows = int((match_counts == 0).sum())
    print(f'Reversal rows relabeled: {relabeled_counts}')
    print(f'Reversal rows ambiguous after relabel: {ambiguous_rows}')
    print(f'Reversal rows unclassified after relabel: {unclassified_rows}')
    return result


def preprocess_transaction_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply all transaction relabeling before unusual detection and downstream
    calculation/detail paths.
    """
    result = relabel_reversal_transactions(df)
    result = relabel_out_cluster_transactions(result)
    return result


def _is_reversal_transaction_label(value: object) -> bool:
    transaction = str(value).strip()
    return (
        transaction.upper() == REVERSAL_TRANSACTION
        or transaction in REVERSAL_CATEGORY_TO_MAIN
    )


def _validate_relabelled_reversal_group(
    rows: pd.DataFrame,
) -> tuple[str | None, list[str], bool]:
    transaction_values = rows['Transaction'].fillna('').astype(str).str.strip()
    known_categories = {
        value for value in transaction_values
        if value in REVERSAL_CATEGORY_TO_MAIN
    }
    implied_main_categories = {
        REVERSAL_CATEGORY_TO_MAIN[value] for value in known_categories
    }
    has_unclassified_rows = transaction_values.str.upper().eq(REVERSAL_TRANSACTION).any()

    if len(implied_main_categories) > 1:
        return None, ['ambiguous reversal remarks matched NGRS and ST'], True

    if not implied_main_categories:
        return None, ['unclassified reversal remarks'], True

    main_transaction = next(iter(implied_main_categories))
    has_main_row = main_transaction in set(transaction_values)
    reasons = []
    exclude_from_summary = False

    if has_unclassified_rows:
        reasons.append('unclassified reversal remarks')
        exclude_from_summary = True

    if not has_main_row:
        reasons.append(REVERSAL_MAIN_MISSING_REASONS[main_transaction])
        exclude_from_summary = True

    reasons.extend(_validate_transaction_group_rules(rows, main_transaction))
    return main_transaction, reasons, exclude_from_summary


def _collect_reversal_unusual_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int], dict[str, int]]:
    """
    Validate already relabeled reversal groups.

    Returns unusual rows, source indices that should be excluded from summary,
    and category counts for rows that remain in summary.
    """
    base_ids = _base_id_from_transaction_id(df['Transaction ID'])
    reversal_mask = df['Transaction'].apply(_is_reversal_transaction_label)
    reversal_indices = set(df[reversal_mask].index)
    invalid_parts = []
    excluded_indices = set()
    categorized_counts: dict[str, int] = {}

    for base_id, group_indices in base_ids.groupby(base_ids, sort=False).groups.items():
        group_reversal_indices = [
            idx for idx in group_indices
            if idx in reversal_indices
        ]
        if not group_reversal_indices:
            continue

        reversal_rows = df.loc[group_reversal_indices]
        _, reasons, exclude_from_summary = _validate_relabelled_reversal_group(
            reversal_rows,
        )

        if reasons:
            unusual_rows = reversal_rows.copy()
            unusual_rows['base_id'] = base_id
            summary_status = (
                'excluded from summary'
                if exclude_from_summary
                else 'included in summary'
            )
            unusual_rows['unusual_reason'] = (
                '; '.join(reasons) + f'; {summary_status}'
            )
            invalid_parts.append(unusual_rows)
            if exclude_from_summary:
                excluded_indices.update(group_reversal_indices)

        if exclude_from_summary:
            continue

        for value, count in reversal_rows['Transaction'].value_counts().items():
            categorized_counts[str(value)] = categorized_counts.get(str(value), 0) + int(count)

    if invalid_parts:
        unusual_df = pd.concat(invalid_parts, ignore_index=True, sort=False)
        unusual_df = unusual_df.sort_values(['base_id', 'No']).reset_index(drop=True)
    else:
        unusual_df = df.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')

    return unusual_df, excluded_indices, categorized_counts


def prepare_reversal_summary_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Relabel REVERSAL rows for summary and return unusual reversal rows.

    Invalid groups with a main NGRS/ST row stay in the summary but are flagged.
    Fee-only, ambiguous, or unclassified groups are flagged and excluded.
    """
    result = relabel_reversal_transactions(df)
    unusual_df, excluded_indices, categorized_counts = (
        _collect_reversal_unusual_transactions(result)
    )

    if excluded_indices:
        summary_ready = result.drop(index=sorted(excluded_indices)).reset_index(drop=True)
    else:
        summary_ready = result.reset_index(drop=True)

    print(f'Reversal rows categorized for summary: {categorized_counts}')
    print(f'Reversal unusual rows flagged: {len(unusual_df)}')
    print(f'Reversal rows excluded from summary: {len(excluded_indices)}')
    return summary_ready, unusual_df


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2d  flag unusual transactions (fee-rule validation)
# ─────────────────────────────────────────────────────────────────────────────

def _flag_fee_rule_unusual_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns all rows (main + fee) belonging to transaction groups that violate
    expected rules defined in TRANSACTION_GROUP_RULES. RECHARGE rows whose
    remarks contain UNUSUAL_RECHARGE_EXEMPT_REMARK are exempt from RECHARGEFEE
    validation.
    Adds 'base_id' and 'unusual_reason' columns to the result.
    """
    df = df.copy()
    df['base_id'] = _base_id_from_transaction_id(df['Transaction ID'])
    unusual_base_ids: dict[str, str] = {}
    for base_id, group in df.groupby('base_id', sort=False):
        transaction = group['Transaction'].fillna('').astype(str)
        main_rows = group[~transaction.str.endswith('FEE')]
        if main_rows.empty:
            continue
        txn_type = main_rows['Transaction'].iloc[0]
        if _is_reversal_transaction_label(txn_type):
            continue
        if txn_type not in TRANSACTION_GROUP_RULES:
            continue
        reasons = _validate_transaction_group_rules(group, txn_type)
        if reasons:
            unusual_base_ids[base_id] = '; '.join(reasons)
    mask = df['base_id'].isin(unusual_base_ids)
    result = df[mask].copy()
    result['unusual_reason'] = result['base_id'].map(unusual_base_ids)
    result = result.sort_values(['base_id', 'No']).reset_index(drop=True)
    print(f'Unusual transaction groups : {result["base_id"].nunique()}')
    print(f'Total rows flagged         : {len(result)}')
    return result


def flag_unusual_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flag all unusual rows after transaction-label preprocessing.

    Duplicate rows are reported as unusual, while fee-rule and reversal
    validation run against the deduplicated view to avoid duplicate-driven false
    positives. Downstream calculation tasks still perform their own dedup step.
    """
    deduplicated_df, duplicate_unusual_df = deduplicate_rows_by_minute_with_report(df)
    fee_unusual_df = _flag_fee_rule_unusual_transactions(deduplicated_df)
    reversal_unusual_df, _, _ = _collect_reversal_unusual_transactions(deduplicated_df)

    unusual_parts = [
        part for part in (fee_unusual_df, duplicate_unusual_df, reversal_unusual_df)
        if not part.empty
    ]
    if unusual_parts:
        result = pd.concat(unusual_parts, ignore_index=True, sort=False)
        sort_cols = [
            col for col in ["Transaction Date", "No", "unusual_reason"]
            if col in result.columns
        ]
        if sort_cols:
            result = result.sort_values(sort_cols).reset_index(drop=True)
        else:
            result = result.reset_index(drop=True)
    else:
        result = fee_unusual_df

    print(f'Combined unusual rows      : {len(result)}')
    print(f'Duplicate unusual rows     : {len(duplicate_unusual_df)}')
    print(f'Fee-rule unusual rows      : {len(fee_unusual_df)}')
    print(f'Reversal unusual rows      : {len(reversal_unusual_df)}')
    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3a  write unusual transactions to a dedicated sheet
# ─────────────────────────────────────────────────────────────────────────────

def append_unusual_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
) -> bool:
    """
    Appends unusual transaction rows to a dedicated sheet using a readable,
    fixed report layout instead of dumping raw DataFrame columns.
    - Writes formatted column headers on the first run.
    - Duplicate guard: skips if the report date already exists.
    Returns True on success, False if skipped (duplicate).
    """
    if unusual_df.empty:
        print('No unusual transactions — nothing to write.')
        return True

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=None):
        ec = ec or len(HEADERS)
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    def _clean(value):
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return ''
        return value

    def _number(value):
        value = _clean(value)
        if value == '':
            return ''
        return int(value)

    def _datetime_display(value):
        value = _clean(value)
        if value == '':
            return ''
        return pd.to_datetime(value).strftime('%d/%m/%Y %H:%M:%S')

    HEADERS = [
        'REPORT DATE',
        'NO',
        'TRANSACTION DATE',
        'TRANSACTION ID',
        'BASE ID',
        'TRANSACTION',
        'KREDIT',
        'DEBET',
        'SALDO AWAL',
        'SALDO AKHIR',
        'NOMOR RS',
        'REMARKS',
        'UNUSUAL REASON',
    ]

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    COL        = {"red": 0.741, "green": 0.843, "blue": 0.933}
    IDR        = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    sh = gspread_client.open(target_spreadsheet)
    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=5000, cols=20)

    report_date = pd.to_datetime(unusual_df['Transaction Date']).dt.strftime('%d/%m/%Y').max()
    existing = ws.get_all_values()
    meaningful_existing = [
        row for row in existing
        if any(str(cell).strip() for cell in row)
    ]

    if meaningful_existing:
        headers = meaningful_existing[0]
        if 'REPORT DATE' in headers:
            report_col = headers.index('REPORT DATE')
            existing_dates = [row[report_col] for row in meaningful_existing[1:] if len(row) > report_col]
            if report_date in existing_dates:
                print(f'⚠️  Unusual transactions for {report_date} already exist — skipping.')
                return False
        elif 'Transaction Date' in headers:
            td_col = headers.index('Transaction Date')
            existing_dates = [row[td_col] for row in meaningful_existing[1:] if len(row) > td_col]
            if any(report_date in d for d in existing_dates):
                print(f'⚠️  Unusual transactions for {report_date} already exist — skipping.')
                return False

    if not meaningful_existing:
        insert_row = 1
    else:
        insert_row = len(existing) + 1

    needs_header = not meaningful_existing or meaningful_existing[0] != HEADERS
    rows_to_append = []

    if needs_header:
        rows_to_append.append(HEADERS)
        data_start = insert_row + 1
    else:
        data_start = insert_row

    for _, row in unusual_df.iterrows():
        rows_to_append.append([
            report_date,
            _number(row.get('No')),
            _datetime_display(row.get('Transaction Date')),
            str(_clean(row.get('Transaction ID'))),
            str(_clean(row.get('base_id'))),
            str(_clean(row.get('Transaction'))),
            _number(row.get('Kredit')),
            _number(row.get('Debet')),
            _number(row.get('Saldo Awal')),
            _number(row.get('Saldo Akhir')),
            str(_clean(row.get('Nomor RS'))),
            str(_clean(row.get('Remarks'))),
            str(_clean(row.get('unusual_reason'))),
        ])

    write_end = insert_row + len(rows_to_append) - 1
    ws.update(_range(insert_row, write_end), rows_to_append, value_input_option='USER_ENTERED')

    header_row = data_start - 1 if needs_header else 1
    data_end = data_start + len(unusual_df) - 1

    widths = {
        0: 110, 1: 70, 2: 165, 3: 220, 4: 220, 5: 170, 6: 120,
        7: 120, 8: 130, 9: 130, 10: 130, 11: 420, 12: 320,
    }
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

    ws.format(_range(header_row, header_row), {
        "backgroundColor": COL_HEADER,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
    })
    ws.format(_range(data_start, data_end), {
        "backgroundColor": COL_WHITE,
        "verticalAlignment": "TOP",
    })
    ws.format(f"A{data_start}:A{data_end}", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}})
    ws.format(f"C{data_start}:C{data_end}", {"numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm:ss"}})
    ws.format(f"G{data_start}:J{data_end}", {"numberFormat": IDR})
    ws.format(f"L{data_start}:M{data_end}", {"wrapStrategy": "WRAP"})
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })

    print(f'✓ Written {len(unusual_df)} unusual rows for {report_date}')
    return True


def process_unusual_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    unusual_df: pd.DataFrame,
    gspread_client,
) -> bool:
    """
    Coordinator for the unusual-transactions upload step.
    Mirrors the interface of process_daily_upload.
    """
    return append_unusual_to_gsheet(
        gspread_client, target_spreadsheet, target_worksheet, unusual_df
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3b  export transaction detail rows to dedicated sheets
# ─────────────────────────────────────────────────────────────────────────────

def extract_disbursement_date_from_remarks(remarks) -> str:
    """
    Extracts the QRIS Duwit disbursement date from remarks text.
    Expected phrase example:
      "... pembayaran QRIS pada tanggal 04-06-2026 sejumlah ..."
    Returns DD/MM/YYYY for Google Sheets readability, or an empty string.
    """
    if remarks is None or (not isinstance(remarks, str) and pd.isna(remarks)):
        return ""

    match = re.search(
        r"\btanggal\s+(\d{2}-\d{2}-\d{4})\b",
        str(remarks),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""

    parsed = pd.to_datetime(match.group(1), format="%d-%m-%Y", errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%d/%m/%Y")


def prepare_transaction_detail_export(
    df: pd.DataFrame,
    transaction: str,
    include_disbursement_date: bool = False,
) -> pd.DataFrame:
    """
    Filters detail rows by Transaction using case-insensitive exact matching.
    Optionally adds Disbursement Date extracted from Remarks for QRISDUWIT.
    """
    if "Transaction" not in df.columns:
        raise KeyError("Transaction column is required for detail export.")

    target = str(transaction).strip().upper()
    normalized = df["Transaction"].fillna("").astype(str).str.strip().str.upper()
    result = df.loc[normalized == target].copy()

    if include_disbursement_date:
        result["Disbursement Date"] = result["Remarks"].apply(
            extract_disbursement_date_from_remarks
        )

    sort_cols = [col for col in ["Transaction Date", "No"] if col in result.columns]
    if sort_cols:
        result = result.sort_values(sort_cols)

    return result.reset_index(drop=True)


def prepare_reversal_detail_export(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filters REVERSAL rows and relabels rows into reversal detail categories
    using the same remark rules as the summary transform.
    """
    if "Transaction" not in df.columns:
        raise KeyError("Transaction column is required for detail export.")

    prepared = relabel_reversal_transactions(df)
    reversal_mask = prepared['Transaction'].apply(_is_reversal_transaction_label)
    result = prepared.loc[reversal_mask].copy()

    if result.empty:
        print('No REVERSAL rows found for detail export.')
        return result

    categorized_counts = {
        str(value): int(count)
        for value, count in result['Transaction'].value_counts().items()
        if value in REVERSAL_CATEGORY_TO_MAIN
    }
    unclassified_rows = int(
        result['Transaction']
        .fillna('')
        .astype(str)
        .str.strip()
        .str.upper()
        .eq(REVERSAL_TRANSACTION)
        .sum()
    )

    sort_cols = [col for col in ["Transaction Date", "No"] if col in result.columns]
    if sort_cols:
        result = result.sort_values(sort_cols)

    print(f'Reversal detail rows categorized: {categorized_counts}')
    print(f'Reversal detail rows left as Reversal: {unclassified_rows}')
    return result.reset_index(drop=True)


def append_transaction_detail_to_gsheet(
    gspread_client,
    target_spreadsheet: str,
    target_worksheet: str,
    detail_df: pd.DataFrame,
    include_disbursement_date: bool = False,
) -> bool:
    """
    Appends filtered transaction detail rows to a dedicated worksheet.
    Returns True on success/no rows, False if skipped by duplicate-date guard.
    """
    if detail_df.empty:
        print(f"No rows for {target_worksheet} — nothing to write.")
        return True

    def _a1(row, col):
        col_letter = ""
        while col:
            col, rem = divmod(col - 1, 26)
            col_letter = chr(65 + rem) + col_letter
        return f"{col_letter}{row}"

    def _range(sr, er, sc=1, ec=None):
        ec = ec or len(HEADERS)
        return f"{_a1(sr, sc)}:{_a1(er, ec)}"

    def _clean(value):
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return ""
        return value

    def _number(value):
        value = _clean(value)
        if value == "":
            return ""
        return int(value)

    def _sheet_text(value):
        value = _clean(value)
        if value == "":
            return ""
        return f"'{str(value)}"

    def _datetime_display(value):
        value = _clean(value)
        if value == "":
            return ""
        return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M:%S")

    def _date_display(value):
        value = _clean(value)
        if value == "":
            return ""
        return pd.to_datetime(value, dayfirst=True).strftime("%d/%m/%Y")

    HEADERS = ["REPORT DATE"]
    if include_disbursement_date:
        HEADERS.append("DISBURSEMENT DATE")
    HEADERS.extend([
        "NO",
        "TRANSACTION DATE",
        "TRANSACTION ID",
        "TRANSACTION TYPE",
        "TRANSACTION",
        "KREDIT",
        "DEBET",
        "SALDO AWAL",
        "SALDO AKHIR",
        "NOMOR RS",
        "REMARKS",
    ])

    COL_HEADER = {"red": 0.122, "green": 0.306, "blue": 0.471}
    COL_WHITE  = {"red": 1,     "green": 1,     "blue": 1}
    IDR        = {"type": "NUMBER", "pattern": "#,##0;(#,##0);-"}

    sh = gspread_client.open(target_spreadsheet)
    try:
        ws = sh.worksheet(target_worksheet)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=target_worksheet, rows=5000, cols=max(20, len(HEADERS)))

    report_date = pd.to_datetime(detail_df["Transaction Date"]).dt.strftime("%d/%m/%Y").max()
    existing = ws.get_all_values()
    meaningful_existing = [
        row for row in existing
        if any(str(cell).strip() for cell in row)
    ]

    if meaningful_existing:
        headers = meaningful_existing[0]
        if "REPORT DATE" in headers:
            report_col = headers.index("REPORT DATE")
            existing_dates = [
                row[report_col] for row in meaningful_existing[1:]
                if len(row) > report_col
            ]
            if report_date in existing_dates:
                print(f"⚠️  {target_worksheet} rows for {report_date} already exist — skipping.")
                return False

    insert_row = 1 if not meaningful_existing else len(existing) + 1
    needs_header = not meaningful_existing or meaningful_existing[0] != HEADERS
    rows_to_append = []

    if needs_header:
        rows_to_append.append(HEADERS)
        data_start = insert_row + 1
    else:
        data_start = insert_row

    for _, row in detail_df.iterrows():
        output_row = [report_date]
        if include_disbursement_date:
            output_row.append(_date_display(row.get("Disbursement Date")))
        output_row.extend([
            _number(row.get("No")),
            _datetime_display(row.get("Transaction Date")),
            str(_clean(row.get("Transaction ID"))),
            str(_clean(row.get("Transaction Type"))),
            str(_clean(row.get("Transaction"))),
            _number(row.get("Kredit")),
            _number(row.get("Debet")),
            _number(row.get("Saldo Awal")),
            _number(row.get("Saldo Akhir")),
            _sheet_text(row.get("Nomor RS")),
            str(_clean(row.get("Remarks"))),
        ])
        rows_to_append.append(output_row)

    write_end = insert_row + len(rows_to_append) - 1
    ws.update(_range(insert_row, write_end), rows_to_append, value_input_option="USER_ENTERED")

    header_row = data_start - 1 if needs_header else 1
    data_end = data_start + len(detail_df) - 1

    widths_by_header = {
        "REPORT DATE": 110,
        "DISBURSEMENT DATE": 145,
        "NO": 70,
        "TRANSACTION DATE": 165,
        "TRANSACTION ID": 220,
        "TRANSACTION TYPE": 150,
        "TRANSACTION": 160,
        "KREDIT": 120,
        "DEBET": 120,
        "SALDO AWAL": 130,
        "SALDO AKHIR": 130,
        "NOMOR RS": 130,
        "REMARKS": 480,
    }
    sh.batch_update({"requests": [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": widths_by_header.get(header, 120)},
                "fields": "pixelSize",
            }
        }
        for i, header in enumerate(HEADERS)
    ]})

    ws.format(_range(header_row, header_row), {
        "backgroundColor": COL_HEADER,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
        "textFormat": {"bold": True, "foregroundColor": COL_WHITE},
    })
    ws.format(_range(data_start, data_end), {
        "backgroundColor": COL_WHITE,
        "verticalAlignment": "TOP",
    })

    for header in ["REPORT DATE", "DISBURSEMENT DATE"]:
        if header in HEADERS:
            col = HEADERS.index(header) + 1
            ws.format(f"{_a1(data_start, col)}:{_a1(data_end, col)}", {
                "numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}
            })

    date_col = HEADERS.index("TRANSACTION DATE") + 1
    ws.format(f"{_a1(data_start, date_col)}:{_a1(data_end, date_col)}", {
        "numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm:ss"}
    })

    for header in ["KREDIT", "DEBET", "SALDO AWAL", "SALDO AKHIR"]:
        col = HEADERS.index(header) + 1
        ws.format(f"{_a1(data_start, col)}:{_a1(data_end, col)}", {"numberFormat": IDR})

    nomor_rs_col = HEADERS.index("NOMOR RS") + 1
    ws.format(f"{_a1(data_start, nomor_rs_col)}:{_a1(data_end, nomor_rs_col)}", {
        "numberFormat": {"type": "TEXT"}
    })

    remarks_col = HEADERS.index("REMARKS") + 1
    ws.format(f"{_a1(data_start, remarks_col)}:{_a1(data_end, remarks_col)}", {
        "wrapStrategy": "WRAP"
    })
    ws.format(_range(data_start, data_start), {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": COL_HEADER}}
    })

    print(f"✓ Written {len(detail_df)} rows to {target_worksheet} for {report_date}")
    return True


def process_transaction_detail_upload(
    target_spreadsheet: str,
    target_worksheet: str,
    detail_df: pd.DataFrame,
    gspread_client,
    include_disbursement_date: bool = False,
) -> bool:
    """
    Coordinator for QRISDUWIT and REVERSAL detail-row upload steps.
    """
    return append_transaction_detail_to_gsheet(
        gspread_client,
        target_spreadsheet,
        target_worksheet,
        detail_df,
        include_disbursement_date=include_disbursement_date,
    )
