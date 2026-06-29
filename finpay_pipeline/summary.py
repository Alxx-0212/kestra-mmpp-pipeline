"""Summary aggregation functions for calculation-ready transactions."""
import pandas as pd

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
