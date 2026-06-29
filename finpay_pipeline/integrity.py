"""Row-level integrity checks used after schema validation."""
import pandas as pd

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
