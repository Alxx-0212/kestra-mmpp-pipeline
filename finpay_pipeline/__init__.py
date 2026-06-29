"""Public FinPay pipeline API used by Kestra tasks.

The Kestra workflow imports these names via ``from pipeline import ...``.
This package keeps the implementation split by workflow concern while exposing
the same public function names.
"""
from .loading import load_file, load_and_validate_schema
from .integrity import validate_debit_credit_integrity
from .database import (
    finpay_db_schema_definition,
    postgres_dsn_from_env,
    write_finpay_dataframe_to_postgres,
)
from .classification import (
    flag_unusual_transactions,
    prepare_reversal_summary_transactions,
    preprocess_transaction_labels,
    relabel_out_cluster_transactions,
    relabel_reversal_transactions,
)
from .dedup import (
    deduplicate_rows_by_minute_with_report,
    drop_duplicate_rows_by_minute,
)
from .summary import summarize_by_transaction
from .sheets_common import make_gspread_client
from .summary_sheets import (
    append_daily_to_gsheet,
    process_daily_upload,
    setup_initial_headers_and_saldo,
)
from .unusual_sheets import append_unusual_to_gsheet, process_unusual_upload
from .detail_exports import (
    append_transaction_detail_to_gsheet,
    extract_disbursement_date_from_remarks,
    prepare_reversal_detail_export,
    prepare_transaction_detail_export,
    process_transaction_detail_upload,
)

__all__ = [
    "append_daily_to_gsheet",
    "append_transaction_detail_to_gsheet",
    "append_unusual_to_gsheet",
    "deduplicate_rows_by_minute_with_report",
    "drop_duplicate_rows_by_minute",
    "extract_disbursement_date_from_remarks",
    "finpay_db_schema_definition",
    "flag_unusual_transactions",
    "load_and_validate_schema",
    "load_file",
    "make_gspread_client",
    "postgres_dsn_from_env",
    "prepare_reversal_detail_export",
    "prepare_reversal_summary_transactions",
    "prepare_transaction_detail_export",
    "preprocess_transaction_labels",
    "process_daily_upload",
    "process_transaction_detail_upload",
    "process_unusual_upload",
    "relabel_out_cluster_transactions",
    "relabel_reversal_transactions",
    "setup_initial_headers_and_saldo",
    "summarize_by_transaction",
    "validate_debit_credit_integrity",
    "write_finpay_dataframe_to_postgres",
]
