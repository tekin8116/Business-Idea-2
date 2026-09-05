"""Findings: what the detectors produce and how they are ranked.

A finding is a single claim line the practice should look at, carrying enough
context to work it without going back to the source file: what was paid, what
should have been paid, why we think so, and how long they have to act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from .model import ZERO

# Days from the remittance date in which a dispute must be filed. These are
# conservative defaults: real windows come from the practice's own contracts,
# and a shorter estimate is the safe error to make. Overridden per payer via
# configuration once contracts are on hand.
DEFAULT_APPEAL_WINDOW_DAYS = 90
APPEAL_WINDOW_BY_PLAN: Dict[str, int] = {
    # Medicare redetermination runs 120 days from the initial determination.
    "medicare": 120,
    "medicaid": 90,
    "commercial": 90,
    "unknown": 90,
}

# Below this, a variance is not worth a staff member's time to appeal.
MATERIALITY_FLOOR = Decimal("5.00")


class Category(str, Enum):
    """What kind of problem a finding represents."""

    RATE_DRIFT = "rate_drift"
    RATE_CHANGE = "rate_change"
    CONTRACT_VARIANCE = "contract_variance"
    SUSPECT_BUNDLING = "suspect_bundling"
    ZERO_ALLOWED = "zero_allowed"


class Confidence(str, Enum):
    """How much evidence stands behind a finding.

    Drives report ordering and, more importantly, honesty: a variance backed
    by forty prior payments at a higher rate is a different claim than one
    backed by four.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_CONFIDENCE_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


@dataclass
class Finding:
    """One underpaid or suspiciously adjudicated service line."""

    category: Category
    confidence: Confidence
    payer: str                     # normalised grouping key
    payer_display: str = ""        # payer name as it appears on the remittance
    procedure: str = ""
    modifiers: List[str] = field(default_factory=list)

    claim_id: str = ""
    payer_claim_id: str = ""
    rendering_npi: str = ""
    place_of_service: str = ""
    plan_class: str = "unknown"

    service_date: Optional[date] = None
    payment_date: Optional[date] = None

    actual_allowed: Decimal = ZERO
    expected_allowed: Decimal = ZERO
    units: Decimal = Decimal("1")

    rationale: str = ""
    evidence: Dict[str, str] = field(default_factory=dict)
    reason_codes: List[str] = field(default_factory=list)
    source_file: str = ""

    @property
    def payer_label(self) -> str:
        """Name to print. Falls back to the grouping key when unknown."""
        return self.payer_display or self.payer

    @property
    def shortfall(self) -> Decimal:
        """Dollars believed to be owed on this line. Never negative."""
        gap = self.expected_allowed - self.actual_allowed
        return gap if gap > ZERO else ZERO

    @property
    def procedure_key(self) -> str:
        mods = "-".join(sorted(m for m in self.modifiers if m))
        return f"{self.procedure}:{mods}" if mods else self.procedure

    @property
    def appeal_deadline(self) -> Optional[date]:
        """Last day to dispute, measured from the remittance date."""
        if self.payment_date is None:
            return None
        window = APPEAL_WINDOW_BY_PLAN.get(
            self.plan_class, DEFAULT_APPEAL_WINDOW_DAYS
        )
        return self.payment_date + timedelta(days=window)

    def days_remaining(self, as_of: Optional[date] = None) -> Optional[int]:
        """Days left to appeal. Negative once the window has closed."""
        deadline = self.appeal_deadline
        if deadline is None:
            return None
        today = as_of or date.today()
        return (deadline - today).days

    def is_appealable(self, as_of: Optional[date] = None) -> bool:
        remaining = self.days_remaining(as_of)
        return remaining is not None and remaining >= 0

    def is_material(self) -> bool:
        return self.shortfall >= MATERIALITY_FLOOR


def rank_findings(
    findings: List[Finding], as_of: Optional[date] = None
) -> List[Finding]:
    """Order findings the way a biller should work them.

    Appealable claims come first regardless of size, because an expired claim
    is worth nothing no matter how large. Within that, urgency and confidence
    beat raw dollars: a $400 line expiring in nine days outranks a $900 line
    with three months left.
    """
    today = as_of or date.today()

    def sort_key(f: Finding):
        remaining = f.days_remaining(today)
        expired = remaining is not None and remaining < 0
        # Unknown deadlines sort as "plenty of time" rather than as urgent, to
        # avoid a missing date manufacturing false urgency.
        urgency = remaining if remaining is not None else 10_000
        return (
            expired,
            _CONFIDENCE_RANK[f.confidence],
            urgency,
            -f.shortfall,
        )

    return sorted(findings, key=sort_key)


def total_shortfall(findings: List[Finding]) -> Decimal:
    return sum((f.shortfall for f in findings), ZERO)


def recoverable_shortfall(
    findings: List[Finding], as_of: Optional[date] = None
) -> Decimal:
    """Shortfall on lines that can still actually be appealed.

    This is the number that belongs at the top of a report. Total shortfall
    including expired claims overstates what the practice can get back, and
    overstating it once destroys the credibility the whole engagement rests on.
    """
    return sum(
        (f.shortfall for f in findings if f.is_appealable(as_of)), ZERO
    )
