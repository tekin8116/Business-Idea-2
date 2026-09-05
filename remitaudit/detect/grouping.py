"""How adjudicated lines are bucketed before they are compared.

Everything downstream depends on comparing like with like. Two payments for
the same CPT code are only comparable if they came from the same payer, under
the same class of plan, in the same place of service, with the same modifiers.
Get this wrong and the tool reports variance that is really just two different
contracts sitting in one bucket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..model import Claim, Remittance, ServiceLine

# Corporate suffixes and plan-line noise that make one payer look like several.
_PAYER_NOISE = re.compile(
    r"\b(INC|LLC|LTD|CORP|CORPORATION|COMPANY|CO|THE|OF|GROUP|PLAN|PLANS"
    r"|INSURANCE|INS|HEALTHCARE|HEALTH ?CARE|HEALTH|ASSURANCE)\b"
)
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize_payer(name: str, payer_id: str = "") -> str:
    """Collapse payer name variants onto one stable key.

    'AETNA', 'Aetna Inc.', and 'AETNA HEALTH INC' are one contracting entity
    and must share a baseline. A payer id, when the remittance carries one, is
    authoritative and skips the guesswork entirely.
    """
    if payer_id and payer_id.strip():
        return payer_id.strip().upper()
    text = _NON_ALNUM.sub(" ", (name or "").upper())
    text = _PAYER_NOISE.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return text or "UNKNOWN PAYER"


@dataclass(frozen=True)
class RateKey:
    """The bucket a line belongs to for rate comparison."""

    payer: str
    plan_class: str
    place_of_service: str
    procedure: str

    def label(self) -> str:
        pos = f" @POS {self.place_of_service}" if self.place_of_service else ""
        return f"{self.payer} / {self.plan_class} / {self.procedure}{pos}"


def rate_key(remit: Remittance, claim: Claim, line: ServiceLine) -> RateKey:
    return RateKey(
        payer=normalize_payer(remit.payer_name, remit.payer_id),
        plan_class=claim.plan_class,
        place_of_service=claim.place_of_service or "",
        procedure=line.key(),
    )


def is_comparable(claim: Claim, line: ServiceLine) -> bool:
    """Whether a line may contribute to a rate baseline.

    Denials, reversals, and zero-allowed lines are excluded: they are real
    problems, but folding them into a baseline drags the expected rate toward
    zero and hides the very variance we are looking for. They are picked up
    separately by the bundling and zero-allowed detectors.
    """
    if not line.procedure:
        return False
    if claim.is_denied:
        return False
    if line.is_zero_paid:
        return False
    if line.units <= 0:
        return False
    return True


def payment_date_of(remit: Remittance, claim: Claim) -> Optional[object]:
    """Best available date for ordering and deadline maths."""
    return remit.payment_date or claim.service_to or claim.service_from
