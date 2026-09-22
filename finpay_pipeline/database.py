"""Postgres persistence helpers for FinPay workflow dataframes."""
import hashlib
import json
import os
import re
from pathlib import Path

import pandas as pd
import psycopg
from psycopg import sql


def _base_id_from_transaction_id(transaction_id: pd.Series) -> pd.Series:
    return (
        transaction_id.astype(str)
        .str.replace(r'(SLSFEE|SALESFEE|FEE)$', '', regex=True)
    )


FINPAY_DB_TABLES = {
    "finpay_raw_transactions",
    "finpay_transactions",
    "finpay_unusual_transactions",
    "finpay_reversal_transactions",
    "finpay_qrisduwit_transactions",
}

FINPAY_TRANSACTION_MODEL_MIGRATION = (
    Path(__file__).resolve().parent / "migrations" / "001_finpay_transaction_model.sql"
)

FINPAY_DB_CORE_COLUMNS = [
    ("cluster_id", "TEXT NOT NULL"),
    ("report_date", "DATE NOT NULL"),
]

FINPAY_DB_RAW_TRANSACTION_COLUMNS = [
    ("transaction_date", "TIMESTAMP"),
    ("transaction_id", "TEXT"),
    ("base_id", "TEXT"),
    ("transaction_id_type", "TEXT"),
    ("saldo_awal", "NUMERIC(18, 2)"),
    ("kredit", "NUMERIC(18, 2)"),
    ("debet", "NUMERIC(18, 2)"),
    ("saldo_akhir", "NUMERIC(18, 2)"),
    ("transaction_type", "TEXT"),
    ("raw_transaction_label", "TEXT"),
    ("nomor_rs", "TEXT"),
    ("remarks", "TEXT"),
]

FINPAY_DB_RAW_TABLE_SCHEMA = [
    *FINPAY_DB_CORE_COLUMNS,
    *FINPAY_DB_RAW_TRANSACTION_COLUMNS,
]

FINPAY_DB_WORKFLOW_SOURCE_COLUMNS = {
    "finpay_transactions": [
        "Transaction Date",
        "Transaction ID",
        "base_id",
        "transaction_id_type",
        "Saldo Awal",
        "Kredit",
        "Debet",
        "Saldo Akhir",
        "Transaction Type",
        "raw_transaction_label",
        "processed_transaction_label",
        "Nomor RS",
        "Remarks",
    ],
    "finpay_unusual_transactions": [
        "Transaction Date",
        "Transaction ID",
        "base_id",
        "transaction_id_type",
        "Saldo Awal",
        "Kredit",
        "Debet",
        "Saldo Akhir",
        "Transaction Type",
        "raw_transaction_label",
        "processed_transaction_label",
        "Nomor RS",
        "Remarks",
        "unusual_reason",
    ],
    "finpay_reversal_transactions": [
        "Transaction Date",
        "Transaction ID",
        "base_id",
        "transaction_id_type",
        "Saldo Awal",
        "Kredit",
        "Debet",
        "Saldo Akhir",
        "Transaction Type",
        "raw_transaction_label",
        "processed_transaction_label",
        "Nomor RS",
        "Remarks",
    ],
    "finpay_qrisduwit_transactions": [
        "Transaction Date",
        "Transaction ID",
        "base_id",
        "Saldo Awal",
        "Kredit",
        "Debet",
        "Saldo Akhir",
        "Transaction Type",
        "raw_transaction_label",
        "processed_transaction_label",
        "Nomor RS",
        "Remarks",
        "Disbursement Date",
    ],
}

FINPAY_DB_SOURCE_COLUMN_MAP = {
    "transaction_date": "Transaction Date",
    "transaction_id": "Transaction ID",
    "transaction_id_type": "transaction_id_type",
    "saldo_awal": "Saldo Awal",
    "kredit": "Kredit",
    "debet": "Debet",
    "saldo_akhir": "Saldo Akhir",
    "transaction_type": "Transaction Type",
    "nomor_rs": "Nomor RS",
    "remarks": "Remarks",
    "base_id": "base_id",
    "disbursement_date": "Disbursement Date",
    "unusual_reason": "unusual_reason",
}

FINPAY_DB_LABEL_COLUMNS = {
    "raw_transaction_label",
    "processed_transaction_label",
}


def postgres_dsn_from_env(prefix: str = "FINPAY_DB_") -> str:
    host = os.environ.get(f"{prefix}HOST", "localhost")
    port = os.environ.get(f"{prefix}PORT", "5432")
    dbname = os.environ.get(f"{prefix}NAME", "finpay")
    user = os.environ.get(f"{prefix}USER", "finpay")
    password = os.environ.get(f"{prefix}PASSWORD", "")
    return (
        f"host={host} port={port} dbname={dbname} user={user} "
        f"password={password}"
    )


def ensure_finpay_transaction_model(dsn: str) -> None:
    """Apply the additive, versioned transaction-model DDL."""
    migration_sql = FINPAY_TRANSACTION_MODEL_MIGRATION.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(migration_sql)
        conn.commit()


def finpay_source_sha256(source_path: str | os.PathLike[str]) -> str:
    """Return the SHA-256 fingerprint of an uploaded source file."""
    digest = hashlib.sha256()
    with open(source_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_finpay_db_column_name(column: object) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", str(column).strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "column"


def _finpay_db_type_for_column(column_name: str, series: pd.Series | None = None) -> str:
    if column_name == "transaction_date":
        return "TIMESTAMP"
    if column_name == "disbursement_date":
        return "DATE"
    if column_name in {"saldo_awal", "kredit", "debet", "saldo_akhir"}:
        return "NUMERIC(18, 2)"
    if series is not None:
        if pd.api.types.is_datetime64_any_dtype(series):
            return "TIMESTAMP"
        if pd.api.types.is_bool_dtype(series):
            return "BOOLEAN"
        if pd.api.types.is_integer_dtype(series) or pd.api.types.is_float_dtype(series):
            return "NUMERIC(18, 2)"
    return "TEXT"


def _dynamic_finpay_db_schema_from_dataframe(
    df: pd.DataFrame,
    source_columns: list[str],
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """
    Build a SQL-friendly schema from the expected workflow dataframe columns.

    The source `No` column is intentionally excluded because it is not a stable
    transaction identifier. Other columns are retained with normalized names.
    """
    schema = list(FINPAY_DB_CORE_COLUMNS)
    source_by_db_column: dict[str, str] = {}
    used_columns = {column_name for column_name, _ in schema}

    for source_column in source_columns:
        normalized = _normalize_finpay_db_column_name(source_column)
        if normalized == "no":
            continue

        db_column = normalized
        suffix = 2
        while db_column in used_columns:
            db_column = f"{normalized}_{suffix}"
            suffix += 1

        used_columns.add(db_column)
        source_by_db_column[db_column] = source_column
        source_series = df[source_column] if source_column in df.columns else None
        schema.append((db_column, _finpay_db_type_for_column(db_column, source_series)))

    return schema, source_by_db_column


def _finpay_db_schema_for_write(
    table_name: str,
    df: pd.DataFrame,
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    if table_name == "finpay_raw_transactions":
        return FINPAY_DB_RAW_TABLE_SCHEMA, {}
    if table_name not in FINPAY_DB_WORKFLOW_SOURCE_COLUMNS:
        raise ValueError(f"Unsupported FinPay DB table: {table_name}")
    return _dynamic_finpay_db_schema_from_dataframe(
        df,
        FINPAY_DB_WORKFLOW_SOURCE_COLUMNS[table_name],
    )


def _finpay_db_column_names(table_name: str, df: pd.DataFrame | None = None) -> list[str]:
    if df is None:
        if table_name == "finpay_raw_transactions":
            return [column_name for column_name, _ in FINPAY_DB_RAW_TABLE_SCHEMA]
        if table_name not in FINPAY_DB_WORKFLOW_SOURCE_COLUMNS:
            raise ValueError(f"Unsupported FinPay DB table: {table_name}")
        schema, _ = _dynamic_finpay_db_schema_from_dataframe(
            pd.DataFrame(),
            FINPAY_DB_WORKFLOW_SOURCE_COLUMNS[table_name],
        )
        return [column_name for column_name, _ in schema]
    schema, _ = _finpay_db_schema_for_write(table_name, df)
    return [column_name for column_name, _ in schema]


def finpay_db_schema_definition(table_name: str) -> list[tuple[str, str]]:
    """Return the expected database columns and SQL types for a FinPay table."""
    if table_name == "finpay_raw_transactions":
        return list(FINPAY_DB_RAW_TABLE_SCHEMA)
    if table_name not in FINPAY_DB_WORKFLOW_SOURCE_COLUMNS:
        raise ValueError(f"Unsupported FinPay DB table: {table_name}")
    schema, _ = _dynamic_finpay_db_schema_from_dataframe(
        pd.DataFrame(),
        FINPAY_DB_WORKFLOW_SOURCE_COLUMNS[table_name],
    )
    return schema


def _finpay_db_create_table_request(table_name: str, schema: list[tuple[str, str]]):
    column_defs = sql.SQL(",\n            ").join(
        sql.SQL("{} {}").format(sql.Identifier(column_name), sql.SQL(column_type))
        for column_name, column_type in schema
    )

    return sql.SQL("""
        CREATE TABLE IF NOT EXISTS {table_name} (
            {column_defs}
        )
    """).format(
        table_name=sql.Identifier(table_name),
        column_defs=column_defs,
    )


def _reconcile_finpay_db_table_schema(
    cur,
    table_name: str,
    schema: list[tuple[str, str]],
) -> None:
    """
    Keep pipeline-owned FinPay tables aligned with the current write schema.

    Older versions created a generic wide shape with id/no/created_at. The DB is
    a pipeline-owned reporting store, so obsolete pipeline columns are removed
    while existing rows in still-required columns are preserved.
    """
    expected_columns = dict(schema)
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table_name,),
    )
    existing_columns = {row[0] for row in cur.fetchall()}

    for column_name, column_type in schema:
        if column_name in existing_columns:
            continue
        nullable_column_type = column_type.replace(" NOT NULL", "")
        cur.execute(
            sql.SQL("ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            .format(
                table_name=sql.Identifier(table_name),
                column_name=sql.Identifier(column_name),
                column_type=sql.SQL(nullable_column_type),
            )
        )

    for column_name in sorted(existing_columns - set(expected_columns)):
        cur.execute(
            sql.SQL("ALTER TABLE {table_name} DROP COLUMN IF EXISTS {column_name}")
            .format(
                table_name=sql.Identifier(table_name),
                column_name=sql.Identifier(column_name),
            )
        )


def _finpay_db_index_request(table_name: str):

    index_name = f"{table_name}_cluster_report_date_idx"
    return sql.SQL(
        "CREATE INDEX IF NOT EXISTS {index_name} "
        "ON {table_name} (cluster_id, report_date)"
    ).format(
        index_name=sql.Identifier(index_name),
        table_name=sql.Identifier(table_name),
    )


def _db_value(value, column: str):
    if pd.isna(value):
        return None
    if column == "transaction_date":
        return pd.to_datetime(value).to_pydatetime()
    if column == "disbursement_date":
        parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    if column in {"saldo_awal", "kredit", "debet", "saldo_akhir"}:
        return float(value)
    if column in FINPAY_DB_LABEL_COLUMNS:
        return str(value).lower()
    return str(value)


def _transaction_id_type_from_transaction_id(transaction_id: pd.Series) -> pd.Series:
    transaction_text = transaction_id.astype(str)
    suffix = transaction_text.str.extract(r"(SLSFEE|SALESFEE|FEE)$", expand=False)
    return suffix.fillna("MAIN")


def _db_enriched_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    if "Transaction ID" in result.columns:
        if "base_id" not in result.columns:
            result["base_id"] = _base_id_from_transaction_id(result["Transaction ID"])
        if "transaction_id_type" not in result.columns:
            result["transaction_id_type"] = _transaction_id_type_from_transaction_id(
                result["Transaction ID"]
            )
    if "Transaction" in result.columns and "processed_transaction_label" not in result.columns:
        result["processed_transaction_label"] = result["Transaction"]
    return result


def _db_raw_source_value(row: pd.Series, db_column: str):
    if db_column == "raw_transaction_label":
        return row["Transaction"] if "Transaction" in row.index else None
    source_column = FINPAY_DB_SOURCE_COLUMN_MAP.get(db_column)
    if source_column and source_column in row.index:
        return row[source_column]
    return None


def _db_source_value(
    row: pd.Series,
    table_name: str,
    db_column: str,
    source_by_db_column: dict[str, str],
):
    if table_name == "finpay_raw_transactions":
        return _db_raw_source_value(row, db_column)
    source_column = source_by_db_column.get(db_column)
    if source_column and source_column in row.index:
        return row[source_column]
    return None


def _db_insert_rows(
    df: pd.DataFrame,
    table_name: str,
    cluster_id: str,
    report_date_value,
    insert_columns: list[str],
    source_by_db_column: dict[str, str],
) -> list[tuple]:
    rows = []
    for _, row in df.iterrows():
        db_row = {
            "cluster_id": cluster_id,
            "report_date": report_date_value,
        }
        for db_col in insert_columns:
            if db_col in db_row:
                continue
            value = _db_source_value(
                row,
                table_name,
                db_col,
                source_by_db_column,
            )
            db_row[db_col] = _db_value(value, db_col)
        rows.append(tuple(db_row.get(col) for col in insert_columns))
    return rows


def _finpay_source_row_hash(row: pd.Series) -> str:
    payload = {}
    for column, value in row.items():
        if pd.isna(value):
            payload[str(column)] = None
        elif isinstance(value, pd.Timestamp):
            payload[str(column)] = value.isoformat()
        else:
            payload[str(column)] = str(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _append_finpay_source_generation(
    cur,
    df: pd.DataFrame,
    *,
    load_id: str,
    cluster_id: str,
    report_date_value,
    source_file: str,
    source_sha256: str,
) -> None:
    """Record one source generation and its append-only ledger rows."""
    transaction_dates = pd.to_datetime(
        df.get("Transaction Date", pd.Series(dtype="datetime64[ns]")),
        errors="coerce",
    ).dropna()
    source_start_date = transaction_dates.min().date() if not transaction_dates.empty else None
    source_end_date = transaction_dates.max().date() if not transaction_dates.empty else None

    cur.execute(
        """
        SELECT load_id
        FROM finpay_source_loads
        WHERE cluster_id = %s
          AND report_date = %s
          AND is_current
          AND load_id <> %s
        ORDER BY loaded_at DESC, load_id DESC
        LIMIT 1
        """,
        (cluster_id, report_date_value, load_id),
    )
    previous = cur.fetchone()
    supersedes_load_id = previous[0] if previous else None
    cur.execute(
        """
        UPDATE finpay_source_loads
        SET is_current = FALSE
        WHERE cluster_id = %s
          AND report_date = %s
          AND load_id <> %s
        """,
        (cluster_id, report_date_value, load_id),
    )
    cur.execute(
        """
        INSERT INTO finpay_source_loads (
            load_id, cluster_id, source_file, source_sha256, report_date,
            source_start_date, source_end_date, row_count, is_current,
            supersedes_load_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
        ON CONFLICT (load_id) DO UPDATE SET
            cluster_id = EXCLUDED.cluster_id,
            source_file = EXCLUDED.source_file,
            source_sha256 = EXCLUDED.source_sha256,
            report_date = EXCLUDED.report_date,
            source_start_date = EXCLUDED.source_start_date,
            source_end_date = EXCLUDED.source_end_date,
            row_count = EXCLUDED.row_count,
            loaded_at = CURRENT_TIMESTAMP,
            is_current = TRUE,
            supersedes_load_id = EXCLUDED.supersedes_load_id
        """,
        (
            load_id,
            cluster_id,
            source_file,
            source_sha256,
            report_date_value,
            source_start_date,
            source_end_date,
            len(df),
            supersedes_load_id,
        ),
    )
    cur.execute("DELETE FROM finpay_ledger_events WHERE load_id = %s", (load_id,))

    rows = []
    enriched = _db_enriched_dataframe(df).reset_index(drop=True)
    for row_number, (_, row) in enumerate(enriched.iterrows(), start=1):
        transaction_date = pd.to_datetime(row.get("Transaction Date"), errors="coerce")
        if pd.isna(transaction_date):
            continue
        transaction_id = str(row.get("Transaction ID", "")).strip()
        if not transaction_id:
            continue
        rows.append((
            load_id,
            row_number,
            _finpay_source_row_hash(row),
            cluster_id,
            report_date_value,
            transaction_date.to_pydatetime(),
            transaction_id,
            str(row.get("base_id", "")).strip() or None,
            str(row.get("transaction_id_type", "MAIN")).strip() or "MAIN",
            str(row.get("Transaction", "")).strip() or None,
            str(row.get("Transaction Type", "")).strip() or None,
            str(row.get("Remarks", "")).strip() or None,
            float(row.get("Kredit", 0) or 0),
            float(row.get("Debet", 0) or 0),
            None if pd.isna(row.get("Saldo Awal")) else float(row.get("Saldo Awal")),
            None if pd.isna(row.get("Saldo Akhir")) else float(row.get("Saldo Akhir")),
            None if pd.isna(row.get("Nomor RS")) else str(row.get("Nomor RS")),
        ))
    if rows:
        cur.executemany(
            """
            INSERT INTO finpay_ledger_events (
                load_id, source_row_number, source_row_hash, cluster_id,
                report_date, transaction_date, transaction_id, base_id,
                transaction_id_type, raw_transaction_label, transaction_type,
                remarks, kredit, debet, saldo_awal, saldo_akhir, nomor_rs
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            ON CONFLICT (load_id, source_row_number) DO UPDATE SET
                source_row_hash = EXCLUDED.source_row_hash,
                transaction_date = EXCLUDED.transaction_date,
                transaction_id = EXCLUDED.transaction_id,
                base_id = EXCLUDED.base_id,
                transaction_id_type = EXCLUDED.transaction_id_type,
                raw_transaction_label = EXCLUDED.raw_transaction_label,
                transaction_type = EXCLUDED.transaction_type,
                remarks = EXCLUDED.remarks,
                kredit = EXCLUDED.kredit,
                debet = EXCLUDED.debet,
                saldo_awal = EXCLUDED.saldo_awal,
                saldo_akhir = EXCLUDED.saldo_akhir,
                nomor_rs = EXCLUDED.nomor_rs
            """,
            rows,
        )


def write_finpay_dataframe_to_postgres(
    df: pd.DataFrame,
    table_name: str,
    cluster_id: str,
    report_date: str,
    dsn: str,
    *,
    load_id: str | None = None,
    source_file: str | None = None,
    source_sha256: str | None = None,
) -> int:
    """
    Replace one cluster/date batch in a FinPay table with the supplied DataFrame.

    The delete-and-insert strategy makes workflow reruns idempotent for the same
    cluster_id + report_date. The caller decides whether the dataframe is raw,
    deduplicated, unusual-only, or summary-ready.
    """
    if table_name not in FINPAY_DB_TABLES:
        raise ValueError(f"Unsupported FinPay DB table: {table_name}")

    report_date_value = pd.to_datetime(report_date).date()
    db_df = _db_enriched_dataframe(df)
    schema, source_by_db_column = _finpay_db_schema_for_write(table_name, db_df)
    insert_columns = [column_name for column_name, _ in schema]
    rows = _db_insert_rows(
        db_df,
        table_name,
        cluster_id,
        report_date_value,
        insert_columns,
        source_by_db_column,
    )

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            if table_name == "finpay_raw_transactions" and load_id:
                migration_sql = FINPAY_TRANSACTION_MODEL_MIGRATION.read_text(
                    encoding="utf-8"
                )
                cur.execute(migration_sql)
            cur.execute(_finpay_db_create_table_request(table_name, schema))
            _reconcile_finpay_db_table_schema(cur, table_name, schema)
            cur.execute(_finpay_db_index_request(table_name))
            cur.execute(
                sql.SQL(
                    "DELETE FROM {table_name} "
                    "WHERE cluster_id = %s AND report_date = %s"
                ).format(table_name=sql.Identifier(table_name)),
                (cluster_id, report_date_value),
            )

            if rows:
                placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns)
                cur.executemany(
                    sql.SQL("INSERT INTO {table_name} ({columns}) VALUES ({values})")
                    .format(
                        table_name=sql.Identifier(table_name),
                        columns=sql.SQL(", ").join(
                            sql.Identifier(col) for col in insert_columns
                        ),
                        values=placeholders,
                    ),
                    rows,
                )
            if table_name == "finpay_raw_transactions" and load_id:
                _append_finpay_source_generation(
                    cur,
                    df,
                    load_id=load_id,
                    cluster_id=cluster_id,
                    report_date_value=report_date_value,
                    source_file=source_file or "unknown",
                    source_sha256=source_sha256 or hashlib.sha256(
                        repr(df.to_dict(orient="records")).encode()
                    ).hexdigest(),
                )
        conn.commit()

    return len(rows)
