"""Micro-unit ledger: balances, escrow holds, captures, releases, refunds.

Amounts are micro-units of the exchange's payment asset (see ``app.algorand``);
one unit is 1_000_000 micros.

Every mutation writes an append-only LedgerEntry, so account state is always
reconstructible and auditable.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, LedgerEntry, Role, User


class InsufficientFunds(Exception):
    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"insufficient funds: need {needed} micros, have {available}")
        self.needed = needed
        self.available = available


def get_account(db: Session, user_id: str) -> Account:
    acct = db.scalar(select(Account).where(Account.user_id == user_id))
    if acct is None:
        acct = Account(user_id=user_id)
        db.add(acct)
        db.flush()
    return acct


def _record(
    db: Session,
    acct: Account,
    kind: str,
    amount: int,
    *,
    job_id: str | None = None,
    payment_id: str | None = None,
    memo: str = "",
) -> LedgerEntry:
    entry = LedgerEntry(
        account_id=acct.id,
        kind=kind,
        amount_micros=amount,
        balance_after_micros=acct.available_micros,
        job_id=job_id,
        payment_id=payment_id,
        memo=memo,
    )
    db.add(entry)
    return entry


def credit(db: Session, user_id: str, amount: int, memo: str = "", **kw) -> Account:
    acct = get_account(db, user_id)
    acct.available_micros += amount
    _record(db, acct, "credit", amount, memo=memo, **kw)
    db.flush()
    return acct


def debit(db: Session, user_id: str, amount: int, memo: str = "", **kw) -> Account:
    acct = get_account(db, user_id)
    if acct.available_micros < amount:
        raise InsufficientFunds(amount, acct.available_micros)
    acct.available_micros -= amount
    acct.lifetime_spent_micros += amount
    _record(db, acct, "debit", -amount, memo=memo, **kw)
    db.flush()
    return acct


def hold(db: Session, user_id: str, amount: int, *, job_id=None, payment_id=None) -> Account:
    """Move funds from available into escrow (x402 verify step)."""
    acct = get_account(db, user_id)
    if acct.available_micros < amount:
        raise InsufficientFunds(amount, acct.available_micros)
    acct.available_micros -= amount
    acct.escrow_micros += amount
    _record(db, acct, "hold", -amount, job_id=job_id, payment_id=payment_id, memo="escrow hold")
    db.flush()
    return acct


def release(db: Session, user_id: str, amount: int, *, job_id=None, payment_id=None, memo="escrow release") -> Account:
    """Return escrowed funds to the payer (refund / expiry / dispute won)."""
    acct = get_account(db, user_id)
    move = min(amount, acct.escrow_micros)
    acct.escrow_micros -= move
    acct.available_micros += move
    _record(db, acct, "release", move, job_id=job_id, payment_id=payment_id, memo=memo)
    db.flush()
    return acct


def capture(
    db: Session,
    *,
    payer_id: str,
    payee_id: str,
    amount: int,
    fee: int,
    job_id: str | None = None,
    payment_id: str | None = None,
) -> tuple[Account, Account]:
    """Settle escrow: payer escrow -> payee available (minus platform fee)."""
    payer = get_account(db, payer_id)
    payee = get_account(db, payee_id)
    take = min(amount, payer.escrow_micros)
    payer.escrow_micros -= take
    payer.lifetime_spent_micros += take
    _record(db, payer, "capture", -take, job_id=job_id, payment_id=payment_id, memo="settlement")

    net = take - fee
    payee.available_micros += net
    payee.lifetime_earned_micros += net
    _record(db, payee, "credit", net, job_id=job_id, payment_id=payment_id, memo="service revenue")

    if fee:
        treasury = _treasury(db)
        treasury.available_micros += fee
        treasury.lifetime_earned_micros += fee
        _record(db, treasury, "fee", fee, job_id=job_id, payment_id=payment_id, memo="platform fee")
    db.flush()
    return payer, payee


def _treasury(db: Session) -> Account:
    admin = db.scalar(select(User).where(User.role == Role.admin).order_by(User.created_at))
    if admin is None:  # platform always has a fee sink
        admin = User(
            email="treasury@m2x.local",
            display_name="Platform Treasury",
            password_hash="!",
            role=Role.admin,
            is_active=False,
        )
        db.add(admin)
        db.flush()
    return get_account(db, admin.id)


def balance_summary(db: Session, user_id: str) -> dict:
    acct = get_account(db, user_id)
    return {
        "account_id": acct.id,
        "available_micros": acct.available_micros,
        "escrow_micros": acct.escrow_micros,
        "lifetime_earned_micros": acct.lifetime_earned_micros,
        "lifetime_spent_micros": acct.lifetime_spent_micros,
    }


def total_volume(db: Session) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(
                LedgerEntry.kind == "capture"
            )
        )
        or 0
    ) * -1
