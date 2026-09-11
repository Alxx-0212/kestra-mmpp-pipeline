# Historical Grist Prototype

This document is retained as historical context only. Grist is not the current
FinPay Top-Up dashboard or refresh trigger. The active read-only dashboard is
Superset; see the root README and Compose configuration.

Do not store boot keys, database passwords, tokens, or private endpoints in
documentation. Obtain local credentials from Docker secrets and environment
configuration.

The prototype used the following FinPay relations for exploration:

- `finpay_topup_txn`
- `finpay_topup_classification`
- `finpay_cluster_balance`
- `finpay_topup_refresh`
- `finpay_topup_refresh_cluster`
- `finpay_bucket_topup_snapshot`

The production data contract remains in
[`../finpay_topup_pipeline/README.md`](../finpay_topup_pipeline/README.md).
