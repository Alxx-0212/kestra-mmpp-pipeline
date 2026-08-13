"""Versioned PostgreSQL migrations and schema guards for LinkAja."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIGRATION_TABLE = "linkaja_schema_migrations"
MIGRATION_DIRECTORY = Path(__file__).with_name("migrations")
SCHEMA_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"finance.linkaja.schema").digest()[:8],
    byteorder="big",
    signed=True,
)

LEGACY_TRANSACTION_COLUMNS = {
    "cluster_id",
    "report_date",
    "finalized_at_local",
    "transaction_id",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "counter_party",
    "company_organization",
    "company_account",
    "company_debit",
    "company_credit",
    "signed_amount",
    "company_balance",
    "balance_before",
    "source_fee",
    "expected_fee",
    "transfer_scope",
    "is_reversal",
    "original_transaction_scenario",
    "original_is_resolved",
    "reversal_category",
    "reversal_in_cluster_fee",
    "is_reversed",
    "reversal_count",
    "reversal_transaction_ids",
    "first_reversal_at_local",
    "latest_reversal_at_local",
    "source_files",
    "updated_load_id",
    "updated_at",
}

VERSION_2_TRANSACTION_COLUMNS = {
    "cluster_id",
    "transaction_id",
    "report_date",
    "finalized_at_local",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "counter_party",
    "source_ledger_row_count",
    "ledger_debit_total",
    "ledger_credit_total",
    "source_fee",
    "source_fee_value_count",
    "company_organization",
    "company_account",
    "has_company_purchase_account",
    "company_debit",
    "company_credit",
    "signed_amount",
    "company_balance",
    "balance_before",
    "transfer_scope",
    "is_reversal",
    "original_transaction_scenario",
    "original_is_resolved",
    "reversal_category",
    "reversal_count",
    "is_reversed",
    "reversal_transaction_ids",
    "first_reversal_at_local",
    "latest_reversal_at_local",
    "source_files",
    "updated_load_id",
    "updated_at",
}

VERSION_3_TRANSACTION_COLUMNS = VERSION_2_TRANSACTION_COLUMNS | {
    "reversal_resolution_status",
    "is_unusual",
    "unusual_reason_codes",
}

CURRENT_TRANSACTION_COLUMNS = VERSION_3_TRANSACTION_COLUMNS | {
    "report_month",
    "calculation_cutoff",
    "materialization_id",
    "materialized_at",
}

TRANSACTION_BASE_VIEW_COLUMNS = {
    "cluster_id",
    "transaction_id",
    "report_date",
    "finalized_at_local",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "counter_party",
    "source_ledger_row_count",
    "ledger_debit_total",
    "ledger_credit_total",
    "source_fee",
    "source_fee_value_count",
    "company_organization",
    "company_account",
    "company_debit",
    "company_credit",
    "company_balance",
    "source_files",
    "updated_load_id",
    "updated_at",
}

CURRENT_TRANSACTION_VIEW_COLUMN_ORDER = (
    "cluster_id",
    "transaction_id",
    "report_date",
    "finalized_at_local",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "counter_party",
    "source_ledger_row_count",
    "ledger_debit_total",
    "ledger_credit_total",
    "signed_amount",
    "source_fee",
    "source_fee_value_count",
    "company_organization",
    "company_account",
    "has_company_purchase_account",
    "company_debit",
    "company_credit",
    "company_balance",
    "balance_before",
    "transfer_scope",
    "is_reversal",
    "original_transaction_scenario",
    "original_is_resolved",
    "reversal_category",
    "reversal_count",
    "is_reversed",
    "reversal_transaction_ids",
    "first_reversal_at_local",
    "latest_reversal_at_local",
    "source_files",
    "updated_load_id",
    "updated_at",
    "reversal_resolution_status",
    "is_unusual",
    "unusual_reason_codes",
)

CURRENT_TRANSACTION_VIEW_COLUMNS = set(CURRENT_TRANSACTION_VIEW_COLUMN_ORDER)

MONTHLY_REFRESH_COLUMNS = {
    "materialization_id",
    "cluster_id",
    "report_month",
    "calculation_cutoff",
    "transaction_rows",
    "unresolved_reversal_count",
    "refreshed_at",
}

MONTHLY_FEE_SUMMARY_VIEW_COLUMNS = {
    "report_month",
    "calculation_cutoff",
    "materialization_id",
    "cluster_id",
    "fee_category",
    "gross_transaction_count",
    "reversed_transaction_count",
    "active_transaction_count",
    "gross_fee",
    "reversed_fee",
    "net_fee",
    "monthly_payable_fee",
    "gross_missing_fee_count",
    "reversed_missing_fee_count",
    "active_missing_fee_count",
    "calculation_status",
}

RAW_TRANSACTION_COLUMNS = {
    "cluster_id",
    "source_file",
    "source_row_number",
    "load_id",
    "ingested_at",
    "source_no",
    "top_organization",
    "parent_organization",
    "organization",
    "transaction_id",
    "original_transaction_id",
    "partner_reference_number",
    "invoice_id",
    "finalized_date",
    "finalized_time",
    "initiate_date",
    "initiate_time",
    "transaction_type",
    "transaction_scenario",
    "transaction_status",
    "transaction_statement",
    "account",
    "counter_party",
    "debit",
    "credit",
    "balance",
    "fee",
}

CURRENT_INDEXES = {
    "linkaja_raw_cluster_date_transaction_idx",
    "linkaja_raw_cluster_transaction_idx",
    "linkaja_raw_cluster_original_transaction_idx",
    "linkaja_transactions_cluster_report_date_idx",
    "linkaja_transactions_cluster_scenario_report_date_idx",
    "linkaja_transactions_cluster_original_idx",
    "linkaja_transactions_unusual_cluster_date_idx",
    "linkaja_transactions_cluster_report_month_idx",
    "linkaja_transactions_materialization_idx",
    "linkaja_monthly_refreshes_cluster_month_idx",
}

CURRENT_TRANSACTION_CONSTRAINTS = {
    "linkaja_transactions_pkey",
    "linkaja_transactions_completed_status_check",
    "linkaja_transactions_source_row_count_check",
    "linkaja_transactions_source_fee_check",
    "linkaja_transactions_company_account_check",
    "linkaja_transactions_transfer_scope_check",
    "linkaja_transactions_resolved_original_check",
    "linkaja_transactions_reversal_count_check",
    "linkaja_transactions_reversal_timestamp_check",
    "linkaja_transactions_reversal_resolution_status_check",
    "linkaja_transactions_unusual_consistency_check",
    "linkaja_transactions_report_month_check",
    "linkaja_transactions_calculation_cutoff_check",
}

CURRENT_MONTHLY_REFRESH_CONSTRAINTS = {
    "linkaja_monthly_refreshes_pkey",
    "linkaja_monthly_refreshes_report_month_check",
    "linkaja_monthly_refreshes_counts_check",
}

CREATE_MIGRATION_TABLE_STATEMENT = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True)
class LinkAjaMigration:
    version: int
    name: str
    checksum: str
    sql: str


def _load_migrations() -> list[LinkAjaMigration]:
    migrations: list[LinkAjaMigration] = []
    for path in sorted(MIGRATION_DIRECTORY.glob("*.sql")):
        version_text, separator, name = path.stem.partition("_")
        if not separator or not version_text.isdigit():
            raise ValueError(f"Invalid LinkAja migration filename: {path.name}")
        sql_text = path.read_text(encoding="utf-8")
        migrations.append(
            LinkAjaMigration(
                version=int(version_text),
                name=name,
                checksum=hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
                sql=sql_text,
            )
        )

    versions = [migration.version for migration in migrations]
    if not migrations or versions != list(range(1, len(migrations) + 1)):
        raise ValueError(
            "LinkAja migrations must be a non-empty contiguous sequence starting at 001"
        )
    return migrations


def latest_linkaja_schema_version() -> int:
    return _load_migrations()[-1].version


def _relation_exists(cursor, relation_name: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (relation_name,))
    return cursor.fetchone()[0] is not None


def _table_columns(cursor, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table_name,),
    )
    return {row[0] for row in cursor.fetchall()}


def _table_column_order(cursor, table_name: str) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return tuple(row[0] for row in cursor.fetchall())


def _generated_column_expression(
    cursor,
    table_name: str,
    column_name: str,
) -> str | None:
    cursor.execute(
        """
        SELECT generation_expression
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    row = cursor.fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _signed_amount_is_ledger_based(cursor) -> bool:
    expression = _generated_column_expression(
        cursor,
        "linkaja_transactions",
        "signed_amount",
    )
    if expression is None:
        return False
    normalized = "".join(expression.lower().split()).replace("(", "").replace(
        ")",
        "",
    )
    return normalized == "ledger_debit_total-ledger_credit_total"


def _primary_key_columns(cursor, table_name: str) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT attribute.attname
        FROM pg_index AS index
        JOIN pg_class AS relation
          ON relation.oid = index.indrelid
        JOIN pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN LATERAL UNNEST(index.indkey) WITH ORDINALITY AS key(attnum, ordinality)
          ON TRUE
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attnum = key.attnum
        WHERE namespace.nspname = current_schema()
          AND relation.relname = %s
          AND index.indisprimary
        ORDER BY key.ordinality
        """,
        (table_name,),
    )
    return tuple(row[0] for row in cursor.fetchall())


def _schema_object_names(cursor, object_type: str) -> set[str]:
    if object_type == "index":
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND (
                  tablename = 'linkaja_raw_transactions'
                  OR tablename = 'linkaja_transactions'
                  OR tablename = 'linkaja_monthly_refreshes'
              )
            """
        )
    elif object_type == "constraint":
        cursor.execute(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE constraint_schema = current_schema()
              AND table_name = %s
            """,
            ("linkaja_transactions",),
        )
    else:
        raise ValueError(f"Unsupported schema object type: {object_type}")
    return {str(row[0]) for row in cursor.fetchall()}


def _assert_current_business_schema(
    cursor,
    *,
    require_monthly_fee_summary: bool = True,
    require_ledger_signed_amount: bool = True,
) -> None:
    if not _relation_exists(cursor, "linkaja_raw_transactions"):
        raise RuntimeError("LinkAja raw table is missing")
    if not _relation_exists(cursor, "linkaja_transactions"):
        raise RuntimeError("LinkAja transaction table is missing")
    if not _relation_exists(cursor, "linkaja_monthly_refreshes"):
        raise RuntimeError("LinkAja monthly refresh table is missing")
    if not _relation_exists(cursor, "linkaja_transaction_base_v"):
        raise RuntimeError("LinkAja transaction base view is missing")
    if not _relation_exists(cursor, "linkaja_transactions_current_v"):
        raise RuntimeError("LinkAja current transaction view is missing")
    if (
        require_monthly_fee_summary
        and not _relation_exists(cursor, "linkaja_monthly_fee_summary_v")
    ):
        raise RuntimeError("LinkAja monthly fee summary view is missing")
    if _table_columns(cursor, "linkaja_raw_transactions") != RAW_TRANSACTION_COLUMNS:
        raise RuntimeError("LinkAja raw table does not match the current schema")
    if _table_columns(cursor, "linkaja_transactions") != CURRENT_TRANSACTION_COLUMNS:
        raise RuntimeError("LinkAja transaction table does not match the current schema")
    if (
        _table_columns(cursor, "linkaja_monthly_refreshes")
        != MONTHLY_REFRESH_COLUMNS
    ):
        raise RuntimeError(
            "LinkAja monthly refresh table does not match the current schema"
        )
    if (
        _table_columns(cursor, "linkaja_transaction_base_v")
        != TRANSACTION_BASE_VIEW_COLUMNS
    ):
        raise RuntimeError("LinkAja transaction base view does not match the schema")
    if (
        _table_columns(cursor, "linkaja_transactions_current_v")
        != CURRENT_TRANSACTION_VIEW_COLUMNS
    ):
        raise RuntimeError(
            "LinkAja current transaction view does not match the schema"
        )
    if require_ledger_signed_amount:
        if (
            _table_column_order(cursor, "linkaja_transactions_current_v")
            != CURRENT_TRANSACTION_VIEW_COLUMN_ORDER
        ):
            raise RuntimeError(
                "LinkAja current transaction view has an unexpected column order"
            )
        if not _signed_amount_is_ledger_based(cursor):
            raise RuntimeError(
                "LinkAja signed_amount is not ledger debit minus ledger credit"
            )
    if require_monthly_fee_summary:
        if (
            _table_columns(cursor, "linkaja_monthly_fee_summary_v")
            != MONTHLY_FEE_SUMMARY_VIEW_COLUMNS
        ):
            raise RuntimeError(
                "LinkAja monthly fee summary view does not match the schema"
            )
    if _primary_key_columns(
        cursor, "linkaja_raw_transactions"
    ) != ("load_id", "source_row_number"):
        raise RuntimeError("LinkAja raw table has an unexpected primary key")
    if _primary_key_columns(
        cursor, "linkaja_transactions"
    ) != ("cluster_id", "transaction_id"):
        raise RuntimeError("LinkAja transaction table has an unexpected primary key")

    indexes = _schema_object_names(cursor, "index")
    missing_indexes = sorted(CURRENT_INDEXES - indexes)
    if missing_indexes:
        raise RuntimeError(f"LinkAja schema is missing indexes: {missing_indexes}")

    constraints = _schema_object_names(cursor, "constraint")
    missing_constraints = sorted(CURRENT_TRANSACTION_CONSTRAINTS - constraints)
    if missing_constraints:
        raise RuntimeError(
            f"LinkAja transaction schema is missing constraints: {missing_constraints}"
        )

    cursor.execute(
        """
        SELECT constraint_name
        FROM information_schema.table_constraints
        WHERE constraint_schema = current_schema()
          AND table_name = %s
        """,
        ("linkaja_monthly_refreshes",),
    )
    monthly_constraints = {str(row[0]) for row in cursor.fetchall()}
    missing_monthly_constraints = sorted(
        CURRENT_MONTHLY_REFRESH_CONSTRAINTS - monthly_constraints
    )
    if missing_monthly_constraints:
        raise RuntimeError(
            "LinkAja monthly refresh schema is missing constraints: "
            f"{missing_monthly_constraints}"
        )


def _applied_migrations(cursor) -> dict[int, tuple[str, str]]:
    if not _relation_exists(cursor, MIGRATION_TABLE):
        return {}
    cursor.execute(
        f"SELECT version, name, checksum FROM {MIGRATION_TABLE} ORDER BY version"
    )
    return {int(version): (str(name), str(checksum)) for version, name, checksum in cursor}


def _record_migration(cursor, migration: LinkAjaMigration) -> None:
    cursor.execute(
        f"""
        INSERT INTO {MIGRATION_TABLE} (version, name, checksum)
        VALUES (%s, %s, %s)
        """,
        (migration.version, migration.name, migration.checksum),
    )


def _validate_applied_migrations(
    applied: dict[int, tuple[str, str]],
    migrations: list[LinkAjaMigration],
) -> None:
    expected = {migration.version: migration for migration in migrations}
    unknown = sorted(set(applied) - set(expected))
    if unknown:
        raise RuntimeError(
            f"Database has unknown LinkAja migration version(s): {unknown}"
        )

    for version, (name, checksum) in applied.items():
        migration = expected[version]
        if name != migration.name or checksum != migration.checksum:
            raise RuntimeError(
                f"LinkAja migration {version:03d} differs from the applied checksum"
            )


def _baseline_existing_schema(
    cursor,
    migrations: list[LinkAjaMigration],
) -> list[int]:
    applied = _applied_migrations(cursor)
    if applied:
        return []

    raw_exists = _relation_exists(cursor, "linkaja_raw_transactions")
    transactions_exist = _relation_exists(cursor, "linkaja_transactions")
    if not raw_exists and not transactions_exist:
        return []
    if raw_exists != transactions_exist:
        raise RuntimeError(
            "LinkAja schema drift: raw and transaction tables must either both exist "
            "or both be absent"
        )

    raw_columns = _table_columns(cursor, "linkaja_raw_transactions")
    if raw_columns != RAW_TRANSACTION_COLUMNS:
        raise RuntimeError(
            "LinkAja raw schema drift prevents automatic migration baseline"
        )

    transaction_columns = _table_columns(cursor, "linkaja_transactions")
    primary_key = _primary_key_columns(cursor, "linkaja_transactions")
    if (
        transaction_columns == LEGACY_TRANSACTION_COLUMNS
        and primary_key == ("cluster_id", "report_date", "transaction_id")
    ):
        _record_migration(cursor, migrations[0])
        return [migrations[0].version]

    if (
        transaction_columns == VERSION_2_TRANSACTION_COLUMNS
        and primary_key == ("cluster_id", "transaction_id")
    ):
        for migration in migrations[:2]:
            _record_migration(cursor, migration)
        return [migration.version for migration in migrations[:2]]

    if (
        transaction_columns == VERSION_3_TRANSACTION_COLUMNS
        and primary_key == ("cluster_id", "transaction_id")
    ):
        for migration in migrations[:3]:
            _record_migration(cursor, migration)
        return [migration.version for migration in migrations[:3]]

    if (
        transaction_columns == CURRENT_TRANSACTION_COLUMNS
        and primary_key == ("cluster_id", "transaction_id")
    ):
        has_monthly_fee_summary = _relation_exists(
            cursor,
            "linkaja_monthly_fee_summary_v",
        )
        has_ledger_signed_amount = _signed_amount_is_ledger_based(cursor)
        _assert_current_business_schema(
            cursor,
            require_monthly_fee_summary=has_monthly_fee_summary,
            require_ledger_signed_amount=has_ledger_signed_amount,
        )
        if has_ledger_signed_amount:
            # Version 007 changes fee-view behavior without changing its
            # columns, so a structurally current unversioned schema can only
            # be proven through version 006. Migration 007 must still run.
            baseline_migrations = migrations[:6]
        elif has_monthly_fee_summary:
            baseline_migrations = migrations[:5]
        else:
            baseline_migrations = migrations[:4]
        for migration in baseline_migrations:
            _record_migration(cursor, migration)
        return [migration.version for migration in baseline_migrations]

    raise RuntimeError(
        "LinkAja transaction schema drift prevents automatic migration baseline"
    )


def _advisory_key(value: str) -> int:
    return int.from_bytes(
        hashlib.sha256(value.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


def acquire_linkaja_ingestion_locks(cursor, cluster_id: str) -> None:
    """Block schema migrations and serialize writes for one cluster."""
    cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (SCHEMA_LOCK_KEY,))
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (_advisory_key(f"finance.linkaja.cluster.{cluster_id}"),),
    )


def assert_linkaja_schema_current(cursor) -> None:
    migrations = _load_migrations()
    applied = _applied_migrations(cursor)
    _validate_applied_migrations(applied, migrations)
    expected_versions = {migration.version for migration in migrations}
    if set(applied) != expected_versions:
        current = max(applied, default=0)
        raise RuntimeError(
            "LinkAja database schema is not current: "
            f"found version {current}, expected {migrations[-1].version}. "
            "Run the LinkAja daily or monthly workflow migration step first."
        )
    _assert_current_business_schema(cursor)


def migrate_linkaja_database(dsn: str) -> dict[str, Any]:
    """Apply immutable LinkAja migrations under an exclusive schema lock."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for LinkAja migrations") from exc

    migrations = _load_migrations()
    applied_now: list[int] = []
    baselined: list[int] = []

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
                cursor.execute(CREATE_MIGRATION_TABLE_STATEMENT)
                baselined = _baseline_existing_schema(cursor, migrations)
                applied = _applied_migrations(cursor)
                _validate_applied_migrations(applied, migrations)

        for migration in migrations:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (SCHEMA_LOCK_KEY,),
                    )
                    applied = _applied_migrations(cursor)
                    _validate_applied_migrations(applied, migrations)
                    if migration.version in applied:
                        continue
                    cursor.execute(migration.sql)
                    _record_migration(cursor, migration)
                    applied_now.append(migration.version)

        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
                assert_linkaja_schema_current(cursor)

    return {
        "current_version": migrations[-1].version,
        "baselined_versions": baselined,
        "applied_versions": applied_now,
    }
