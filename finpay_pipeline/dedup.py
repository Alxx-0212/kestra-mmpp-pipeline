"""Minute-level duplicate detection and unusual duplicate reporting."""
import pandas as pd

DUPLICATE_UNUSUAL_REASON = "duplicate row removed from calculation; excluded from summary"


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
