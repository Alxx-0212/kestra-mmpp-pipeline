import argparse
import sys

from .config import dsn_from_env
from . import schema, loader, saldo, backfill, reconcile, classification, extract
from .correctness import restore_dashboard_data
from . import refresh_service


def _conn(args):
    import psycopg
    return psycopg.connect(args.dsn or dsn_from_env())


def cmd_ensure_schema(args):
    conn = _conn(args)
    schema.ensure_schema(conn)
    conn.close()
    print("schema ensured")


def cmd_load(args):
    conn = _conn(args)
    res = loader.load_inbox(conn, args.inbox)
    conn.close()
    for cluster, counts in res.items():
        print(f"{cluster}: inserted={counts['inserted']} seen={counts['seen']}")


def cmd_summary(args):
    conn = _conn(args)
    rows = saldo.summary_by_cluster(conn)
    conn.close()
    for r in rows:
        print(r)


def cmd_saldo(args):
    conn = _conn(args)
    val = saldo.current_saldo(conn, args.cluster)
    conn.close()
    print(val)


def cmd_backfill(args):
    conn = _conn(args)
    res = backfill.backfill_xlsx(
        conn,
        args.xlsx,
        default_cluster_id=args.cluster_id,
        source_file=args.xlsx,
        from_date=args.from_date,
        before_date=args.before_date,
    )
    conn.close()
    print(res)


def cmd_reconcile(args):
    conn = _conn(args)
    mismatches = reconcile.reconcile_xlsx(conn, args.xlsx, tolerance=args.tolerance)
    conn.close()
    print(f"mismatches={len(mismatches)}")
    for m in mismatches[:20]:
        print(m)


def cmd_extract(args):
    res = extract.run_extract(args.start, args.end, users=args.users, output_dir=args.output_dir, password=args.password)
    print(res)


def cmd_restore_dashboard_data(args):
    import json

    conn = _conn(args)
    result = restore_dashboard_data(
        conn,
        legacy_xlsx=args.legacy_xlsx,
        legacy_cluster_id=args.legacy_cluster_id,
        legacy_from_date=args.legacy_from_date,
        legacy_before_date=args.legacy_before_date,
        opening_as_of_date=args.opening_as_of_date,
        opening_balance=args.opening_balance,
        inbox_dirs=args.inbox,
        reset=args.reset_topup_data,
        refresh_start=args.refresh_start,
        refresh_end=args.refresh_end,
        requested_users=args.users,
        requested_by=args.requested_by,
        trigger_source=args.trigger_source,
        verify_status=args.verify_status,
        bucket_topup_value=args.bucket_topup_value,
        bucket_topup_text=args.bucket_topup_text,
    )
    conn.close()
    print(json.dumps({
        "legacy": result.legacy,
        "loaded": result.loaded,
        "refresh_id": result.refresh_id,
        "status": result.status,
        "summary": result.summary,
    }, default=str, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="finpay_topup_pipeline")
    p.add_argument("--dsn", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ensure-schema")
    sp.set_defaults(func=cmd_ensure_schema)

    lp = sub.add_parser("load")
    lp.add_argument("--inbox", required=True)
    lp.set_defaults(func=cmd_load)

    sm = sub.add_parser("summary")
    sm.set_defaults(func=cmd_summary)

    sa = sub.add_parser("saldo")
    sa.add_argument("--cluster", required=True)
    sa.set_defaults(func=cmd_saldo)

    bf = sub.add_parser("backfill")
    bf.add_argument("--xlsx", required=True)
    bf.add_argument("--cluster-id", default="MOROWALI")
    bf.add_argument("--from-date", default=None)
    bf.add_argument("--before-date", default=None)
    bf.set_defaults(func=cmd_backfill)

    rc = sub.add_parser("reconcile")
    rc.add_argument("--xlsx", required=True)
    rc.add_argument("--tolerance", type=float, default=0.5)
    rc.set_defaults(func=cmd_reconcile)

    ex = sub.add_parser("extract")
    ex.add_argument("--start", required=True)
    ex.add_argument("--end", required=True)
    ex.add_argument("--users", nargs="*", default=None)
    ex.add_argument("--output-dir", default="finpay-topup-inbox")
    ex.add_argument("--password", default=None)
    ex.set_defaults(func=cmd_extract)

    rd = sub.add_parser("restore-dashboard-data")
    rd.add_argument("--legacy-xlsx", default="data/FINPAY MOROWALI.xlsx")
    rd.add_argument("--legacy-cluster-id", default="421318")
    rd.add_argument("--legacy-from-date", default=None)
    rd.add_argument("--legacy-before-date", default=None)
    rd.add_argument("--opening-as-of-date", default=None)
    rd.add_argument("--opening-balance", default=None)
    rd.add_argument("--inbox", action="append", default=[])
    rd.add_argument("--reset-topup-data", action="store_true")
    rd.add_argument("--refresh-start", default=None)
    rd.add_argument("--refresh-end", default=None)
    rd.add_argument("--users", nargs="*", default=None)
    rd.add_argument("--requested-by", default="codex")
    rd.add_argument("--trigger-source", default="local_restore")
    rd.add_argument("--verify-status", default="VERIFY_SKIPPED")
    rd.add_argument("--bucket-topup-value", type=float, default=None)
    rd.add_argument("--bucket-topup-text", default=None)
    rd.set_defaults(func=cmd_restore_dashboard_data)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
