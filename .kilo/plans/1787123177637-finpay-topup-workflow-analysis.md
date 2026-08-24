# FinPay Top-Up Workflow - Simplified Plan (Grist-First)

## 1. Simplified Schema (Core Tables Only)

### Core Tables (Keep)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `finpay_topup_txn` | Immutable transaction facts (append-only, source of truth) | `txn_id`, `cluster_id`, `transaction_date`, `transaction_type` (Kredit/Debit), `amount`, `currency`, `remarks`, `source_file`, `row_hash` (unique) |
| `finpay_topup_classification` | Finance-owned classification | `txn_id` (FK), `category`, `note`, `classified_by`, `classified_at` |
| `finpay_cluster_balance` | Opening balance checkpoints | `cluster_id`, `as_of_date`, `opening_balance` (PK: cluster_id, as_of_date) |
| `finpay_refresh_audit` | Refresh orchestration | `refresh_id`, `requested_start`, `requested_end`, `status`, `kestra_execution_id` |
| `finpay_refresh_cluster` | Per-cluster refresh detail | `refresh_id`, `cluster_id`, `source_row_count`, `loaded_seen`, `loaded_inserted`, `computed_saldo`, `bucket_topup_value`, `verification_diff`, `status` |
| `finpay_bucket_topup_snapshot` | CMS bucket snapshots for verification history | `cluster_id`, `report_date`, `bucket_topup_value`, `computed_saldo`, `verification_diff`, `status` (PK: cluster_id, report_date) |

### Tables/Views REMOVED (Moved to Grist Formulas)

| Removed | Replaced By |
|---------|-------------|
| `VIEW_SALDO` (running saldo view) | Grist formula on `finpay_topup_txn` + `finpay_cluster_balance` |
| `VIEW_DAILY` (daily aggregates) | Grist formula/widget |
| `VIEW_CLUSTER_SUMMARY` | Grist widget |
| `VIEW_UNCLASSIFIED` | Grist filter on classification table |
| `VIEW_BUCKET_COMPARE` | Grist chart widget |
| `VIEW_RECONCILIATION_DAILY` | Grist table widget |
| `VIEW_REFRESH_STATUS` | Grist widget (pipeline still writes to `finpay_refresh_audit`/`finpay_refresh_cluster`) |

---

## 2. Verification Logic Rewrite (Python)

Move running sum computation from SQL views to Python functions in `saldo.py`:

```python
# New saldo.py - computes running saldo directly from transactions + balance checkpoints

def _compute_running_saldo(conn, cluster_id, cutoff_date=None):
    """Compute running saldo for a cluster up to cutoff_date (exclusive)."""
    with conn.cursor() as cur:
        # Get opening balance
        cur.execute(
            "SELECT opening_balance FROM finpay_cluster_balance "
            "WHERE cluster_id = %s AND as_of_date <= COALESCE(%s, 'infinity'::date) "
            "ORDER BY as_of_date DESC LIMIT 1",
            (cluster_id, cutoff_date)
        )
        row = cur.fetchone()
        opening = row[0] if row else 0
        
        # Get transactions
        query = """
            SELECT transaction_date, transaction_type, amount
            FROM finpay_topup_txn
            WHERE cluster_id = %s
        """
        params = [cluster_id]
        if cutoff_date:
            query += " AND transaction_date::date < %s"
            params.append(cutoff_date)
        query += " ORDER BY transaction_date, txn_id"
        
        cur.execute(query, params)
        saldo = opening
        for row in cur.fetchall():
            if row[1] == 'Kredit':
                saldo += row[2]
            elif row[1] == 'Debit':
                saldo -= row[2]
        return saldo
```

**Functions to rewrite:**
- `current_saldo(conn, cluster_id)` → `_compute_running_saldo(conn, cluster_id)`
- `latest_saldo_snapshot(conn, cluster_id)` → returns `{saldo, transaction_date}` from latest txn
- `current_saldo_before_date(conn, cluster_id, cutoff_date)` → `_compute_running_saldo(conn, cluster_id, cutoff_date)`
- `summary_by_cluster(conn)` → compute in Python from transactions
- `update_opening_balance_from_latest(conn)` → use Python computation
- `update_opening_balance_before_date(conn, cutoff_date)` → use Python computation

---

## 2. Grist Integration (Simplified)

### Grist Docker Service

```yaml
grist:
  image: gristlabs/grist:latest
  networks:
    - mmpp-finance-network
  volumes:
    - grist-data:/persist
  environment:
    - PORT=8484
    - GRIST_BOOT_KEY=${GRIST_BOOT_KEY}
    - GRIST_DEFAULT_EMAIL=finance@mmpp.local
    - DATABASE_URL=postgresql://${GRIST_DB_USER}:${GRIST_DB_PASSWORD}@postgres:5432/${GRIST_DB_NAME}
  ports:
    - "8484:8484"
  depends_on:
    postgres:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8484"]
    interval: 30s
    timeout: 10s
    retries: 5
  secrets:
    - grist_db_password
    - grist_boot_key
```

**Required secrets:** `grist_db_password.txt`, `grist_boot_key.txt`

### Grist Database Setup

1. Grist metadata DB: `grist` (own database)
2. FinPay source data: `finpay` database (read-only connection from Grist)
3. Create `finpay_readonly` user with `GRANT SELECT ON ALL TABLES IN SCHEMA public TO finpay_readonly`

### Grist Dashboard (Grist Formulas)

| Component | Grist Implementation |
|-----------|---------------------|
| **Running Saldo** | Formula column on Transactions table: `SUMIF(Transaction_Type="Kredit", Amount) - SUMIF(Transaction_Type="Debit", Amount) + Opening_Balance` grouped by Cluster |
| **Daily Aggregates** | Pivot table / summary table widget |
| **KPI Cards** | Card widgets from summary table |
| **Trend Chart** | Line chart widget |
| **Reconciliation** | Table widget with conditional formatting |
| **Refresh Button** | Link widget → `http://<host>:8095/refresh` |

---

## 3. Implementation Tasks

### Phase 1: Database Simplification (Week 1)

| Task | Files |
|------|-------|
| 1.1 Remove all dashboard views from `schema.py` | `finpay_topup_pipeline/schema.py` |
| 1.2 Rewrite `saldo.py` with Python-based running sum computation | `finpay_topup_pipeline/saldo.py` |
| 1.3 Update `verify.py` to use new `saldo.py` functions | `finpay_topup_pipeline/verify.py` |
| 1.4 Update `correctness.py` for new `saldo.py` API | `finpay_topup_pipeline/correctness.py` |
| 1.5 Update `reconcile.py` (uses `VIEW_SALDO`) | `finpay_topup_pipeline/reconcile.py` |
| 1.6 Update `audit.py` if it references views | `finpay_topup_pipeline/audit.py` |
| 1.7 Run tests to verify verification logic | `finpay_topup_pipeline/tests/` |

### Phase 2: Grist Infrastructure (Week 1)

| Task | Files |
|------|-------|
| 2.1 Add Grist service to `docker-compose.yml` | `docker-compose.yml` |
| 1.2 Add Grist secrets | `secrets/grist_db_password.txt`, `secrets/grist_boot_key.txt` |
| 2.3 Update `.env.example` with Grist vars | `.env.example` |
| 2.4 Add Grist DB + read-only user to initdb | `docker/initdb/00-create-databases.sh` |
| 2.5 Add Grist secrets to `.env_encoded.example` | `.env_encoded.example` |
| 2.6 Add `finpay_readonly` user to initdb | `docker/initdb/00-create-databases.sh` |
| 2.7 Validate compose & restart | `docker compose up -d` |

### Phase 3: Grist Workspace Setup (Week 2)

| Task | Description |
|------|-------------|
| 3.1 | Create Grist workspace "FinPay Finance" |
| 3.2 | Connect Grist to `finpay` PostgreSQL (read-only user) |
| 3.3 | Import core tables as Grist tables |
| 3.3 | Add Grist formula columns for running saldo |
| 3.3 | Build dashboard widgets (Ledger, KPIs, Trend, Reconciliation) |
| 3.4 | Add Refresh button link widget |
| 3.4 | Configure filter widgets (cluster_id, report_date) |

### Phase 4: Validation & Cutover (Week 2)

| Task | Description |
|------|-------------|
| 4.1 | Run validation: synthetic fixtures, pagination, legacy reconciliation |
| 4.2 | Finance team UAT on Grist dashboard |
| 4.3 | Add Kestra cron trigger (02:00 Asia/Makassar daily) |
| 4.4 | Deprecate Superset (keep for reference) |

---

## 4. Validation Plan

- [ ] Synthetic Kredit/Debit fixtures pass with new Python running sum
- [ ] Pagination responses tested
- [ ] Legacy workbook reconciliation against isolated DB
- [ ] Read-only database role check for Grist
- [ ] Bounded Grist table query with cluster/date filters
- [ ] BUCKET TOP UP verification matches legacy tolerance (0.5 IDR)

---

## 4. Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Python running sum performance | Medium | Index on `(cluster_id, transaction_date, txn_id)`; batch compute |
| Verification logic regression | High | Comprehensive tests comparing old vs new computation |
| Grist large table performance | Medium | Use Grist views with LIMIT; avoid loading full `finpay_topup_txn` |
| DigiPOS CMS scrape brittleness | High | Retry logic; monitor scrape failures |

---

## 6. Conclusion

**Simplified architecture:**
- **Database**: 6 core tables only (no dashboard views)
- **Verification**: Python-based running sum computation
- **Dashboard**: Grist formulas/widgets (Excel-like UX for finance team)
- **Orchestration**: Unchanged Kestra workflow

**Benefits:**
- ✅ Simpler database schema (6 tables vs 13+ views)
- ✅ Excel-like UX for finance team
- ✅ No CSP issues (no Handlebars/eval)
- ✅ Grist formulas handle running sums naturally
- ✅ Finance team can self-serve classification in Grist

**Next Step**: Implement Phase 1 (database simplification + Python running sum rewrite).