"""FinPay Kredit classification bot (Telegram inline-keyboard walkthrough).

The bot holds no authoritative state: pending/classified truth lives only in
Postgres (`finpay_topup_txn ⟕ finpay_topup_classification`) and sessions
rebuild from the database after restart.
"""
