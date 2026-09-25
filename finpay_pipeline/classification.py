"""Transaction relabeling, fee validation, and unusual-row classification."""
from datetime import date
import re

import pandas as pd

from .dedup import deduplicate_rows_by_minute_with_report

SUMMARY_OUT_CLUSTER_REMARK = 'fee pembelian recharge out cluster'
UNUSUAL_RECHARGE_EXEMPT_REMARK = 'biaya pembelian recharge out cluster'
PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY = 'PEMBELIAN RECHARGE OUT CLUSTER'

REVERSAL_TRANSACTION = 'REVERSAL'
REVERSAL_NGRS_CATEGORY = 'Reversal - NGRS'
REVERSAL_NGRS_FEE_CATEGORY = 'Reversal - NGRS FEE'
REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY = (
    'Reversal - PEMBELIAN RECHARGE OUT CLUSTER'
)
REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY = 'Reversal - Recharge Out Cluster'
REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY = 'Reversal - Recharge Out Cluster FEE'
REVERSAL_ST_CATEGORY = 'Reversal - ST'
REVERSAL_ST_SELLTHRU_FEE_CATEGORY = 'Reversal - ST SELLTHRUFEE'
REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY = 'Reversal - ST SELLTHRUSALESFEE'
REVERSAL_NGRS_MAIN_REMARK = 'biaya pembelian recharge'
REVERSAL_RECHARGE_OUT_CLUSTER_MAIN_REMARK = SUMMARY_OUT_CLUSTER_REMARK
REVERSAL_NGRS_PLATFORM_FEE_REMARK = 'platform fee recharge rp. 20,-'
REVERSAL_ST_MAIN_REMARK = 'sellthru sales fee'
REVERSAL_ST_PLATFORM_FEE_REMARK = 'platform fee sellthru rp. 100,-'
REVERSAL_ST_TRANSACTION_FEE_REMARK = 'fee transaksi sellthru sejumlah 100 rupiah'
REVERSAL_ST_SALES_HOLD_REMARK = 'sales hold transaksi sellthru'
REVERSAL_ST_UNSUPPORTED_REASON = 'unsupported reversal ST category'
PROCESSED_TRANSACTION_LABEL_COLUMN = 'processed_transaction_label'
STANDALONE_SELLTHRU_REMARK = 'transaksi sellthru'
ST_RULE_CUTOFF_DATE = date(2026, 9, 1)
ST_RULE_LEGACY = 'legacy'
ST_RULE_CURRENT = 'current'

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
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: {
        'required': {
            REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY: {
                'column': 'Kredit',
                'equals': 20,
                'missing_reason': (
                    'missing reversal recharge out-cluster platform fee remark: '
                    f'{REVERSAL_NGRS_PLATFORM_FEE_REMARK}'
                ),
                'amount_label': 'reversal recharge out-cluster platform fee',
            },
        },
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

FEE_TRANSACTION_TO_MAIN = {
    'RECHARGEFEE': 'RECHARGE',
    'RECHARGE OUT CLUSTER FEE': 'RECHARGE OUT CLUSTER',
    'SELLTHRUFEE': 'SELLTHRU',
    'SELLTHRUSALESFEE': 'SELLTHRU',
}
FEE_CAP_RULES = {
    'RECHARGEFEE': {
        'column': 'Debet',
        'limit': 20,
        'label': 'Recharge fee',
    },
    'RECHARGE OUT CLUSTER FEE': {
        'column': 'Debet',
        'limit': 20,
        'label': 'Recharge out-cluster fee',
    },
    'SELLTHRUFEE': {
        'column': 'Debet',
        'limit': 100,
        'label': 'Sellthru fee',
    },
    REVERSAL_NGRS_FEE_CATEGORY: {
        'column': 'Kredit',
        'limit': 20,
        'label': 'Reversal NGRS fee',
    },
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY: {
        'column': 'Kredit',
        'limit': 20,
        'label': 'Reversal recharge out-cluster fee',
    },
}

REVERSAL_CATEGORY_TO_MAIN = {
    REVERSAL_NGRS_CATEGORY: REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY: REVERSAL_NGRS_CATEGORY,
    REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY: (
        REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY
    ),
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY: REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_ST_CATEGORY: REVERSAL_ST_CATEGORY,
    REVERSAL_ST_SELLTHRU_FEE_CATEGORY: REVERSAL_ST_CATEGORY,
    REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY: REVERSAL_ST_CATEGORY,
}
REVERSAL_ST_UNSUPPORTED_CATEGORIES = {
    REVERSAL_ST_CATEGORY,
    REVERSAL_ST_SELLTHRU_FEE_CATEGORY,
    REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY,
}
REVERSAL_MAIN_MISSING_REASONS = {
    REVERSAL_NGRS_CATEGORY: f'missing reversal remark: {REVERSAL_NGRS_MAIN_REMARK}',
    REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY: (
        f'missing reversal remark: {UNUSUAL_RECHARGE_EXEMPT_REMARK}'
    ),
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: (
        f'missing reversal remark: {REVERSAL_RECHARGE_OUT_CLUSTER_MAIN_REMARK}'
    ),
    REVERSAL_ST_CATEGORY: f'missing reversal remark: {REVERSAL_ST_MAIN_REMARK}',
}
KNOWN_SUMMARY_TRANSACTION_LABELS = {
    'CASHOUT APOLLO',
    'QRISDUWIT',
    'DISBURSEMENT',
    'FEETRANSAKSI',
    'RECHARGE',
    'RECHARGEFEE',
    PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
    'RECHARGE OUT CLUSTER',
    'RECHARGE OUT CLUSTER FEE',
    REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY,
    REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY,
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY,
    'SELLTHRU',
    'SELLTHRUFEE',
    'SELLTHRUSALESFEE',
}
UNKNOWN_TRANSACTION_UNUSUAL_REASON_PREFIX = 'unknown transaction label'
UNKNOWN_REMARK_UNUSUAL_REASON_PREFIX = 'unknown remarks pattern for transaction'

RECHARGE_REMARK_PATTERNS = frozenset({
    'biaya pembelian recharge sejumlah <value> rupiah, dari <value> ke <value>',
})
RECHARGE_OUT_CLUSTER_REMARK_PATTERNS = frozenset({
    'fee pembelian recharge out cluster sejumlah <value> rupiah, '
    'dari <value> ke <value>',
})
PEMBELIAN_RECHARGE_OUT_CLUSTER_REMARK_PATTERNS = frozenset({
    'biaya pembelian recharge out cluster sejumlah <value> rupiah, '
    'dari <value> ke <value>',
})
RECHARGE_FEE_REMARK_PATTERNS = frozenset({
    'platform fee recharge rp. <value>,-',
})
SELLTHRU_MAIN_REMARK_PATTERNS = frozenset({
    'sellthru sales fee',
    STANDALONE_SELLTHRU_REMARK,
})
SELLTHRU_FEE_REMARK_PATTERNS = frozenset({
    'platform fee sellthru rp. <value>,-',
    'fee transaksi sellthru sejumlah <value> rupiah, dari <value> ke <value>, '
    'fee tsel rp.<value> | fee finnet rp.<value>',
})
SELLTHRU_SALES_FEE_REMARK_PATTERNS = frozenset({
    'sales hold transaksi sellthru sejumlah <value> rupiah, dari <value>',
})

# This allowlist was characterized from all FinPay exports currently available
# under data/411311. Numeric values and DD-MM-YYYY dates are normalized before
# comparison, while the source Remarks value remains unchanged in every output.
KNOWN_TRANSACTION_REMARK_PATTERNS = {
    'CASHOUT APOLLO': frozenset({'apollo mrc<value> <date>'}),
    'QRISDUWIT': frozenset({
        'disburse qris duwit atas transaksi pembayaran qris pada tanggal '
        '<date> sejumlah rp <value>',
    }),
    'DISBURSEMENT': frozenset({
        'sales fee payment untuk perdana sebanyak <value> sejumlah <value> rupiah',
        'sales fee payment untuk voucher sebanyak <value> sejumlah <value> rupiah',
        'sales fee payment untuk voucher sebanyak <value> dan perdana sebanyak '
        '<value> sejumlah <value> rupiah',
        'sales fee payment untuk perdana sebanyak <value> dan voucher sebanyak '
        '<value> sejumlah <value> rupiah',
    }),
    'FEETRANSAKSI': frozenset({
        'fee digipos rp.<value> | fee sbp rp.<value> | dari nomor :<value>',
    }),
    'RECHARGE': RECHARGE_REMARK_PATTERNS,
    'RECHARGEFEE': RECHARGE_FEE_REMARK_PATTERNS,
    PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY: (
        PEMBELIAN_RECHARGE_OUT_CLUSTER_REMARK_PATTERNS
    ),
    'RECHARGE OUT CLUSTER': RECHARGE_OUT_CLUSTER_REMARK_PATTERNS,
    'RECHARGE OUT CLUSTER FEE': RECHARGE_FEE_REMARK_PATTERNS,
    REVERSAL_NGRS_CATEGORY: RECHARGE_REMARK_PATTERNS,
    REVERSAL_NGRS_FEE_CATEGORY: RECHARGE_FEE_REMARK_PATTERNS,
    REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY: (
        PEMBELIAN_RECHARGE_OUT_CLUSTER_REMARK_PATTERNS
    ),
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: RECHARGE_OUT_CLUSTER_REMARK_PATTERNS,
    REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY: RECHARGE_FEE_REMARK_PATTERNS,
    'SELLTHRU': SELLTHRU_MAIN_REMARK_PATTERNS,
    'SELLTHRUFEE': SELLTHRU_FEE_REMARK_PATTERNS,
    'SELLTHRUSALESFEE': SELLTHRU_SALES_FEE_REMARK_PATTERNS,
    REVERSAL_ST_CATEGORY: frozenset({'sellthru sales fee'}),
    REVERSAL_ST_SELLTHRU_FEE_CATEGORY: SELLTHRU_FEE_REMARK_PATTERNS,
    REVERSAL_ST_SELLTHRU_SALES_FEE_CATEGORY: SELLTHRU_SALES_FEE_REMARK_PATTERNS,
}


def _normalize_remark_pattern(value: object) -> str:
    """Remove changing date/number values while preserving remark structure."""
    if pd.isna(value):
        return ''
    normalized = str(value).strip().casefold()
    normalized = re.sub(r'\b\d{1,2}-\d{1,2}-\d{4}\b', '<date>', normalized)
    normalized = re.sub(r'\d+(?:[.,]\d+)*', '<value>', normalized)
    return ' '.join(normalized.split())


def _remarks_contain(remarks: pd.Series, phrase: str) -> pd.Series:
    normalized = remarks.fillna('').astype(str).str.lower()
    return normalized.str.contains(phrase, regex=False)


def _base_id_from_transaction_id(transaction_id: pd.Series) -> pd.Series:
    return (
        transaction_id.astype(str)
        .str.replace(r'(SLSFEE|SALESFEE|FEE)$', '', regex=True)
    )


def _st_rule_era_for_group(group: pd.DataFrame) -> tuple[str | None, str | None]:
    """Return the ST policy era for one transaction family.

    Transaction timestamps are the source of truth. A family that crosses the
    cutover cannot be assigned one deterministic fee policy, so it is rejected
    rather than silently applying one side of the boundary to all rows.
    """
    if 'Transaction Date' not in group.columns:
        return None, 'missing Transaction Date for ST rule classification'

    parsed = pd.to_datetime(group['Transaction Date'], errors='coerce')
    if parsed.isna().any():
        return None, 'invalid Transaction Date for ST rule classification'

    eras = set((value.date() >= ST_RULE_CUTOFF_DATE) for value in parsed)
    if len(eras) != 1:
        return None, 'ST transaction family crosses the 2026-09-01 rule cutoff'
    return (
        ST_RULE_CURRENT if True in eras else ST_RULE_LEGACY,
        None,
    )


def _is_standalone_sellthru_source_row(row: pd.Series) -> bool:
    """Identify the exact raw RECHARGE exception, not a processed ST row."""
    raw_label = str(row.get('raw_transaction_label', '')).strip().upper()
    return (
        raw_label == 'RECHARGE'
        and _normalize_remark_pattern(row.get('Remarks')) == STANDALONE_SELLTHRU_REMARK
    )


def _st_fee_validation_reasons(
    group: pd.DataFrame,
    *,
    require_sales_fee: bool,
) -> list[str]:
    reasons = []
    fee_rows = group[group['Transaction'] == 'SELLTHRUFEE']
    if fee_rows.empty:
        reasons.append('missing SELLTHRUFEE')
    else:
        actual = _numeric_sum(fee_rows, 'Debet')
        if actual != 100:
            reasons.append(f'SELLTHRUFEE Debet={actual} (expected 100)')

    if require_sales_fee and group[group['Transaction'] == 'SELLTHRUSALESFEE'].empty:
        reasons.append('missing SELLTHRUSALESFEE')
    return reasons


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


def relabel_pembelian_recharge_out_cluster_transactions(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Relabel RECHARGE rows whose Remarks contain the separate pembelian
    out-cluster phrase so they are summarized outside regular NGRS.
    """
    result = df.copy()
    pembelian_mask = (
        (result['Transaction'] == 'RECHARGE')
        & _remarks_contain(result['Remarks'], UNUSUAL_RECHARGE_EXEMPT_REMARK)
    )
    result.loc[pembelian_mask, 'Transaction'] = (
        PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY
    )
    print(f'Pembelian recharge out-cluster rows relabeled: {int(pembelian_mask.sum())}')
    return result


def relabel_standalone_sellthru_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Relabel the FinPay standalone ST case currently emitted as RECHARGE."""
    result = df.copy()
    transaction = result['Transaction'].fillna('').astype(str).str.strip().str.upper()
    normalized_remarks = result['Remarks'].map(_normalize_remark_pattern)
    standalone_sellthru_mask = (
        transaction.eq('RECHARGE')
        & normalized_remarks.eq(STANDALONE_SELLTHRU_REMARK)
    )
    result.loc[standalone_sellthru_mask, 'Transaction'] = 'SELLTHRU'
    print(
        'Standalone Sellthru rows relabeled: '
        f'{int(standalone_sellthru_mask.sum())}'
    )
    return result


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


def _numeric_value(value) -> int | float:
    parsed = pd.to_numeric(pd.Series([value]), errors='coerce').fillna(0).iloc[0]
    if float(parsed).is_integer():
        return int(parsed)
    return float(parsed)


def _format_expected_text(rule: dict) -> str:
    if 'expected_text' in rule:
        return str(rule['expected_text'])
    if 'equals' in rule:
        return str(rule['equals'])
    if 'greater_than' in rule:
        return f"> {rule['greater_than']}"
    return 'present'


def _collect_fee_cap_excess_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int]]:
    """
    Return fee rows that exceed the per-base-id included fee cap.

    The cap is row-based: rows are kept in source order until the configured
    amount is reached. Rows that cross or exceed the cap are flagged and dropped
    from summary calculations.
    """
    source = df.copy()
    if 'base_id' not in source.columns:
        source['base_id'] = _base_id_from_transaction_id(source['Transaction ID'])

    invalid_parts = []
    excluded_indices = set()
    for base_id, group in source.groupby('base_id', sort=False):
        for fee_transaction, rule in FEE_CAP_RULES.items():
            fee_rows = group[group['Transaction'] == fee_transaction]
            if fee_rows.empty:
                continue

            column = str(rule['column'])
            limit = _numeric_value(rule['limit'])
            total = _numeric_sum(fee_rows, column)
            if total <= limit:
                continue

            cumulative = 0
            excess_indices = []
            for idx, row in fee_rows.iterrows():
                amount = _numeric_value(row.get(column))
                if amount <= 0:
                    continue
                cumulative += amount
                if cumulative > limit:
                    excess_indices.append(idx)

            if not excess_indices:
                continue

            label = str(rule.get('label', fee_transaction))
            unusual_rows = source.loc[excess_indices].copy()
            unusual_rows['base_id'] = base_id
            unusual_rows['unusual_reason'] = (
                f'{label} {column} total={total} exceeds included limit {limit}; '
                'excess fee row excluded from summary'
            )
            invalid_parts.append(unusual_rows)
            excluded_indices.update(excess_indices)

    if invalid_parts:
        unusual_df = pd.concat(invalid_parts, ignore_index=True, sort=False)
        sort_cols = [
            col for col in ['base_id', 'No', 'unusual_reason']
            if col in unusual_df.columns
        ]
        unusual_df = unusual_df.sort_values(sort_cols).reset_index(drop=True)
    else:
        unusual_df = source.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')

    return unusual_df, excluded_indices


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


def _fee_only_group_missing_main_reasons(transactions: pd.Series) -> list[str]:
    implied_mains = _fee_only_group_implied_mains(transactions)

    if len(implied_mains) == 1:
        return [f'missing main transaction for fee-only group: {implied_mains[0]}']
    if len(implied_mains) > 1:
        return [
            'fee-only group missing main transaction; fee rows imply multiple '
            f'main transactions: {", ".join(implied_mains)}'
        ]
    return []


def _fee_only_group_implied_mains(transactions: pd.Series) -> list[str]:
    normalized = transactions.fillna('').astype(str).str.strip().str.upper()
    return sorted({
        FEE_TRANSACTION_TO_MAIN[value]
        for value in normalized
        if value in FEE_TRANSACTION_TO_MAIN
    })


def _fee_only_group_excluded_from_summary(transactions: pd.Series) -> bool:
    implied_mains = _fee_only_group_implied_mains(transactions)
    return 'SELLTHRU' in implied_mains or len(implied_mains) != 1


def _unknown_transaction_reason(value: object) -> str:
    transaction = str(value).strip()
    if not transaction:
        transaction = '<blank>'
    return (
        f'{UNKNOWN_TRANSACTION_UNUSUAL_REASON_PREFIX}: {transaction}; '
        'excluded from summary'
    )


def _unknown_remark_reason(transaction_value: object) -> str:
    transaction = str(transaction_value).strip()
    if not transaction:
        transaction = '<blank>'
    return (
        f'{UNKNOWN_REMARK_UNUSUAL_REASON_PREFIX}: {transaction}; '
        'excluded from summary'
    )


def _collect_unknown_transaction_or_remark_unusual_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int]]:
    """
    Return rows whose Transaction label or normalized Remarks are not known.

    The summary sheet writes a fixed set of transaction labels. Unknown labels
    and known labels with unrecognized remark structures are therefore flagged
    and excluded instead of silently disappearing from the user-facing report.
    """
    source = df.copy()
    if 'base_id' not in source.columns:
        source['base_id'] = _base_id_from_transaction_id(source['Transaction ID'])

    transactions = source['Transaction'].fillna('').astype(str).str.strip()
    reversal_mask = transactions.apply(_is_reversal_transaction_label)
    known_mask = transactions.isin(KNOWN_SUMMARY_TRANSACTION_LABELS)
    unknown_mask = ~reversal_mask & ~known_mask

    normalized_remarks = source['Remarks'].map(_normalize_remark_pattern)
    remark_mismatch_mask = pd.Series(False, index=source.index)
    for transaction, known_patterns in KNOWN_TRANSACTION_REMARK_PATTERNS.items():
        transaction_mask = transactions.eq(transaction)
        remark_mismatch_mask |= (
            transaction_mask & ~normalized_remarks.isin(known_patterns)
        )

    unusual_mask = unknown_mask | remark_mismatch_mask
    row_reasons = {
        index: (
            _unknown_transaction_reason(source.at[index, 'Transaction'])
            if bool(unknown_mask.at[index])
            else _unknown_remark_reason(source.at[index, 'Transaction'])
        )
        for index in source.index[unusual_mask]
    }
    if not row_reasons:
        unusual_df = source.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')
        return unusual_df, set()

    unusual_base_ids = set(source.loc[list(row_reasons), 'base_id'])
    excluded_indices = set(
        source.index[source['base_id'].isin(unusual_base_ids)]
    )
    unusual_df = source.loc[sorted(excluded_indices)].copy()
    first_reason_by_base_id = {}
    for index, reason in row_reasons.items():
        first_reason_by_base_id.setdefault(source.at[index, 'base_id'], reason)
    unusual_df['unusual_reason'] = [
        row_reasons.get(
            index,
            f'{first_reason_by_base_id[source.at[index, "base_id"]]}; '
            'transaction family excluded',
        )
        for index in unusual_df.index
    ]
    unusual_df = unusual_df.sort_values(['base_id', 'No']).reset_index(drop=True)
    return unusual_df, excluded_indices


def _collect_st_family_unusual_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int]]:
    """Apply the date-versioned ST family contract once per base transaction."""
    source = df.copy()
    source['base_id'] = _base_id_from_transaction_id(source['Transaction ID'])
    invalid_parts = []
    excluded_indices = set()

    for base_id, group in source.groupby('base_id', sort=False):
        transaction = group['Transaction'].fillna('').astype(str).str.strip()
        reversal_mask = transaction.apply(_is_reversal_transaction_label)
        non_reversal_group = group[~reversal_mask]
        if non_reversal_group.empty:
            continue

        labels = set(non_reversal_group['Transaction'])
        if 'SELLTHRU' not in labels:
            continue

        main_rows = non_reversal_group[
            ~non_reversal_group['Transaction'].str.upper().str.endswith('FEE')
        ]
        standalone_rows = main_rows[
            main_rows.apply(_is_standalone_sellthru_source_row, axis=1)
        ]
        if not standalone_rows.empty:
            unexpected_rows = non_reversal_group[
                ~non_reversal_group['Transaction'].eq('SELLTHRU')
            ]
            if len(main_rows) != len(standalone_rows) or not unexpected_rows.empty:
                reasons = [
                    'standalone SELLTHRU must not have fee or companion rows; '
                    'excluded from summary'
                ]
                unusual_rows = group.copy()
                unusual_rows['base_id'] = base_id
                unusual_rows['unusual_reason'] = reasons[0]
                invalid_parts.append(unusual_rows)
                excluded_indices.update(group.index)
            continue

        era, era_reason = _st_rule_era_for_group(non_reversal_group)
        if era_reason:
            unusual_rows = group.copy()
            unusual_rows['base_id'] = base_id
            unusual_rows['unusual_reason'] = f'{era_reason}; excluded from summary'
            invalid_parts.append(unusual_rows)
            excluded_indices.update(group.index)
            continue

        sales_fee_rows = non_reversal_group[
            non_reversal_group['Transaction'] == 'SELLTHRUSALESFEE'
        ]
        reasons = []
        exclude_from_summary = False
        if era == ST_RULE_CURRENT and not sales_fee_rows.empty:
            reasons.append(
                'SELLTHRUSALESFEE retired from 2026-09-01; excluded from summary'
            )
            exclude_from_summary = True
        reasons.extend(
            _st_fee_validation_reasons(
                non_reversal_group,
                require_sales_fee=era == ST_RULE_LEGACY,
            )
        )
        if not reasons:
            continue

        summary_status = (
            'excluded from summary' if exclude_from_summary else 'included in summary'
        )
        unusual_rows = group.copy()
        unusual_rows['base_id'] = base_id
        unusual_rows['unusual_reason'] = '; '.join(reasons) + f'; {summary_status}'
        invalid_parts.append(unusual_rows)
        if exclude_from_summary:
            excluded_indices.update(group.index)

    if invalid_parts:
        unusual_df = pd.concat(invalid_parts, ignore_index=True, sort=False)
        unusual_df = unusual_df.sort_values(['base_id', 'No']).reset_index(drop=True)
    else:
        unusual_df = source.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')

    return unusual_df, excluded_indices


def _collect_fee_only_unusual_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int]]:
    """
    Return known fee-only non-reversal groups and their excluded source indices.

    Recharge-type fee-only groups are flagged but included in summary. ST
    fee-only groups are flagged and excluded because ST requires the main row.
    """
    source = df.copy()
    if 'base_id' not in source.columns:
        source['base_id'] = _base_id_from_transaction_id(source['Transaction ID'])

    invalid_parts = []
    excluded_indices = set()
    for base_id, group in source.groupby('base_id', sort=False):
        transaction = group['Transaction'].fillna('').astype(str)
        non_reversal_mask = ~transaction.apply(_is_reversal_transaction_label)
        non_reversal_group = group[non_reversal_mask]
        if non_reversal_group.empty:
            continue

        non_reversal_transaction = non_reversal_group['Transaction'].fillna('').astype(str)
        fee_mask = non_reversal_transaction.str.strip().str.upper().str.endswith('FEE')
        if not fee_mask.all():
            continue

        reasons = _fee_only_group_missing_main_reasons(non_reversal_transaction)
        if not reasons:
            continue

        exclude_from_summary = _fee_only_group_excluded_from_summary(
            non_reversal_transaction,
        )
        summary_status = (
            'excluded from summary'
            if exclude_from_summary
            else 'included in summary'
        )

        unusual_rows = non_reversal_group.copy()
        unusual_rows['base_id'] = base_id
        unusual_rows['unusual_reason'] = (
            '; '.join(reasons) + f'; {summary_status}'
        )
        invalid_parts.append(unusual_rows)
        if exclude_from_summary:
            excluded_indices.update(non_reversal_group.index)

    if invalid_parts:
        unusual_df = pd.concat(invalid_parts, ignore_index=True, sort=False)
        unusual_df = unusual_df.sort_values(['base_id', 'No']).reset_index(drop=True)
    else:
        unusual_df = source.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')

    return unusual_df, excluded_indices


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
    reversal_base_ids = _base_id_from_transaction_id(
        result.loc[reversal_mask, 'Transaction ID']
    )
    pembelian_recharge_out_cluster_mask = _remarks_contain(
        remarks,
        UNUSUAL_RECHARGE_EXEMPT_REMARK,
    )
    recharge_out_cluster_mask = _remarks_contain(
        remarks,
        REVERSAL_RECHARGE_OUT_CLUSTER_MAIN_REMARK,
    )
    recharge_out_cluster_base_ids = set(
        reversal_base_ids[recharge_out_cluster_mask]
    )
    in_recharge_out_cluster_group = reversal_base_ids.isin(
        recharge_out_cluster_base_ids
    )
    recharge_platform_fee_mask = _remarks_contain(
        remarks,
        REVERSAL_NGRS_PLATFORM_FEE_REMARK,
    )
    category_masks = {
        REVERSAL_NGRS_CATEGORY: (
            _remarks_contain(remarks, REVERSAL_NGRS_MAIN_REMARK)
            & ~pembelian_recharge_out_cluster_mask
        ),
        REVERSAL_NGRS_FEE_CATEGORY: (
            recharge_platform_fee_mask & ~in_recharge_out_cluster_group
        ),
        REVERSAL_PEMBELIAN_RECHARGE_OUT_CLUSTER_CATEGORY: (
            pembelian_recharge_out_cluster_mask
        ),
        REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: recharge_out_cluster_mask,
        REVERSAL_RECHARGE_OUT_CLUSTER_FEE_CATEGORY: (
            recharge_platform_fee_mask & in_recharge_out_cluster_group
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


def ensure_reversal_labels_prepared(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return rows whose reversal labels are ready for summary/detail consumers.

    The Kestra flow preprocesses labels once and carries
    processed_transaction_label downstream. For that normal path, trust the
    prepared labels and avoid running the remark classifier again. External
    callers that pass raw legacy data still get the original relabel behavior.
    """
    if PROCESSED_TRANSACTION_LABEL_COLUMN in df.columns:
        return df.copy()
    return relabel_reversal_transactions(df)


def preprocess_transaction_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply all transaction relabeling before unusual detection and downstream
    calculation/detail paths.
    """
    result = df.copy()
    if 'raw_transaction_label' not in result.columns:
        result['raw_transaction_label'] = result['Transaction']
    result['Transaction'] = (
        result['Transaction'].fillna('').astype(str).str.strip().str.upper()
    )
    result = relabel_reversal_transactions(result)
    result = relabel_standalone_sellthru_transactions(result)
    result = relabel_out_cluster_transactions(result)
    result = relabel_pembelian_recharge_out_cluster_transactions(result)
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

    if known_categories & REVERSAL_ST_UNSUPPORTED_CATEGORIES:
        return None, [REVERSAL_ST_UNSUPPORTED_REASON], True

    if len(implied_main_categories) > 1:
        return None, ['ambiguous reversal remarks matched multiple categories'], True

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


def _collect_validation_unusual_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, set[int], dict[str, int]]:
    """Evaluate all non-duplicate rows through one shared disposition path."""
    unknown_unusual_df, unknown_excluded_indices = (
        _collect_unknown_transaction_or_remark_unusual_transactions(df)
    )
    known_result = (
        df.drop(index=sorted(unknown_excluded_indices))
        if unknown_excluded_indices
        else df
    )
    st_unusual_df, st_excluded_indices = _collect_st_family_unusual_transactions(
        known_result
    )
    st_result = (
        known_result.drop(index=sorted(st_excluded_indices))
        if st_excluded_indices
        else known_result
    )
    fee_cap_unusual_df, fee_cap_excluded_indices = (
        _collect_fee_cap_excess_transactions(st_result)
    )
    validation_result = (
        st_result.drop(index=sorted(fee_cap_excluded_indices))
        if fee_cap_excluded_indices
        else st_result
    )
    reversal_unusual_df, reversal_excluded_indices, categorized_counts = (
        _collect_reversal_unusual_transactions(validation_result)
    )
    fee_only_unusual_df, fee_only_excluded_indices = (
        _collect_fee_only_unusual_transactions(validation_result)
    )
    all_excluded_indices = (
        set(unknown_excluded_indices)
        | set(st_excluded_indices)
        | set(fee_cap_excluded_indices)
        | set(reversal_excluded_indices)
        | set(fee_only_excluded_indices)
    )
    unusual_parts = [
        part for part in (
            fee_cap_unusual_df,
            unknown_unusual_df,
            st_unusual_df,
            _flag_fee_rule_unusual_transactions(validation_result),
            reversal_unusual_df,
        )
        if not part.empty
    ]
    if unusual_parts:
        unusual_df = pd.concat(unusual_parts, ignore_index=True, sort=False)
        sort_cols = [
            col for col in ['base_id', 'No', 'unusual_reason']
            if col in unusual_df.columns
        ]
        if sort_cols:
            unusual_df = unusual_df.sort_values(sort_cols).reset_index(drop=True)
    else:
        unusual_df = df.iloc[0:0].copy()
        unusual_df['base_id'] = pd.Series(dtype='object')
        unusual_df['unusual_reason'] = pd.Series(dtype='object')

    return unusual_df, all_excluded_indices, categorized_counts


def prepare_reversal_summary_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepare summary rows and unusual rows through one shared evaluator."""
    result = ensure_reversal_labels_prepared(df)
    unusual_df, all_excluded_indices, categorized_counts = (
        _collect_validation_unusual_transactions(result)
    )
    summary_ready = (
        result.drop(index=sorted(all_excluded_indices)).reset_index(drop=True)
        if all_excluded_indices
        else result.reset_index(drop=True)
    )

    print(f'Reversal rows categorized for summary: {categorized_counts}')
    print(f'Summary unusual rows flagged: {len(unusual_df)}')
    print(f'Summary rows excluded from summary: {len(all_excluded_indices)}')
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
    fee_only_unusual_df, _ = _collect_fee_only_unusual_transactions(df)
    unusual_parts = [fee_only_unusual_df] if not fee_only_unusual_df.empty else []
    for base_id, group in df.groupby('base_id', sort=False):
        transaction = group['Transaction'].fillna('').astype(str)
        non_reversal_mask = ~transaction.apply(_is_reversal_transaction_label)
        non_reversal_group = group[non_reversal_mask]
        if non_reversal_group.empty:
            continue

        non_reversal_transaction = non_reversal_group['Transaction'].fillna('').astype(str)
        fee_mask = non_reversal_transaction.str.strip().str.upper().str.endswith('FEE')
        main_rows = non_reversal_group[~fee_mask]
        if main_rows.empty:
            continue
        txn_type = main_rows['Transaction'].iloc[0]
        if txn_type == 'SELLTHRU':
            # ST is date-versioned and validated by the family evaluator below.
            continue
        if txn_type not in TRANSACTION_GROUP_RULES:
            continue
        reasons = _validate_transaction_group_rules(non_reversal_group, txn_type)
        if reasons:
            unusual_rows = non_reversal_group.copy()
            unusual_rows['unusual_reason'] = '; '.join(reasons) + '; included in summary'
            unusual_parts.append(unusual_rows)

    if unusual_parts:
        result = pd.concat(unusual_parts, ignore_index=True, sort=False)
        result = result.sort_values(['base_id', 'No']).reset_index(drop=True)
    else:
        result = df.iloc[0:0].copy()
        result['unusual_reason'] = pd.Series(dtype='object')

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
    validation_unusual_df, _, _ = _collect_validation_unusual_transactions(
        deduplicated_df
    )

    unusual_parts = [
        part
        for part in (
            validation_unusual_df,
            duplicate_unusual_df,
        )
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
        result = validation_unusual_df

    print(f'Combined unusual rows      : {len(result)}')
    print(f'Validation unusual rows    : {len(validation_unusual_df)}')
    print(f'Duplicate unusual rows     : {len(duplicate_unusual_df)}')
    return result
