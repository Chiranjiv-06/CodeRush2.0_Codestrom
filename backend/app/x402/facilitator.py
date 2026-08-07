"""x402 facilitator: verify + settle.

Two settlement backends behind one interface:

* ``ledger``   — built-in escrow ledger, HMAC-signed authorizations. Always works.
* ``algorand`` — real ASA transfer via AlgoKit/algosdk when ALGOD is configured;
                 falls back to ``ledger`` if the SDK or node is unavailable.

A remote facilitator (``M2X_X402_FACILITATOR_URL``) is used when set, matching the
/verify and /settle endpoints of the official x402 facilitator API.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..algorand import AssetPolicyError, asset_descriptor, assert_asset
from ..algorand import asset_id as mandated_asset_id
from ..algorand import normalize_asset
from ..config import settings
from ..integrity import hash_object, sha256_hex
from ..models import Payment, PaymentStatus, User
from ..services import ledger
from .protocol import (
    PaymentPayload,
    PaymentRequirements,
    X402Error,
    verify_authorization_signature,
)

log = logging.getLogger("m2x.x402")


@dataclass
class VerifyResult:
    is_valid: bool
    payer: str = ""
    invalid_reason: str = ""
    amount_micros: int = 0
    asset_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "isValid": self.is_valid,
            "payer": self.payer,
            "assetId": self.asset_id or None,
            "invalidReason": self.invalid_reason or None,
        }


@dataclass
class SettleResult:
    success: bool
    transaction: str = ""
    network: str = ""
    payer: str = ""
    error_reason: str = ""
    backend: str = "ledger"
    asset_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "transaction": self.transaction,
            "network": self.network,
            "blockchain": settings.blockchain,
            "asset": str(self.asset_id or mandated_asset_id()),
            "assetId": self.asset_id or mandated_asset_id(),
            "payer": self.payer,
            "errorReason": self.error_reason or None,
        }


# --------------------------------------------------------------------------- #
# Settlement backends
# --------------------------------------------------------------------------- #
class LedgerSettlement:
    name = "ledger"

    def available(self) -> bool:
        return True

    def settle(self, db: Session, payment: Payment, amount_micros: int, fee_micros: int) -> SettleResult:
        ledger.capture(
            db,
            payer_id=payment.payer_id,
            payee_id=payment.payee_id,
            amount=amount_micros,
            fee=fee_micros,
            job_id=payment.job_id,
            payment_id=payment.id,
        )
        tx = "0x" + sha256_hex(
            f"{payment.id}:{payment.nonce}:{payment.asset_id}:{amount_micros}:{time.time_ns()}"
        )[:56]
        return SettleResult(True, tx, payment.network, payment.payer_id, backend=self.name,
                            asset_id=payment.asset_id or mandated_asset_id())

    def refund(self, db: Session, payment: Payment, amount_micros: int, reason: str) -> str:
        ledger.credit(
            db,
            payment.payer_id,
            amount_micros,
            memo=f"refund: {reason}",
            job_id=payment.job_id,
            payment_id=payment.id,
        )
        return "0x" + sha256_hex(f"refund:{payment.id}:{amount_micros}:{time.time_ns()}")[:56]


class AlgorandSettlement:  # pragma: no cover - requires algod + funded accounts
    """ASA transfer settlement via AlgoKit / algosdk."""

    name = "algorand"

    def __init__(self) -> None:
        self._client = None
        self._checked = False

    def _algod(self):
        if self._checked:
            return self._client
        self._checked = True
        if not settings.algod_url:
            return None
        try:
            from algosdk.v2client import algod

            client = algod.AlgodClient(settings.algod_token, settings.algod_url)
            client.status()
            self._client = client
        except Exception as exc:
            log.warning("algorand settlement unavailable (%s); using ledger backend", exc)
            self._client = None
        return self._client

    def available(self) -> bool:
        return self._algod() is not None

    def settle(self, db: Session, payment: Payment, amount_micros: int, fee_micros: int) -> SettleResult:
        client = self._algod()
        if client is None:
            raise RuntimeError("algod unavailable")
        from algosdk import mnemonic, transaction

        # Refuse to broadcast anything denominated in another ASA: an on-chain
        # transfer is the one step that cannot be taken back.
        asset = assert_asset(payment.asset_id or payment.asset, context="algorand settlement")

        sk = mnemonic.to_private_key(settings.algorand_dispenser_mnemonic)
        sender = mnemonic.to_public_key(settings.algorand_dispenser_mnemonic)
        params = client.suggested_params()
        txn = transaction.AssetTransferTxn(
            sender=sender,
            sp=params,
            receiver=payment.pay_to,
            amt=amount_micros - fee_micros,
            index=asset,
            note=f"m2x:{payment.id}:asa:{asset}".encode(),
        )
        txid = client.send_transaction(txn.sign(sk))
        transaction.wait_for_confirmation(client, txid, 4)
        # mirror the movement in the internal ledger for reporting parity
        ledger.capture(
            db,
            payer_id=payment.payer_id,
            payee_id=payment.payee_id,
            amount=amount_micros,
            fee=fee_micros,
            job_id=payment.job_id,
            payment_id=payment.id,
        )
        return SettleResult(True, txid, payment.network, payment.payer_id, backend=self.name,
                            asset_id=asset)

    def refund(self, db: Session, payment: Payment, amount_micros: int, reason: str) -> str:
        return LedgerSettlement().refund(db, payment, amount_micros, reason)


class RemoteFacilitator:  # pragma: no cover - requires a live facilitator
    """Delegates verify/settle to an external x402 facilitator service."""

    name = "remote"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, body: dict) -> dict:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{self.base_url}{path}", json=body)
            resp.raise_for_status()
            return resp.json()

    def verify(self, payload: PaymentPayload, requirements: PaymentRequirements) -> dict:
        return self._post(
            "/verify",
            {"x402Version": settings.x402_version,
             "paymentPayload": payload.as_dict(),
             "paymentRequirements": requirements.as_dict()},
        )

    def settle(self, payload: PaymentPayload, requirements: PaymentRequirements) -> dict:
        return self._post(
            "/settle",
            {"x402Version": settings.x402_version,
             "paymentPayload": payload.as_dict(),
             "paymentRequirements": requirements.as_dict()},
        )


# --------------------------------------------------------------------------- #
# Facilitator
# --------------------------------------------------------------------------- #
class Facilitator:
    SUPPORTED_SCHEMES = ("exact",)

    def __init__(self) -> None:
        self.ledger_backend = LedgerSettlement()
        self.algorand_backend = AlgorandSettlement()
        self.remote = RemoteFacilitator(settings.x402_facilitator_url) if settings.x402_facilitator_url else None

    # -- capability advertisement (mirrors facilitator /supported) ----------
    def supported(self) -> dict:
        kinds = [
            {"scheme": s, "network": settings.x402_network, "x402Version": settings.x402_version,
             "asset": settings.x402_asset, "assetId": mandated_asset_id()}
            for s in self.SUPPORTED_SCHEMES
        ]
        return {
            "kinds": kinds,
            "asset": asset_descriptor(),
            "settlementBackend": self.active_backend().name,
            "remoteFacilitator": settings.x402_facilitator_url or None,
        }

    def active_backend(self):
        pref = settings.x402_settlement_backend
        if pref == "algorand" or (pref == "auto" and self.algorand_backend.available()):
            if self.algorand_backend.available():
                return self.algorand_backend
        return self.ledger_backend

    # -- verify -------------------------------------------------------------
    def verify(
        self,
        db: Session,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        payment: Payment,
    ) -> VerifyResult:
        if payload.scheme not in self.SUPPORTED_SCHEMES:
            return VerifyResult(False, invalid_reason=f"unsupported_scheme:{payload.scheme}")
        if payload.network != requirements.network:
            return VerifyResult(
                False, invalid_reason=f"network_mismatch:{payload.network}!={requirements.network}"
            )
        if payload.network != settings.x402_network:
            return VerifyResult(
                False, invalid_reason=f"unsupported_network:{payload.network}"
            )

        # --- asset gate ----------------------------------------------------
        # Checked before the payer is even looked up: an authorization for the
        # wrong ASA is never worth further processing.
        try:
            required_asset = assert_asset(
                requirements.asset_id, context="payment requirements"
            )
        except AssetPolicyError as exc:
            return VerifyResult(False, invalid_reason=f"asset_not_configured:{exc}")

        auth = payload.payload.get("authorization")
        signature = payload.payload.get("signature")
        if not isinstance(auth, dict) or not signature:
            return VerifyResult(False, invalid_reason="malformed_payload")

        payer = db.get(User, auth.get("from", ""))
        if payer is None:
            return VerifyResult(False, invalid_reason="unknown_payer")
        if payer.id != payment.payer_id:
            return VerifyResult(False, invalid_reason="payer_mismatch")

        if not verify_authorization_signature(auth, signature, payer.payment_secret):
            return VerifyResult(False, payer=payer.id, invalid_reason="invalid_signature")

        now = int(time.time())
        if now < int(auth.get("validAfter", 0)):
            return VerifyResult(False, payer=payer.id, invalid_reason="not_yet_valid")
        if now > int(auth.get("validBefore", 0)):
            return VerifyResult(False, payer=payer.id, invalid_reason="authorization_expired")
        if auth.get("nonce") != payment.nonce:
            return VerifyResult(False, payer=payer.id, invalid_reason="nonce_mismatch")
        if auth.get("resource") != requirements.resource:
            return VerifyResult(False, payer=payer.id, invalid_reason="resource_mismatch")
        if auth.get("to") != requirements.payTo:
            return VerifyResult(False, payer=payer.id, invalid_reason="pay_to_mismatch")

        offered_asset = normalize_asset(auth.get("asset"))
        if offered_asset is None:
            offered_asset = normalize_asset(payload.payload.get("asset"))
        if offered_asset is None:
            return VerifyResult(False, payer=payer.id, invalid_reason="asset_missing")
        if offered_asset != required_asset:
            return VerifyResult(
                False, payer=payer.id,
                invalid_reason=f"asset_mismatch:{offered_asset}!={required_asset}",
            )
        if payment.asset_id and payment.asset_id != required_asset:
            return VerifyResult(
                False, payer=payer.id,
                invalid_reason=f"asset_mismatch:{payment.asset_id}!={required_asset}",
            )
        auth_network = str(auth.get("network") or "").lower()
        if auth_network and auth_network != settings.x402_network:
            return VerifyResult(
                False, payer=payer.id,
                invalid_reason=f"network_mismatch:{auth_network}!={settings.x402_network}",
            )

        try:
            value = int(auth.get("value", "0"))
        except (TypeError, ValueError):
            return VerifyResult(False, payer=payer.id, invalid_reason="invalid_value")
        required = int(requirements.maxAmountRequired)
        if value < required:
            return VerifyResult(
                False, payer=payer.id, invalid_reason=f"insufficient_authorization:{value}<{required}"
            )

        acct = ledger.get_account(db, payer.id)
        if acct.available_micros < required:
            return VerifyResult(
                False, payer=payer.id, invalid_reason="insufficient_funds",
                amount_micros=required, asset_id=required_asset,
            )

        if self.remote is not None:  # pragma: no cover
            try:
                remote = self.remote.verify(payload, requirements)
                if not remote.get("isValid", True):
                    return VerifyResult(False, payer=payer.id,
                                        invalid_reason=remote.get("invalidReason", "remote_rejected"))
            except Exception as exc:
                log.warning("remote facilitator verify failed, using local decision: %s", exc)

        return VerifyResult(True, payer=payer.id, amount_micros=required,
                            asset_id=required_asset)

    # -- escrow / settle / refund ------------------------------------------
    def escrow(self, db: Session, payment: Payment) -> None:
        ledger.hold(db, payment.payer_id, payment.amount_micros,
                    job_id=payment.job_id, payment_id=payment.id)
        payment.status = PaymentStatus.escrowed

    def settle(self, db: Session, payment: Payment, amount_micros: int, fee_micros: int) -> SettleResult:
        # Last gate before value moves: settle only in the mandated ASA.
        asset = assert_asset(payment.asset_id or payment.asset, context="settlement")
        backend = self.active_backend()
        try:
            result = backend.settle(db, payment, amount_micros, fee_micros)
        except AssetPolicyError:
            raise
        except Exception as exc:  # pragma: no cover - chain hiccup
            log.warning("settlement via %s failed (%s); falling back to ledger", backend.name, exc)
            result = self.ledger_backend.settle(db, payment, amount_micros, fee_micros)

        result.asset_id = result.asset_id or asset
        payment.captured_micros = amount_micros
        payment.fee_micros = fee_micros
        payment.asset_id = asset
        payment.asset = str(asset)
        payment.tx_hash = result.transaction
        payment.settlement_backend = result.backend
        payment.status = PaymentStatus.settled
        payment.settled_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        payment.payload_hash = hash_object(
            {"payment": payment.id, "amount": amount_micros, "fee": fee_micros,
             "asset_id": asset, "tx": result.transaction}
        )
        return result

    def refund(self, db: Session, payment: Payment, amount_micros: int, reason: str) -> str:
        backend = self.active_backend()
        if payment.status == PaymentStatus.escrowed:
            ledger.release(db, payment.payer_id, amount_micros,
                           job_id=payment.job_id, payment_id=payment.id, memo=f"refund: {reason}")
            tx = "0x" + sha256_hex(f"release:{payment.id}:{amount_micros}:{time.time_ns()}")[:56]
        else:
            tx = backend.refund(db, payment, amount_micros, reason)
        payment.refunded_micros += amount_micros
        payment.status = (
            PaymentStatus.refunded
            if payment.refunded_micros >= payment.captured_micros or payment.captured_micros == 0
            else PaymentStatus.partially_refunded
        )
        return tx


facilitator = Facilitator()


def require_valid_payment(
    db: Session, header: str, requirements: PaymentRequirements, payment: Payment
) -> VerifyResult:
    """Decode + verify an ``X-PAYMENT`` header, raising X402Error when malformed."""
    from .protocol import decode_payment_header

    payload = decode_payment_header(header)
    result = facilitator.verify(db, payload, requirements, payment)
    if not result.is_valid:
        raise X402Error(result.invalid_reason)
    return result
