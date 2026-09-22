import hashlib
from decimal import Decimal

from .config import TABLE_CHECKPOINT_OVERRIDE, TABLE_MANUAL_ADJUSTMENT


def _hash_adjustment(values):
    payload = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def propose_adjustment(
    conn,
    cluster_id,
    effective_at,
    transaction_type,
    amount,
    reason,
    created_by,
    *,
    currency="IDR",
    sender=None,
    receiver=None,
    remarks=None,
    source_reference=None,
    commit=True,
):
    """Create an auditable adjustment; it is excluded until explicitly approved."""
    if transaction_type not in {"Kredit", "Debit"}:
        raise ValueError("adjustment transaction_type must be Kredit or Debit")
    amount = Decimal(str(amount))
    if amount < 0:
        raise ValueError("adjustment amount must be non-negative")
    if not reason or not created_by:
        raise ValueError("adjustment reason and creator are required")
    adjustment_hash = _hash_adjustment((
        cluster_id,
        effective_at,
        transaction_type,
        amount,
        currency,
        sender,
        receiver,
        remarks,
        reason,
        source_reference,
    ))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_MANUAL_ADJUSTMENT} "
            "(cluster_id, effective_at, transaction_type, amount, currency, sender, receiver, remarks, "
            "reason, source_reference, adjustment_hash, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (adjustment_hash) DO NOTHING RETURNING adjustment_id, status",
            (
                cluster_id,
                effective_at,
                transaction_type,
                amount,
                currency,
                sender,
                receiver,
                remarks,
                reason,
                source_reference,
                adjustment_hash,
                created_by,
            ),
        )
        row = cur.fetchone()
    if commit:
        conn.commit()
    return {"adjustment_id": row[0], "status": row[1], "adjustment_hash": adjustment_hash} if row else None


def approve_adjustment(conn, adjustment_id, approved_by, *, commit=True):
    if not approved_by:
        raise ValueError("approver is required")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE_MANUAL_ADJUSTMENT} SET status='APPROVED', approved_by=%s, approved_at=now() "
            "WHERE adjustment_id=%s AND status='PROPOSED' RETURNING adjustment_id, status",
            (approved_by, adjustment_id),
        )
        row = cur.fetchone()
    if commit:
        conn.commit()
    return row


def void_adjustment(conn, adjustment_id, voided_by, *, commit=True):
    if not voided_by:
        raise ValueError("voiding operator is required")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE_MANUAL_ADJUSTMENT} SET status='VOID', voided_by=%s, voided_at=now() "
            "WHERE adjustment_id=%s AND status='APPROVED' RETURNING adjustment_id, status",
            (voided_by, adjustment_id),
        )
        row = cur.fetchone()
    if commit:
        conn.commit()
    return row


def propose_checkpoint_override(conn, cluster_id, as_of_date, opening_balance, reason, created_by):
    if not reason or not created_by:
        raise ValueError("checkpoint override reason and creator are required")
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_CHECKPOINT_OVERRIDE} "
            "(cluster_id, as_of_date, opening_balance, reason, created_by) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING override_id, status",
            (cluster_id, as_of_date, Decimal(str(opening_balance)), reason, created_by),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def approve_checkpoint_override(conn, override_id, approved_by):
    if not approved_by:
        raise ValueError("approver is required")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT cluster_id, as_of_date FROM {TABLE_CHECKPOINT_OVERRIDE} "
            "WHERE override_id=%s AND status='PROPOSED' FOR UPDATE",
            (override_id,),
        )
        proposed = cur.fetchone()
        if proposed is None:
            conn.commit()
            return None
        cur.execute(
            f"SELECT as_of_date FROM {TABLE_CHECKPOINT_OVERRIDE} WHERE cluster_id=%s AND status='APPROVED' "
            "UNION ALL SELECT as_of_date FROM finpay_cluster_balance WHERE cluster_id=%s "
            "ORDER BY as_of_date DESC LIMIT 1",
            (proposed[0], proposed[0]),
        )
        latest = cur.fetchone()
        if latest and proposed[1] < latest[0]:
            conn.rollback()
            raise ValueError("checkpoint override cannot move backward")
        cur.execute(
            f"UPDATE {TABLE_CHECKPOINT_OVERRIDE} SET status='APPROVED', approved_by=%s, approved_at=now() "
            "WHERE override_id=%s RETURNING override_id, status",
            (approved_by, override_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row
