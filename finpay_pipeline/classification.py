"""Transaction relabeling, fee validation, and unusual-row classification."""
import pandas as pd

from .dedup import deduplicate_rows_by_minute_with_report

SUMMARY_OUT_CLUSTER_REMARK = 'fee pembelian recharge out cluster'
UNUSUAL_RECHARGE_EXEMPT_REMARK = 'biaya pembelian recharge out cluster'

REVERSAL_TRANSACTION = 'REVERSAL'
REVERSAL_NGRS_CATEGORY = 'Reversal - NGRS'
REVERSAL_NGRS_FEE_CATEGORY = 'Reversal - NGRS FEE'
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

REVERSAL_CATEGORY_TO_MAIN = {
    REVERSAL_NGRS_CATEGORY: REVERSAL_NGRS_CATEGORY,
    REVERSAL_NGRS_FEE_CATEGORY: REVERSAL_NGRS_CATEGORY,
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
    REVERSAL_RECHARGE_OUT_CLUSTER_CATEGORY: (
        f'missing reversal remark: {REVERSAL_RECHARGE_OUT_CLUSTER_MAIN_REMARK}'
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
        REVERSAL_NGRS_CATEGORY: _remarks_contain(remarks, REVERSAL_NGRS_MAIN_REMARK),
        REVERSAL_NGRS_FEE_CATEGORY: (
            recharge_platform_fee_mask & ~in_recharge_out_cluster_group
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


def prepare_reversal_summary_transactions(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Relabel REVERSAL rows for summary and return unusual summary rows.

    Invalid NGRS, Recharge Out Cluster, and Recharge-type fee-only groups stay
    in the summary but are flagged. ST, ambiguous, or unclassified groups are
    flagged and excluded.
    """
    result = relabel_reversal_transactions(df)
    unusual_df, excluded_indices, categorized_counts = (
        _collect_reversal_unusual_transactions(result)
    )
    fee_only_unusual_df, fee_only_excluded_indices = (
        _collect_fee_only_unusual_transactions(result)
    )
    all_excluded_indices = set(excluded_indices) | set(fee_only_excluded_indices)

    if all_excluded_indices:
        summary_ready = result.drop(index=sorted(all_excluded_indices)).reset_index(drop=True)
    else:
        summary_ready = result.reset_index(drop=True)

    unusual_parts = [
        part for part in (unusual_df, fee_only_unusual_df)
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
            unusual_df = unusual_df.reset_index(drop=True)

    print(f'Reversal rows categorized for summary: {categorized_counts}')
    print(f'Summary unusual rows flagged: {len(unusual_df)}')
    print(f'Reversal rows excluded from summary: {len(excluded_indices)}')
    print(f'Fee-only rows excluded from summary: {len(fee_only_excluded_indices)}')
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
