"""Domain model for remittance data.

Shape mirrors the 835 itself: a Remittance (one payer deposit) holds Claims,
which hold ServiceLines, which hold Adjustments. Detection operates almost
entirely at the service-line level, because that is where a contracted rate
actually applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterator, List, Optional

# CAS group codes.
GROUP_CONTRACTUAL = "CO"  # payer write-off under the contract
GROUP_PATIENT = "PR"      # deductible, coinsurance, copay
GROUP_OTHER = "OA"
GROUP_PAYER_INITIATED = "PI"

ZERO = Decimal("0.00")


@dataclass
class Adjustment:
    """One CAS triplet: why the payer reduced the line, and by how much."""

    group: str
    reason: str
    amount: Decimal
    quantity: Optional[Decimal] = None

    @property
    def is_patient_responsibility(self) -> bool:
        return self.group == GROUP_PATIENT

    @property
    def is_contractual(self) -> bool:
        return self.group in (GROUP_CONTRACTUAL, GROUP_PAYER_INITIATED)


@dataclass
class ServiceLine:
    """A single procedure on a claim, as adjudicated by the payer."""

    procedure: str                      # CPT / HCPCS
    modifiers: List[str] = field(default_factory=list)
    charge: Decimal = ZERO              # SVC02, what was billed
    paid: Decimal = ZERO                # SVC03, what the payer sent
    units: Decimal = Decimal("1")       # SVC05
    service_date: Optional[date] = None
    adjustments: List[Adjustment] = field(default_factory=list)
    reported_allowed: Optional[Decimal] = None  # AMT*B6 when the payer sends it

    @property
    def patient_responsibility(self) -> Decimal:
        return sum(
            (a.amount for a in self.adjustments if a.is_patient_responsibility),
            ZERO,
        )

    @property
    def contractual_adjustment(self) -> Decimal:
        return sum((a.amount for a in self.adjustments if a.is_contractual), ZERO)

    @property
    def allowed(self) -> Decimal:
        """The contracted allowed amount for this line.

        This, not ``paid``, is the number a contract governs: paid is simply
        allowed minus whatever the patient owes. Comparing paid against a fee
        schedule produces false positives on every high-deductible plan.

        Prefer the payer's own AMT*B6 when present; otherwise reconstruct it
        as paid plus patient responsibility, which is the identity the 835
        balancing rules guarantee.
        """
        if self.reported_allowed is not None:
            return self.reported_allowed
        return self.paid + self.patient_responsibility

    @property
    def allowed_per_unit(self) -> Decimal:
        """Allowed amount normalised by units billed.

        Without this, a line billed with 3 units looks like a windfall next to
        the same code billed with 1, and drift detection drowns in noise.
        """
        if self.units and self.units != 0:
            return self.allowed / self.units
        return self.allowed

    @property
    def reason_codes(self) -> List[str]:
        return [a.reason for a in self.adjustments]

    @property
    def is_zero_paid(self) -> bool:
        return self.allowed <= ZERO

    def key(self) -> str:
        """Grouping key for comparing like against like.

        Modifiers are part of the key because they change the contracted rate:
        a -26 professional component is a different price than the global
        service, and blending them would manufacture variance that isn't real.
        """
        mods = "-".join(sorted(m for m in self.modifiers if m))
        return f"{self.procedure}:{mods}" if mods else self.procedure


@dataclass
class Claim:
    """One claim as adjudicated, holding its service lines."""

    claim_id: str = ""                 # CLP01, the practice's account number
    payer_claim_id: str = ""           # CLP07, needed to file an appeal
    status_code: str = ""              # CLP02
    filing_indicator: str = ""         # CLP06
    charge: Decimal = ZERO
    paid: Decimal = ZERO
    patient_responsibility: Decimal = ZERO
    place_of_service: str = ""         # CLP08
    rendering_npi: str = ""
    service_from: Optional[date] = None
    service_to: Optional[date] = None
    lines: List[ServiceLine] = field(default_factory=list)

    @property
    def plan_class(self) -> str:
        """Coarse plan type, used to keep unlike contracts out of one bucket.

        CLP06 distinguishes a payer's commercial book (CI/HM/12-16) from its
        Medicare Advantage (MA/MB) and Medicaid (MC) books, which are priced
        under entirely different contracts. Without this split, an Aetna
        commercial rate and an Aetna MA rate land in the same baseline and the
        detector reports variance that is really just two different contracts.
        """
        code = (self.filing_indicator or "").upper()
        if code in ("MA", "MB"):
            return "medicare"
        if code == "MC":
            return "medicaid"
        if code in ("CI", "HM", "BL", "CH", "12", "13", "14", "15", "16", "17"):
            return "commercial"
        return code or "unknown"

    @property
    def is_denied(self) -> bool:
        # CLP02 = 4 is "denied"; 22 is a reversal of a prior payment.
        return self.status_code in ("4", "22")

    @property
    def allowed(self) -> Decimal:
        return sum((line.allowed for line in self.lines), ZERO)


@dataclass
class Remittance:
    """One payer deposit: an ST/SE transaction set within an 835 file."""

    payer_name: str = ""
    payer_id: str = ""
    payee_name: str = ""
    payee_npi: str = ""
    total_paid: Decimal = ZERO
    payment_date: Optional[date] = None   # BPR16, when the money moved
    trace_number: str = ""                # TRN02, the check / EFT number
    claims: List[Claim] = field(default_factory=list)
    source_file: str = ""

    def iter_lines(self) -> Iterator[tuple["Remittance", Claim, ServiceLine]]:
        for claim in self.claims:
            for line in claim.lines:
                yield self, claim, line

    @property
    def line_count(self) -> int:
        return sum(len(c.lines) for c in self.claims)


def iter_all_lines(
    remittances: List[Remittance],
) -> Iterator[tuple[Remittance, Claim, ServiceLine]]:
    """Flatten a batch of remittances into one stream of adjudicated lines."""
    for remit in remittances:
        yield from remit.iter_lines()
