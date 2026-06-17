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
# STEP 2a  preprocessing: drop duplicate rows
# ─────────────────────────────────────────────────────────────────────────────

def drop_duplicate_rows_by_minute(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop duplicate rows using all columns as the comparison key, except
    Transaction Date is compared at minute precision only.
    The original Transaction Date value is preserved in the returned rows.
    """
    result = df.copy()
    dedup_key = result.copy()
    dedup_key["Transaction Date"] = (
        pd.to_datetime(dedup_key["Transaction Date"]).dt.floor("min")
    )

    duplicate_mask = dedup_key.duplicated(keep="first")
    dropped_rows = int(duplicate_mask.sum())
    print(f"Duplicate rows dropped using minute-level datetime: {dropped_rows}")

    if dropped_rows == 0:
        return result

    return result.loc[~duplicate_mask].reset_index(drop=True)


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
        ("REVERSAL",                   "Reversal"),
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
    #  +7 RECHARGE OUT CLUSTER FEE    +8 Reversal   +9 SELLTHRU
    #  +10 SELLTHRUFEE  +11 SELLTHRUSALESFEE
    ir = insert_row
    n  = len(KETERANGAN)   # = 10  -> footer rows start at ir + n
    footer_formulas = [
        # NGRS  = net(RECHARGE + RECHARGEFEE + RECHARGE OUT CLUSTER + Reversal)
        f"=C{ir+4}-D{ir+4}+C{ir+5}-D{ir+5}+C{ir+6}-D{ir+6}+C{ir+7}-D{ir+7}+C{ir+8}-D{ir+8}",
        # PPOB  = net(FeeTransaksi)
        f"=C{ir+3}-D{ir+3}",
        # ST    = net(SELLTHRU + SELLTHRUFEE + SELLTHRUSALESFEE)
        f"=C{ir+9}-D{ir+9}+C{ir+10}-D{ir+10}+C{ir+11}-D{ir+11}",
        # DISBURSEMENT
        f"=C{ir+2}-D{ir+2}",
        # QRISDUWIT
        f"=C{ir+1}-D{ir+1}",
    ]
    # Total = sum of the five footer C-cells above
    footer_formulas.append(
        "=" + "+".join(f"C{ir + n + j}" for j in range(len(footer_formulas)))
    )

    FOOTER_LABELS = ["NGRS", "PPOB", "ST", "DISBURSEMENT", "QRISDUWIT", "Total"]
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
# STEP 2b  preprocessing: relabel RECHARGE out-cluster transactions
# ─────────────────────────────────────────────────────────────────────────────

SUMMARY_OUT_CLUSTER_REMARK = 'fee pembelian recharge out cluster'
UNUSUAL_RECHARGE_EXEMPT_REMARK = 'biaya pembelian recharge out cluster'

# Fee validation rules: txn_type -> {fee_type: expected_total_debet, None = presence check only}
FEE_RULES = {
    'RECHARGE':             {'RECHARGEFEE':              20  },
    'SELLTHRU':             {'SELLTHRUFEE': 100, 'SELLTHRUSALESFEE': None},
}


def _remarks_contain(remarks: pd.Series, phrase: str) -> pd.Series:
    normalized = remarks.fillna('').astype(str).str.lower()
    return normalized.str.contains(phrase, regex=False)


def relabel_out_cluster_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Relabels RECHARGE / RECHARGEFEE rows that belong to out-cluster groups:
      RECHARGE    -> RECHARGE OUT CLUSTER
      RECHARGEFEE -> RECHARGE OUT CLUSTER FEE
    Out-cluster groups are identified by the RECHARGE row's Remarks containing
    SUMMARY_OUT_CLUSTER_REMARK (case-insensitive). This relabeling is for the
    summary calculation only.
    """
    df = df.copy()
    df['_base_id'] = df['Transaction ID'].str.replace(r'(SLSFEE|SALESFEE|FEE)$', '', regex=True)
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
# STEP 2c  flag unusual transactions (fee-rule validation)
# ─────────────────────────────────────────────────────────────────────────────

def flag_unusual_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns all rows (main + fee) belonging to transaction groups that violate
    expected fee rules defined in FEE_RULES. RECHARGE rows whose remarks contain
    UNUSUAL_RECHARGE_EXEMPT_REMARK are exempt from RECHARGEFEE validation.
    Adds 'base_id' and 'unusual_reason' columns to the result.
    """
    df = df.copy()
    df['base_id'] = df['Transaction ID'].str.replace(r'(SLSFEE|SALESFEE|FEE)$', '', regex=True)
    unusual_base_ids: dict[str, str] = {}
    for base_id, group in df.groupby('base_id', sort=False):
        main_rows = group[~group['Transaction'].str.endswith('FEE')]
        if main_rows.empty:
            continue
        txn_type = main_rows['Transaction'].iloc[0]
        is_recharge_fee_exempt = (
            txn_type == 'RECHARGE'
            and _remarks_contain(main_rows['Remarks'], UNUSUAL_RECHARGE_EXEMPT_REMARK).any()
        )
        rules = FEE_RULES.get(txn_type)
        if rules is None:
            continue
        reasons = []
        for fee_type, expected_debet in rules.items():
            if is_recharge_fee_exempt and fee_type == 'RECHARGEFEE':
                continue
            fee_rows = group[group['Transaction'] == fee_type]
            if fee_rows.empty:
                reasons.append(f'missing {fee_type}')
            elif expected_debet is not None:
                actual = fee_rows['Debet'].sum()
                if actual != expected_debet:
                    reasons.append(f'{fee_type} Debet={actual} (expected {expected_debet})')
        if reasons:
            unusual_base_ids[base_id] = '; '.join(reasons)
    mask = df['base_id'].isin(unusual_base_ids)
    result = df[mask].copy()
    result['unusual_reason'] = result['base_id'].map(unusual_base_ids)
    result = result.sort_values(['base_id', 'No']).reset_index(drop=True)
    print(f'Unusual transaction groups : {result["base_id"].nunique()}')
    print(f'Total rows flagged         : {len(result)}')
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

    def _range(sr, er, sc=1, ec=14):
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
        'AMOUNT',
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
            _number(row.get('Amount')),
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
        7: 120, 8: 120, 9: 130, 10: 130, 11: 130, 12: 420, 13: 320,
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
    ws.format(f"G{data_start}:K{data_end}", {"numberFormat": IDR})
    ws.format(f"M{data_start}:N{data_end}", {"wrapStrategy": "WRAP"})
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
