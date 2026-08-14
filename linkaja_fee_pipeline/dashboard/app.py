"""Interactive, read-only Taipy GUI for LinkAja reconciliation and fees."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from linkaja_fee_pipeline.dashboard.config import DashboardConfig
from linkaja_fee_pipeline.dashboard.repository import (
    FEE_TYPES,
    LinkAjaDashboardRepository,
)
from linkaja_fee_pipeline.dashboard.service import (
    DashboardFilters,
    DashboardSnapshot,
    FEE_LABELS,
    LinkAjaDashboardService,
)


date_range = [date.today() - timedelta(days=30), date.today()]
cluster_options: list[str] = []
selected_clusters: list[str] = []
fee_options = [(fee_type, FEE_LABELS[fee_type]) for fee_type in FEE_TYPES]
selected_fee_types = list(FEE_TYPES)
daily_rows: list[dict[str, Any]] = []
fee_rows: list[dict[str, Any]] = []
fee_trend_rows: list[dict[str, Any]] = []
mandiri_rows: list[dict[str, Any]] = []
exception_rows: list[dict[str, Any]] = []
transaction_rows: list[dict[str, Any]] = []
monthly_rows: list[dict[str, Any]] = []
freshness_rows: list[dict[str, Any]] = []
expected_out_fee = "Rp0"
in_cluster_fee = "Rp0"
posted_digipos_fee = "Rp0"
ppob_fee = "Rp0"
withdrawal_total = "Rp0"
exception_count = "0"
blocking_exception_count = "0"
reconciliation_status = "Not loaded"
refreshed_at = "Not loaded"
range_label = ""
error_message = ""
is_loading = False


_service: LinkAjaDashboardService | None = None
_config: DashboardConfig | None = None


SHELL = """
<div class="dashboard-header">
  <div>
    <p class="eyebrow">FINANCE OPERATIONS</p>
    <h1>LinkAja reconciliation</h1>
    <p class="page-note">Daily settlement evidence, reversal-aware fees, and month-close status.</p>
  </div>
  <div class="status-panel">
    <span class="status-label">Current status</span>
    <strong><|{reconciliation_status}|text|></strong>
    <small><|{refreshed_at}|text|></small>
  </div>
</div>

<|navbar|lov={[("/overview", "Overview"), ("/fees", "Fees"), ("/mandiri", "Mandiri"), ("/exceptions", "Exceptions"), ("/transactions", "Transactions"), ("/monthly", "Month close")]}|class_name=dashboard-nav|>

<|part|class_name=filter-bar|
<|layout|columns=1.1 1 1.1 auto|gap=16px|
<|
<span class="field-label">Posting date</span>
<|{date_range}|date_range|label_start=Start|label_end=End|>
|>
<|
<span class="field-label">Clusters</span>
<|{selected_clusters}|selector|lov={cluster_options}|dropdown=True|multiple=True|>
|>
<|
<span class="field-label">Fee categories</span>
<|{selected_fee_types}|selector|lov={fee_options}|dropdown=True|multiple=True|>
|>
<|
<span class="field-label">&nbsp;</span>
<|Apply range|button|on_action=on_apply_filters|class_name=primary-action|>
|>
|>
<|{error_message}|text|class_name=error-message|>
|>
"""


OVERVIEW_PAGE = SHELL + """
<div class="section-heading">
  <div><h2>Daily position</h2><p><|{range_label}|text|></p></div>
</div>

<|layout|columns=repeat(3, minmax(0, 1fr))|gap=14px|class_name=kpi-grid|
<|part|class_name=kpi-card|<span>Withdrawal total</span><strong><|{withdrawal_total}|text|></strong><small>Actual withdrawal postings</small>|>
<|part|class_name=kpi-card|<span>Expected out-cluster</span><strong><|{expected_out_fee}|text|></strong><small>Active transactions × Rp200</small>|>
<|part|class_name=kpi-card|<span>In-cluster</span><strong><|{in_cluster_fee}|text|></strong><small>Active transactions × Rp20</small>|>
<|part|class_name=kpi-card|<span>Posted Digipos fee</span><strong><|{posted_digipos_fee}|text|></strong><small>Validated source fee</small>|>
<|part|class_name=kpi-card|<span>PPOB / agent-telco</span><strong><|{ppob_fee}|text|></strong><small>Source fee; never imputed</small>|>
<|part|class_name=kpi-card kpi-alert|<span>Exceptions</span><strong><|{exception_count}|text|></strong><small><|{blocking_exception_count}|text|> blocking</small>|>
|>

<|layout|columns=1.4 1|gap=18px|class_name=content-grid|
<|part|class_name=data-card|
### Fee movement
<p class="card-note">Active and reversed fee values for the selected categories.</p>
<|{fee_trend_rows}|chart|type=line|x=Date|y[1]=Active fee|y[2]=Reversed fee|height=340px|>
|>
<|part|class_name=data-card|
### Latest source loads
<p class="card-note">Most recent successful raw load by cluster.</p>
<|{freshness_rows}|table|page_size=8|show_all=False|>
|>
|>

<|part|class_name=data-card|
### Daily reconciliation
<p class="card-note">Fee categories remain separate; values are not summed into one payable amount.</p>
<|{daily_rows}|table|page_size=25|show_all=False|filter=True|>
|>
"""


FEES_PAGE = SHELL + """
<div class="section-heading"><div><h2>Fee evidence</h2><p><|{range_label}|text|></p></div></div>
<|part|class_name=callout|Expected, posted, and PPOB fees are distinct evidence streams. “Incomplete” means a required source fee is missing or invalid.|>
<|part|class_name=data-card|
<|{fee_rows}|table|page_size=30|show_all=False|filter=True|>
|>
"""


MANDIRI_PAGE = SHELL + """
<div class="section-heading"><div><h2>Mandiri settlement cycles</h2><p>Each cycle is bounded by actual LinkAja withdrawal timestamps.</p></div></div>
<|part|class_name=callout|LinkAja balance checks and Mandiri bank confirmation are independent statuses. Bank evidence remains pending until a separate statement feed is connected.|>
<|part|class_name=data-card|
<|{mandiri_rows}|table|page_size=25|show_all=False|filter=True|>
|>
"""


EXCEPTIONS_PAGE = SHELL + """
<div class="section-heading"><div><h2>Exceptions</h2><p>Exact transaction IDs are retained for investigation.</p></div></div>
<|part|class_name=data-card|
<|{exception_rows}|table|page_size=30|show_all=False|filter=True|>
|>
"""


TRANSACTIONS_PAGE = SHELL + """
<div class="section-heading"><div><h2>Transaction evidence</h2><p>Read-only current facts with source provenance and reversal state.</p></div></div>
<|part|class_name=data-card|
<|{transaction_rows}|table|page_size=30|show_all=False|filter=True|>
|>
"""


MONTHLY_PAGE = SHELL + """
<div class="section-heading"><div><h2>Published month close</h2><p>Frozen monthly snapshots only; this page does not materialize or change a close.</p></div></div>
<|part|class_name=data-card|
<|{monthly_rows}|table|page_size=30|show_all=False|filter=True|>
|>
"""


PAGES = {
    "/": OVERVIEW_PAGE,
    "overview": OVERVIEW_PAGE,
    "fees": FEES_PAGE,
    "mandiri": MANDIRI_PAGE,
    "exceptions": EXCEPTIONS_PAGE,
    "transactions": TRANSACTIONS_PAGE,
    "monthly": MONTHLY_PAGE,
}


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _state_filters(state: Any) -> DashboardFilters:
    values = list(state.date_range or [])
    if len(values) != 2:
        raise ValueError("select both a start and end date")
    return DashboardFilters(
        start_date=_as_date(values[0]),
        end_date=_as_date(values[1]),
        cluster_ids=tuple(state.selected_clusters or []),
        fee_types=tuple(state.selected_fee_types or []),
    )


def _assign_snapshot(state: Any, snapshot: DashboardSnapshot) -> None:
    for name in (
        "daily_rows",
        "fee_rows",
        "fee_trend_rows",
        "mandiri_rows",
        "exception_rows",
        "transaction_rows",
        "monthly_rows",
        "freshness_rows",
        "expected_out_fee",
        "in_cluster_fee",
        "posted_digipos_fee",
        "ppob_fee",
        "withdrawal_total",
        "exception_count",
        "blocking_exception_count",
        "reconciliation_status",
        "refreshed_at",
        "range_label",
    ):
        setattr(state, name, getattr(snapshot, name))


def _load_state(state: Any) -> None:
    if _service is None:
        raise RuntimeError("dashboard service is not configured")
    state.is_loading = True
    state.error_message = ""
    try:
        _assign_snapshot(state, _service.load(_state_filters(state)))
    except Exception as exc:
        state.error_message = f"Unable to load reconciliation: {exc}"
    finally:
        state.is_loading = False


def on_init(state: Any) -> None:
    if _service is None or _config is None:
        state.error_message = "Dashboard service is not configured."
        return
    state.date_range = [
        date.today() - timedelta(days=_config.default_date_span_days - 1),
        date.today(),
    ]
    try:
        state.cluster_options = _service.available_clusters()
        state.selected_clusters = list(state.cluster_options)
        if not state.selected_clusters:
            state.error_message = "No LinkAja clusters are available."
            return
        _load_state(state)
    except Exception as exc:
        state.error_message = f"Unable to initialize dashboard: {exc}"


def on_apply_filters(state: Any, _id: str | None = None, _payload: Any = None) -> None:
    _load_state(state)


def create_gui(
    config: DashboardConfig | None = None,
    repository: LinkAjaDashboardRepository | None = None,
):
    """Create the GUI without starting a server, enabling runtime smoke tests."""
    global _config, _service

    try:
        from taipy.gui import Gui
    except ImportError as exc:
        raise RuntimeError(
            "taipy-gui is required to run the LinkAja dashboard"
        ) from exc

    _config = config or DashboardConfig.from_env()
    active_repository = repository or LinkAjaDashboardRepository(
        _config.dsn,
        max_date_span_days=_config.max_date_span_days,
        query_timeout_seconds=_config.query_timeout_seconds,
    )
    _service = LinkAjaDashboardService(active_repository)
    return Gui(
        pages=PAGES,
        css_file=str(Path(__file__).with_name("dashboard.css")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="parse dashboard pages and exit without connecting to PostgreSQL",
    )
    args = parser.parse_args()
    config = DashboardConfig.from_env()
    gui = create_gui(config)
    if args.check:
        print("LinkAja dashboard configuration and pages are valid.")
        return
    gui.run(
        title="LinkAja Finance Reconciliation",
        host=config.host,
        port=config.port,
        debug=config.debug,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
