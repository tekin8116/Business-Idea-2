"""Contract-rate variance: what the agreement says versus what arrived.

This is layer two of the product. Layer one (baseline detection) needs nothing
but the remittance files and holds a payer to its own demonstrated rate. This
layer needs the practice's contracts, and in exchange produces the finding
that is hardest for a payer to argue with: the contract says one number, the
remittance says another.

Terms are expressed either as an explicit allowed amount per unit or, far more
commonly for a small practice, as a multiple of Medicare. Both are supported,
and the most specific matching term wins so that a single code carve-out can
sit on top of a blanket "150% of Medicare" without either being lost.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Sequence

from ..benchmarks.medicare import MedicareSchedule
from ..findings import Category, Confidence, Finding
from ..model import ZERO, Claim, Remittance, ServiceLine
from .grouping import is_comparable, normalize_payer

WILDCARD = "*"
DEFAULT_TOLERANCE = Decimal("0.02")


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def _parse_iso(value) -> Optional[date]:
    text = (str(value) if value is not None else "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class ContractTerm:
    """One pricing rule from a payer agreement.

    ``procedure``, ``plan_class`` and ``place_of_service`` accept ``*`` to
    match anything, which is how a blanket multiple is expressed.
    """

    payer: str
    procedure: str = WILDCARD
    plan_class: str = WILDCARD
    place_of_service: str = WILDCARD
    rate: Optional[Decimal] = None
    medicare_multiple: Optional[Decimal] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None

    def specificity(self) -> int:
        """How narrowly this term is scoped; higher wins a match contest."""
        score = 0
        if self.procedure != WILDCARD:
            score += 4
        if self.plan_class != WILDCARD:
            score += 2
        if self.place_of_service != WILDCARD:
            score += 1
        return score

    def _field_matches(self, term_value: str, actual: str) -> bool:
        return term_value == WILDCARD or term_value == (actual or "")

    def matches(
        self,
        payer: str,
        procedure: str,
        plan_class: str,
        place_of_service: str,
        service_date: Optional[date],
    ) -> bool:
        if self.payer != WILDCARD and self.payer != payer:
            return False
        if not self._field_matches(self.procedure, procedure):
            return False
        if not self._field_matches(self.plan_class, plan_class):
            return False
        if not self._field_matches(self.place_of_service, place_of_service):
            return False
        # A term with dates only governs claims inside its window. A claim
        # with no usable date is allowed through rather than dropped, since
        # excluding it would silently shrink the audit.
        if service_date is not None:
            if self.effective_from and service_date < self.effective_from:
                return False
            if self.effective_to and service_date > self.effective_to:
                return False
        return True


@dataclass
class FeeSchedule:
    """The practice's contracted terms, plus the Medicare table they price against."""

    terms: List[ContractTerm] = field(default_factory=list)
    medicare: Optional[MedicareSchedule] = None

    def add(self, term: ContractTerm) -> None:
        self.terms.append(term)

    def find_term(
        self,
        payer: str,
        procedure: str,
        plan_class: str,
        place_of_service: str,
        service_date: Optional[date],
    ) -> Optional[ContractTerm]:
        candidates = [
            t
            for t in self.terms
            if t.matches(payer, procedure, plan_class, place_of_service, service_date)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.specificity())

    def expected_per_unit(
        self,
        payer: str,
        procedure: str,
        plan_class: str,
        place_of_service: str,
        service_date: Optional[date],
    ) -> tuple[Optional[Decimal], Optional[ContractTerm], str]:
        """Expected allowed amount for one unit of a procedure.

        Returns the amount, the term it came from, and a short description of
        the basis so the report can show its working rather than asserting a
        number the practice cannot check.
        """
        term = self.find_term(
            payer, procedure, plan_class, place_of_service, service_date
        )
        if term is None:
            return None, None, ""

        if term.rate is not None:
            return term.rate, term, "contracted fee schedule"

        if term.medicare_multiple is not None:
            if self.medicare is None:
                return None, term, ""
            base = self.medicare.allowed_for(procedure, place_of_service)
            if base is None:
                return None, term, ""
            setting = (
                "facility"
                if self.medicare.is_facility(place_of_service)
                else "non-facility"
            )
            expected = (base * term.medicare_multiple).quantize(Decimal("0.01"))
            basis = (
                f"{term.medicare_multiple * 100:.0f}% of the {setting} "
                f"Medicare rate of ${base:,.2f}"
            )
            return expected, term, basis

        return None, term, ""

    @classmethod
    def from_csv(
        cls, path: str | Path, medicare: Optional[MedicareSchedule] = None
    ) -> "FeeSchedule":
        """Load contracted terms from CSV.

        Columns (case-insensitive; omit what does not apply):
            payer, procedure, plan_class, place_of_service,
            rate, medicare_multiple, effective_from, effective_to

        A practice with one blanket agreement per payer needs only two
        columns: payer and medicare_multiple.
        """
        schedule = cls(medicare=medicare)
        with Path(path).open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                lower = {
                    (k or "").lower().strip(): v for k, v in row.items()
                }
                payer = (lower.get("payer") or WILDCARD).strip().upper()
                multiple = _dec(lower.get("medicare_multiple"))
                # Accept a multiple written as "150" to mean 150%.
                if multiple is not None and multiple > 10:
                    multiple = multiple / Decimal("100")
                schedule.add(
                    ContractTerm(
                        payer=payer or WILDCARD,
                        procedure=(lower.get("procedure") or WILDCARD).strip().upper()
                        or WILDCARD,
                        plan_class=(lower.get("plan_class") or WILDCARD).strip().lower()
                        or WILDCARD,
                        place_of_service=(
                            lower.get("place_of_service") or WILDCARD
                        ).strip()
                        or WILDCARD,
                        rate=_dec(lower.get("rate")),
                        medicare_multiple=multiple,
                        effective_from=_parse_iso(lower.get("effective_from")),
                        effective_to=_parse_iso(lower.get("effective_to")),
                    )
                )
        return schedule


class ContractDetector:
    """Flags lines allowed below their contracted rate."""

    def __init__(
        self,
        schedule: FeeSchedule,
        tolerance: Decimal = DEFAULT_TOLERANCE,
    ) -> None:
        self.schedule = schedule
        self.tolerance = tolerance

    def run(self, remittances: Sequence[Remittance]) -> List[Finding]:
        findings: List[Finding] = []
        for remit in remittances:
            payer = normalize_payer(remit.payer_name, remit.payer_id)
            for claim in remit.claims:
                for line in claim.lines:
                    if not is_comparable(claim, line):
                        continue
                    finding = self._inspect(remit, payer, claim, line)
                    if finding is not None:
                        findings.append(finding)
        return findings

    def _inspect(
        self,
        remit: Remittance,
        payer: str,
        claim: Claim,
        line: ServiceLine,
    ) -> Optional[Finding]:
        expected_unit, term, basis = self.schedule.expected_per_unit(
            payer,
            line.procedure,
            claim.plan_class,
            claim.place_of_service,
            line.service_date,
        )
        if expected_unit is None or expected_unit <= ZERO:
            return None

        threshold = expected_unit * (Decimal("1") - self.tolerance)
        if line.allowed_per_unit >= threshold:
            return None

        expected_total = (expected_unit * line.units).quantize(Decimal("0.01"))
        shortfall_unit = expected_unit - line.allowed_per_unit

        return Finding(
            category=Category.CONTRACT_VARIANCE,
            confidence=Confidence.HIGH,
            payer=payer,
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
            expected_allowed=expected_total,
            units=line.units,
            rationale=(
                f"Contract calls for ${expected_unit:,.2f} per unit "
                f"({basis}); the payer allowed ${line.allowed_per_unit:,.2f} "
                f"- ${shortfall_unit:,.2f} short per unit."
            ),
            reason_codes=line.reason_codes,
            source_file=remit.source_file,
            evidence={
                "contracted_per_unit": f"{expected_unit:.2f}",
                "observed_per_unit": f"{line.allowed_per_unit:.2f}",
                "basis": basis or "contracted fee schedule",
            },
        )
