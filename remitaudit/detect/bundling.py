"""Detects adjudication that looks wrong on its face, regardless of rate.

Rate detection catches money shaved off a correctly-processed line. This
catches the other failure mode: a line the payer processed under the wrong
rule entirely - bundled into another service, treated as a duplicate, or
reduced under multiple-procedure logic that its modifier should have
prevented.

The interventional cardiology add-on codes are called out explicitly because
they are the classic case. IVUS, FFR, and thrombectomy performed alongside an
intervention are separately payable by design; a CO-97 "included in another
service" on one of them is very often the payer misapplying a bundling edit,
and it recurs claim after claim until somebody appeals it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Dict, List, Optional, Sequence

from ..findings import Category, Confidence, Finding
from ..model import ZERO, Claim, Remittance, ServiceLine
from .grouping import RateKey, normalize_payer, rate_key


@dataclass(frozen=True)
class SuspectReason:
    """A CARC worth a second look, and what to say about it."""

    code: str
    label: str
    note: str
    confidence: Confidence


# Claim Adjustment Reason Codes that frequently indicate misadjudication
# rather than a legitimate contractual reduction. CARC 45 (charge exceeds fee
# schedule) is deliberately absent: that is the normal contractual write-off
# and flagging it would bury the real findings in noise.
SUSPECT_REASONS: Dict[str, SuspectReason] = {
    r.code: r
    for r in (
        SuspectReason(
            "97",
            "Bundled into another service",
            "Payment was folded into another line. Legitimate for components "
            "of a single procedure, frequently wrong for designated add-on "
            "codes and for distinct services carrying a -59/-XS modifier.",
            Confidence.MEDIUM,
        ),
        SuspectReason(
            "B15",
            "Qualifying service not found",
            "The payer could not match a required primary procedure. Usually "
            "a sequencing or same-day-claim-split problem rather than a real "
            "coverage denial.",
            Confidence.MEDIUM,
        ),
        SuspectReason(
            "59",
            "Multiple procedure reduction",
            "Reduced under multiple or concurrent procedure rules. Check "
            "whether a distinct-service modifier should have exempted it, and "
            "whether the reduction percentage matches the contract.",
            Confidence.LOW,
        ),
        SuspectReason(
            "18",
            "Treated as an exact duplicate",
            "Commonly misapplied to bilateral procedures and to a service "
            "legitimately repeated on the same day.",
            Confidence.MEDIUM,
        ),
        SuspectReason(
            "4",
            "Modifier inconsistent with procedure",
            "A coding-edit rejection. Frequently correctable and re-billable "
            "rather than a true denial.",
            Confidence.LOW,
        ),
        SuspectReason(
            "151",
            "Frequency not supported",
            "The payer decided the documentation did not support this many "
            "units. Appealable with the operative note when the units are real.",
            Confidence.LOW,
        ),
        SuspectReason(
            "234",
            "Not paid separately",
            "The payer treats this as non-separately-payable. Verify against "
            "the contract and the add-on status of the code.",
            Confidence.MEDIUM,
        ),
        SuspectReason(
            "197",
            "Precertification absent",
            "Often appealable where authorization exists but was attached to "
            "a different claim or obtained retroactively.",
            Confidence.LOW,
        ),
    )
}

# Codes that are designated add-ons: separately payable alongside a primary
# procedure. A bundling denial on one of these deserves a hard look.
CARDIOLOGY_ADD_ON_CODES = frozenset(
    {
        "92920", "92921",              # angioplasty, additional branch
        "92928", "92929",              # stent placement, additional branch
        "92933", "92934",              # atherectomy with stent
        "92937", "92938",              # bypass graft intervention
        "92943", "92944",              # chronic total occlusion
        "92973",                       # mechanical thrombectomy
        "92974",                       # brachytherapy catheter
        "92978", "92979",              # intravascular ultrasound
        "93563", "93564", "93565",     # selective angiography add-ons
        "93566", "93567", "93568",
        "93571", "93572",              # intravascular Doppler / FFR
    }
)

# The minimum estimate quality we will attach a dollar figure to.
_MIN_RATE_OBSERVATIONS = 3


class BundlingDetector:
    """Flags lines whose adjustment reasons suggest misadjudication."""

    def __init__(
        self,
        add_on_codes: frozenset[str] = CARDIOLOGY_ADD_ON_CODES,
        suspect_reasons: Optional[Dict[str, SuspectReason]] = None,
    ) -> None:
        self.add_on_codes = add_on_codes
        self.suspect_reasons = suspect_reasons or SUSPECT_REASONS

    def _reference_rates(
        self, remittances: Sequence[Remittance]
    ) -> Dict[RateKey, Decimal]:
        """Typical allowed-per-unit for each bucket, from lines that did pay.

        Used only to put a dollar estimate on a bundled line: if this code
        normally allows $1,840 from this payer, that is what the denial is
        plausibly worth. Estimates are omitted rather than guessed when the
        practice has too few paid examples.
        """
        collected: Dict[RateKey, List[Decimal]] = defaultdict(list)
        for remit in remittances:
            for claim in remit.claims:
                if claim.is_denied:
                    continue
                for line in claim.lines:
                    if not line.procedure or line.is_zero_paid:
                        continue
                    collected[rate_key(remit, claim, line)].append(
                        line.allowed_per_unit
                    )
        return {
            key: Decimal(str(median(values))).quantize(Decimal("0.01"))
            for key, values in collected.items()
            if len(values) >= _MIN_RATE_OBSERVATIONS
        }

    def run(self, remittances: Sequence[Remittance]) -> List[Finding]:
        rates = self._reference_rates(remittances)
        findings: List[Finding] = []

        for remit in remittances:
            for claim in remit.claims:
                for line in claim.lines:
                    if not line.procedure:
                        continue
                    finding = self._inspect(remit, claim, line, rates)
                    if finding is not None:
                        findings.append(finding)
        return findings

    def _inspect(
        self,
        remit: Remittance,
        claim: Claim,
        line: ServiceLine,
        rates: Dict[RateKey, Decimal],
    ) -> Optional[Finding]:
        hits = [
            self.suspect_reasons[a.reason]
            for a in line.adjustments
            if a.reason in self.suspect_reasons and a.is_contractual
        ]
        if not hits:
            return None

        # Rank by how strongly the reason implies an error.
        order = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
        primary = sorted(hits, key=lambda r: order[r.confidence])[0]

        is_add_on = line.procedure in self.add_on_codes
        confidence = primary.confidence
        rationale = f"{primary.label}. {primary.note}"

        if is_add_on and primary.code in ("97", "234", "B15"):
            # An add-on code denied as bundled is the highest-yield pattern in
            # this whole detector, and it repeats across every similar claim.
            confidence = Confidence.HIGH
            rationale = (
                f"{line.procedure} is a designated add-on code, separately "
                f"payable alongside its primary procedure, but was adjudicated "
                f"as '{primary.label.lower()}'. This is the classic bundling "
                f"edit misfire and it will recur on every comparable claim "
                f"until it is disputed."
            )

        key = rate_key(remit, claim, line)
        reference = rates.get(key)
        expected = (
            reference * line.units if reference is not None else line.allowed
        )
        # Never claim a recovery larger than what was billed.
        if expected > line.charge > ZERO:
            expected = line.charge

        if reference is None:
            rationale += (
                " No dollar estimate is shown: this practice has too few paid "
                "examples of this code with this payer to price it."
            )

        return Finding(
            category=Category.ZERO_ALLOWED
            if line.is_zero_paid
            else Category.SUSPECT_BUNDLING,
            confidence=confidence,
            payer=normalize_payer(remit.payer_name, remit.payer_id),
            payer_display=remit.payer_name,
            procedure=line.procedure,
            modifiers=list(line.modifiers),
            claim_id=claim.claim_id,
            payer_claim_id=claim.payer_claim_id,
            rendering_npi=claim.rendering_npi,
            place_of_service=claim.place_of_service,
            plan_class=claim.plan_class,
            service_date=line.service_date,
            payment_date=remit.payment_date,
            actual_allowed=line.allowed,
            expected_allowed=expected,
            units=line.units,
            rationale=rationale,
            reason_codes=line.reason_codes,
            source_file=remit.source_file,
            evidence={
                "carc": primary.code,
                "add_on_code": "yes" if is_add_on else "no",
                "typical_allowed": (
                    f"{reference:.2f}" if reference is not None else "unknown"
                ),
                "basis": "adjustment reason code review",
            },
        )
