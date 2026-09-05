"""Rate baseline detection - finding underpayments with no contract in hand.

This is the detector that makes the product deliverable on day one. A practice
can rarely produce its fee schedules on request; it can always produce its
remittance files. So rather than asking what the contract says, this asks what
the payer *itself* has demonstrably paid for the same work, and treats its own
best-supported rate as the standard it should be held to.

That framing is also what makes a finding appealable. "Your contract says
$612.40" invites an argument about which amendment governs. "You paid $612.40
for this code on 23 other claims this year, and $551.16 on these 14" is not
really arguable - the payer's own adjudication history is the evidence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

from ..findings import Category, Confidence, Finding
from ..model import ZERO, Claim, Remittance, ServiceLine
from .grouping import RateKey, is_comparable, rate_key

# How far below baseline a line must fall before it is worth reporting.
# Small enough to catch a real rate cut, large enough to absorb rounding and
# penny-level differences in how payers compute coinsurance.
DEFAULT_TOLERANCE = Decimal("0.02")

# Distinct payments at the same amount before that amount is credible as a
# contracted rate rather than a coincidence.
DEFAULT_MIN_SUPPORT = 3

# A step change is evidenced differently from a recurring rate. The old rate
# needs fewer observations to be credible, because a clean temporal break -
# every payment before at one amount, every payment after at a lower one - is
# itself the evidence. Requiring full support on the earlier side means a cut
# that lands early in the data set is invisible precisely when the practice
# most needs to see it.
DEFAULT_CHANGE_MIN_BEFORE = 2
DEFAULT_CHANGE_MIN_AFTER = 3

# Observations needed in a bucket before any conclusion is drawn at all.
DEFAULT_MIN_OBSERVATIONS = 4

# Support levels at which a baseline is considered strongly evidenced.
_HIGH_CONFIDENCE_SUPPORT = 8

# A rate change must be at least this large, and this consistent, to report.
_RATE_CHANGE_MIN_DROP = Decimal("0.03")
_RATE_CHANGE_CONSISTENCY = 0.8


@dataclass
class Observation:
    """One adjudicated line, reduced to what the rate maths needs."""

    remit: Remittance
    claim: Claim
    line: ServiceLine
    allowed_per_unit: Decimal
    paid_on: Optional[date]


@dataclass
class RateChange:
    """A step change in what a payer allows for one procedure.

    The single most persuasive artifact this tool produces: a dated, sourced
    statement that a payer quietly repriced a code mid-year.
    """

    key: RateKey
    old_rate: Decimal
    new_rate: Decimal
    changed_on: date
    observations_before: int
    observations_after: int
    affected_lines: int
    exposure: Decimal = ZERO
    payer_display: str = ""

    @property
    def payer_label(self) -> str:
        return self.payer_display or self.key.payer

    @property
    def drop_pct(self) -> Decimal:
        if self.old_rate <= ZERO:
            return ZERO
        return (self.old_rate - self.new_rate) / self.old_rate

    def describe(self) -> str:
        pct = self.drop_pct * 100
        return (
            f"{self.payer_label} allowed ${self.old_rate:,.2f} per unit for "
            f"{self.key.procedure} across {self.observations_before} payments, "
            f"then ${self.new_rate:,.2f} on {self.observations_after} payments "
            f"from {self.changed_on:%b %d, %Y} onward - a {pct:.1f}% reduction."
        )


def _round_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _baseline_from(
    values: Sequence[Decimal], min_support: int
) -> Tuple[Optional[Decimal], int, bool]:
    """Derive the rate a payer should be held to.

    Returns (baseline, supporting_count, is_well_supported).

    Contracted rates are discrete: the same number recurs exactly, payment
    after payment. So the strongest signal is the *highest* amount that recurs
    often enough to be a real rate rather than an outlier - that is proof the
    payer both can and does pay it. When nothing recurs (fee schedules with
    per-claim variation, or a thin sample), fall back to the median and mark
    the result weakly supported so it is reported with appropriate hedging.
    """
    if not values:
        return None, 0, False

    counts: Dict[Decimal, int] = defaultdict(int)
    for value in values:
        counts[_round_cents(value)] += 1

    supported = [rate for rate, n in counts.items() if n >= min_support]
    if supported:
        baseline = max(supported)
        return baseline, counts[baseline], True

    return _round_cents(Decimal(str(median(values)))), 0, False


def _detect_rate_change(
    observations: List[Observation],
    min_before: int = DEFAULT_CHANGE_MIN_BEFORE,
    min_after: int = DEFAULT_CHANGE_MIN_AFTER,
) -> Optional[Tuple[Decimal, Decimal, date, int, int]]:
    """Find a dated step down in the allowed rate.

    Walks every split point in the time-ordered series and picks the one with
    the largest drop between the median before and the median after, then
    requires that the drop be both material and consistent - most payments
    after the split must actually sit below the earlier rate. Without the
    consistency test, one cheap claim in a noisy series reads as a rate cut.
    """
    dated = [o for o in observations if o.paid_on is not None]
    if len(dated) < min_before + min_after:
        return None

    dated.sort(key=lambda o: o.paid_on)
    values = [o.allowed_per_unit for o in dated]

    best: Optional[Tuple[Decimal, Decimal, date, int, int]] = None
    best_drop = ZERO

    for split in range(min_before, len(dated) - min_after + 1):
        before = values[:split]
        after = values[split:]
        old_rate = _round_cents(Decimal(str(median(before))))
        new_rate = _round_cents(Decimal(str(median(after)))) 
        if old_rate <= ZERO:
            continue
        drop = (old_rate - new_rate) / old_rate
        if drop <= best_drop:
            continue
        consistent = sum(1 for v in after if v < old_rate) / len(after)
        if consistent < _RATE_CHANGE_CONSISTENCY:
            continue
        best_drop = drop
        best = (
            old_rate,
            new_rate,
            dated[split].paid_on,
            len(before),
            len(after),
        )

    if best is None or best_drop < _RATE_CHANGE_MIN_DROP:
        return None
    return best


class BaselineDetector:
    """Flags lines paid below the payer's own demonstrated rate."""

    def __init__(
        self,
        tolerance: Decimal = DEFAULT_TOLERANCE,
        min_support: int = DEFAULT_MIN_SUPPORT,
        min_observations: int = DEFAULT_MIN_OBSERVATIONS,
        change_min_before: int = DEFAULT_CHANGE_MIN_BEFORE,
        change_min_after: int = DEFAULT_CHANGE_MIN_AFTER,
    ) -> None:
        self.tolerance = tolerance
        self.min_support = min_support
        self.min_observations = min_observations
        self.change_min_before = change_min_before
        self.change_min_after = change_min_after
        self.rate_changes: List[RateChange] = []

    def _bucket(
        self, remittances: Sequence[Remittance]
    ) -> Dict[RateKey, List[Observation]]:
        buckets: Dict[RateKey, List[Observation]] = defaultdict(list)
        for remit in remittances:
            for claim in remit.claims:
                for line in claim.lines:
                    if not is_comparable(claim, line):
                        continue
                    buckets[rate_key(remit, claim, line)].append(
                        Observation(
                            remit=remit,
                            claim=claim,
                            line=line,
                            allowed_per_unit=line.allowed_per_unit,
                            paid_on=remit.payment_date,
                        )
                    )
        return buckets

    def run(self, remittances: Sequence[Remittance]) -> List[Finding]:
        self.rate_changes = []
        findings: List[Finding] = []

        for key, observations in self._bucket(remittances).items():
            if len(observations) < self.min_observations:
                continue

            values = [o.allowed_per_unit for o in observations]

            # Look for a dated step down first. When one exists it *defines*
            # the baseline: the rate in force before the cut is what the payer
            # should still be allowing. Deriving the baseline from the whole
            # series instead lets a large enough post-cut sample outvote the
            # original rate and hide the cut completely.
            change = _detect_rate_change(
                observations, self.change_min_before, self.change_min_after
            )
            if change is not None:
                baseline = change[0]
                support = change[3]
                well_supported = True
            else:
                baseline, support, well_supported = _baseline_from(
                    values, self.min_support
                )
            if baseline is None or baseline <= ZERO:
                continue

            threshold = baseline * (Decimal("1") - self.tolerance)

            underpaid = [
                o for o in observations if o.allowed_per_unit < threshold
            ]
            if not underpaid:
                continue

            if change is not None:
                old_rate, new_rate, changed_on, n_before, n_after = change
                exposure = sum(
                    (
                        (old_rate - o.allowed_per_unit) * o.line.units
                        for o in underpaid
                    ),
                    ZERO,
                )
                self.rate_changes.append(
                    RateChange(
                        key=key,
                        old_rate=old_rate,
                        new_rate=new_rate,
                        changed_on=changed_on,
                        observations_before=n_before,
                        observations_after=n_after,
                        affected_lines=len(underpaid),
                        exposure=exposure,
                        payer_display=underpaid[0].remit.payer_name,
                    )
                )

            for obs in underpaid:
                findings.append(
                    self._build_finding(
                        key, obs, baseline, support, well_supported, change
                    )
                )

        return findings

    def _build_finding(
        self,
        key: RateKey,
        obs: Observation,
        baseline: Decimal,
        support: int,
        well_supported: bool,
        change,
    ) -> Finding:
        shortfall_per_unit = baseline - obs.allowed_per_unit
        expected = baseline * obs.line.units

        if change is not None:
            # Evidence here is the step itself, so grade on how much history
            # sits on the earlier side of it.
            confidence = (
                Confidence.HIGH if support >= 3 else Confidence.MEDIUM
            )
            rationale = (
                f"{obs.remit.payer_name or key.payer} allowed "
                f"${baseline:,.2f} per unit for this code "
                f"before the rate stepped down; this line was allowed "
                f"${obs.allowed_per_unit:,.2f}."
            )
        elif not well_supported:
            confidence = Confidence.LOW
            rationale = (
                f"Allowed ${obs.allowed_per_unit:,.2f} per unit against a "
                f"median of ${baseline:,.2f} for this code with this payer. "
                f"No single rate recurs often enough to confirm a contracted "
                f"amount, so treat this as a lead rather than a proven "
                f"underpayment."
            )
        else:
            confidence = (
                Confidence.HIGH
                if support >= _HIGH_CONFIDENCE_SUPPORT
                else Confidence.MEDIUM
            )
            rationale = (
                f"{obs.remit.payer_name or key.payer} allowed "
                f"${baseline:,.2f} per unit for this code "
                f"on {support} other payments, but allowed "
                f"${obs.allowed_per_unit:,.2f} here - a shortfall of "
                f"${shortfall_per_unit:,.2f} per unit."
            )

        if change is not None:
            old_rate, new_rate, changed_on, n_before, n_after = change
            rationale += (
                f" The rate stepped down from ${old_rate:,.2f} to "
                f"${new_rate:,.2f} on {changed_on:%b %d, %Y}, across "
                f"{n_before} payments before and {n_after} after."
            )
            category = Category.RATE_CHANGE
        else:
            category = Category.RATE_DRIFT

        return Finding(
            category=category,
            confidence=confidence,
            payer=key.payer,
            payer_display=obs.remit.payer_name,
            procedure=obs.line.procedure,
            modifiers=list(obs.line.modifiers),
            claim_id=obs.claim.claim_id,
            payer_claim_id=obs.claim.payer_claim_id,
            rendering_npi=obs.claim.rendering_npi,
            place_of_service=obs.claim.place_of_service,
            plan_class=obs.claim.plan_class,
            service_date=obs.line.service_date,
            payment_date=obs.remit.payment_date,
            actual_allowed=obs.line.allowed,
            expected_allowed=expected,
            units=obs.line.units,
            rationale=rationale,
            reason_codes=obs.line.reason_codes,
            source_file=obs.remit.source_file,
            evidence={
                "baseline_per_unit": f"{baseline:.2f}",
                "observed_per_unit": f"{obs.allowed_per_unit:.2f}",
                "supporting_payments": str(support),
                "basis": "payer's own adjudication history",
            },
        )
