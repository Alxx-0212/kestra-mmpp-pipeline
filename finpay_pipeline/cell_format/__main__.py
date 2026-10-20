"""Run cell-format DigiPOS acquisition from the command line."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .acquisition import acquire_cell_day
from .settings import expand_cell_range


def main() -> None:
    parser = argparse.ArgumentParser(description="Acquire cell-format FinPay DigiPOS exports")
    parser.add_argument("start_date")
    parser.add_argument("end_date")
    parser.add_argument("cluster_id", help="One configured cluster ID or ALL")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()
    runs = expand_cell_range(args.start_date, args.end_date, args.cluster_id)
    password = os.environ.get("DIGIPOS_PASSWORD", "")
    if not password:
        parser.error("DIGIPOS_PASSWORD environment variable is required")
    results = []
    for run in runs:
        path = Path(args.output_dir) / run["report_date"] / run["cluster_id"]
        results.append(acquire_cell_day(
            run["report_date"], run["cluster_id"], password, str(path),
        ))
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
