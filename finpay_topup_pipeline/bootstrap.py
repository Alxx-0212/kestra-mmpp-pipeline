from decimal import Decimal

import psycopg

from .config import (
    TABLE_BALANCE,
    TABLE_CLUSTER_OUTLET,
    TABLE_OUTLET,
    dsn_from_env,
)
from .legacy_normalization import LEGACY_CHECKPOINT_CANDIDATES
from .legacy_outlet import LEGACY_OUTLETS_BY_CLUSTER, outlet_code
from .schema import ensure_schema


def bootstrap_database(conn):
    """Create/validate the production Top-Up foundation without loading legacy rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        database = cur.fetchone()[0]
        if database != "finpay":
            raise RuntimeError(f"refusing Top-Up bootstrap on database {database!r}")

    ensure_schema(conn)
    expected_mappings = {
        (cluster_id, outlet_code(cluster_id, label), label)
        for cluster_id, labels in LEGACY_OUTLETS_BY_CLUSTER.items()
        for label in labels
    }
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT m.cluster_id,m.outlet_code,o.label FROM {TABLE_CLUSTER_OUTLET} m "
            f"JOIN {TABLE_OUTLET} o ON o.code=m.outlet_code "
            "WHERE m.active AND o.active"
        )
        actual_mappings = set(cur.fetchall())
        if actual_mappings != expected_mappings:
            raise RuntimeError(
                f"outlet foundation mismatch: expected {len(expected_mappings)}, "
                f"found {len(actual_mappings)}"
            )

        checkpoint_updates = 0
        for cluster_id, (as_of_date, opening_balance) in LEGACY_CHECKPOINT_CANDIDATES.items():
            expected_balance = Decimal(str(opening_balance))
            cur.execute(
                f"SELECT opening_balance FROM {TABLE_BALANCE} "
                "WHERE cluster_id=%s AND as_of_date=%s",
                (cluster_id, as_of_date),
            )
            existing = cur.fetchone()
            if existing is not None:
                if Decimal(str(existing[0])) != expected_balance:
                    raise RuntimeError(
                        f"checkpoint conflict for {cluster_id} at {as_of_date}"
                    )
                continue
            cur.execute(
                f"SELECT max(as_of_date) FROM {TABLE_BALANCE} WHERE cluster_id=%s",
                (cluster_id,),
            )
            latest = cur.fetchone()[0]
            if latest is not None and latest > as_of_date:
                continue
            cur.execute(
                f"INSERT INTO {TABLE_BALANCE} (cluster_id,as_of_date,opening_balance,note) "
                "VALUES (%s,%s,%s,%s)",
                (
                    cluster_id,
                    as_of_date,
                    expected_balance,
                    "finance validated production foundation",
                ),
            )
            checkpoint_updates += 1
    conn.commit()
    return {
        "database": "finpay",
        "outlet_mappings": len(actual_mappings),
        "checkpoint_updates": checkpoint_updates,
        "legacy_rows_loaded": 0,
    }


def main():
    conn = psycopg.connect(dsn_from_env())
    try:
        result = bootstrap_database(conn)
    finally:
        conn.close()
    print(
        "event=finpay_topup_bootstrap "
        f"database={result['database']} outlet_mappings={result['outlet_mappings']} "
        f"checkpoint_updates={result['checkpoint_updates']} legacy_rows_loaded=0"
    )


if __name__ == "__main__":
    main()
