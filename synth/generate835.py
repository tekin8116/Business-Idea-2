"""Generate realistic 835 remittance files with known, planted defects.

Two jobs. First, development: an underpayment detector cannot be trusted until
it has been pointed at data whose right answer is known in advance, and no
real remittance file comes with an answer key. Second, demonstration: this
produces a runnable example of the whole pipeline without exposing a single
byte of anyone's real claims data.

The clinical picture is an interventional cardiology practice - diagnostic
catheterisation, PCI with stents, the add-on codes that ride along with them,
plus the office visits and echoes that fill the rest of the schedule.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Clinical and contractual reference data
# --------------------------------------------------------------------------

# (code, description, medicare_allowed, is_add_on)
PROCEDURES: Dict[str, Tuple[str, Decimal, bool]] = {
    "93458": ("Left heart cath with coronary angiography", Decimal("262.40"), False),
    "93460": ("Right and left heart cath with angiography", Decimal("401.15"), False),
    "92928": ("Coronary stent, single major vessel", Decimal("621.80"), False),
    "92929": ("Coronary stent, each additional branch", Decimal("154.90"), True),
    "92920": ("Coronary angioplasty, single vessel", Decimal("538.60"), False),
    "92941": ("PCI during acute myocardial infarction", Decimal("781.25"), False),
    "92978": ("Intravascular ultrasound, initial vessel", Decimal("181.30"), True),
    "93571": ("Intravascular Doppler / FFR, initial vessel", Decimal("129.75"), True),
    "92973": ("Percutaneous mechanical thrombectomy", Decimal("212.40"), True),
    "93306": ("Echocardiography, complete with Doppler", Decimal("231.90"), False),
    "93000": ("Electrocardiogram, complete", Decimal("18.20"), False),
    "99214": ("Office visit, established patient, moderate", Decimal("128.75"), False),
    "99215": ("Office visit, established patient, high", Decimal("184.30"), False),
}

# Add-on codes are billed only alongside an intervention.
PRIMARY_INTERVENTIONS = ["92928", "92920", "92941"]
DIAGNOSTIC_CODES = ["93458", "93460"]
OFFICE_CODES = ["99214", "99215", "93306", "93000"]

# payer name -> (payer id, plan class filing indicator, medicare multiple)
PAYERS: Dict[str, Tuple[str, str, Decimal]] = {
    "AETNA": ("60054", "CI", Decimal("1.62")),
    "UNITED HEALTHCARE": ("87726", "CI", Decimal("1.48")),
    "BLUE CROSS BLUE SHIELD": ("00590", "CI", Decimal("1.71")),
    "MEDICARE PART B": ("00882", "MB", Decimal("1.00")),
}

# Place of service: 11 office, 22 hospital outpatient, 21 inpatient.
OFFICE_POS = "11"
FACILITY_POS = "22"


@dataclass
class PlantedDefect:
    """Ground truth for one deliberately introduced problem."""

    kind: str
    payer: str
    procedure: str
    claim_id: str
    service_date: str
    correct_allowed: str
    actual_allowed: str

    @property
    def shortfall(self) -> Decimal:
        return Decimal(self.correct_allowed) - Decimal(self.actual_allowed)


@dataclass
class GeneratedLine:
    procedure: str
    modifiers: List[str]
    charge: Decimal
    allowed: Decimal
    patient_responsibility: Decimal
    units: int = 1
    extra_reason: Optional[Tuple[str, str]] = None  # (group, CARC)

    @property
    def paid(self) -> Decimal:
        return self.allowed - self.patient_responsibility


@dataclass
class GeneratedClaim:
    claim_id: str
    payer_claim_id: str
    service_date: date
    place_of_service: str
    filing_indicator: str
    lines: List[GeneratedLine] = field(default_factory=list)

    @property
    def charge(self) -> Decimal:
        return sum((l.charge for l in self.lines), Decimal("0"))

    @property
    def paid(self) -> Decimal:
        return sum((l.paid for l in self.lines), Decimal("0"))

    @property
    def patient_responsibility(self) -> Decimal:
        return sum((l.patient_responsibility for l in self.lines), Decimal("0"))


class RemittanceGenerator:
    """Builds a year of remittances for one practice, with planted defects."""

    # The rate cut: this payer quietly reprices this code on this date.
    RATE_CUT_PAYER = "UNITED HEALTHCARE"
    RATE_CUT_CODE = "92928"
    RATE_CUT_FACTOR = Decimal("0.88")

    # Sporadic shaving: this payer occasionally allows less than its own rate.
    DRIFT_PAYER = "AETNA"
    DRIFT_CODE = "93458"
    DRIFT_RATE = 0.18
    DRIFT_FACTOR = Decimal("0.82")

    # Bundling misfire: this payer denies a legitimate add-on as included.
    BUNDLE_PAYER = "BLUE CROSS BLUE SHIELD"
    BUNDLE_CODE = "92978"
    BUNDLE_RATE = 0.55

    def __init__(self, seed: int = 20260101, months: int = 12) -> None:
        self.random = random.Random(seed)
        self.months = months
        self.defects: List[PlantedDefect] = []
        self._claim_counter = 1000
        self.rate_cut_date = date(2026, 4, 6)

    # -- pricing ----------------------------------------------------------

    def _contracted_allowed(
        self, payer: str, procedure: str, service_date: date
    ) -> Decimal:
        """What the payer *should* allow under its contract."""
        _, _, multiple = PAYERS[payer]
        medicare = PROCEDURES[procedure][1]
        return (medicare * multiple).quantize(Decimal("0.01"))

    def _adjudicated_allowed(
        self, payer: str, procedure: str, service_date: date, claim_id: str
    ) -> Tuple[Decimal, Optional[Tuple[str, str]], Optional[str]]:
        """What the payer actually allows, defects included.

        Returns (allowed, extra_reason_code, defect_kind).
        """
        correct = self._contracted_allowed(payer, procedure, service_date)

        if (
            payer == self.RATE_CUT_PAYER
            and procedure == self.RATE_CUT_CODE
            and service_date >= self.rate_cut_date
        ):
            actual = (correct * self.RATE_CUT_FACTOR).quantize(Decimal("0.01"))
            self._record("rate_change", payer, procedure, claim_id, service_date, correct, actual)
            return actual, None, "rate_change"

        if (
            payer == self.DRIFT_PAYER
            and procedure == self.DRIFT_CODE
            and self.random.random() < self.DRIFT_RATE
        ):
            actual = (correct * self.DRIFT_FACTOR).quantize(Decimal("0.01"))
            self._record("rate_drift", payer, procedure, claim_id, service_date, correct, actual)
            return actual, None, "rate_drift"

        if (
            payer == self.BUNDLE_PAYER
            and procedure == self.BUNDLE_CODE
            and self.random.random() < self.BUNDLE_RATE
        ):
            self._record(
                "suspect_bundling", payer, procedure, claim_id, service_date,
                correct, Decimal("0.00"),
            )
            return Decimal("0.00"), ("CO", "97"), "suspect_bundling"

        return correct, None, None

    def _record(
        self, kind, payer, procedure, claim_id, service_date, correct, actual
    ) -> None:
        self.defects.append(
            PlantedDefect(
                kind=kind,
                payer=payer,
                procedure=procedure,
                claim_id=claim_id,
                service_date=service_date.isoformat(),
                correct_allowed=str(correct),
                actual_allowed=str(actual),
            )
        )

    # -- claim construction ----------------------------------------------

    def _next_claim_id(self) -> str:
        self._claim_counter += 1
        return f"ACCT{self._claim_counter}"

    def _build_claim(self, payer: str, service_date: date) -> GeneratedClaim:
        claim_id = self._next_claim_id()
        _, filing_indicator, _ = PAYERS[payer]

        roll = self.random.random()
        if roll < 0.30:
            codes = [self.random.choice(PRIMARY_INTERVENTIONS)]
            # Interventions usually carry at least one add-on.
            if self.random.random() < 0.75:
                codes.append(self.random.choice(["92978", "93571", "92929"]))
            if self.random.random() < 0.25:
                codes.append("92973")
            pos = FACILITY_POS
        elif roll < 0.55:
            codes = [self.random.choice(DIAGNOSTIC_CODES)]
            if self.random.random() < 0.40:
                codes.append("93571")
            pos = FACILITY_POS
        else:
            codes = [self.random.choice(OFFICE_CODES)]
            if self.random.random() < 0.35:
                codes.append("93000")
            pos = OFFICE_POS

        claim = GeneratedClaim(
            claim_id=claim_id,
            payer_claim_id=f"CLM{self.random.randint(10**8, 10**9 - 1)}",
            service_date=service_date,
            place_of_service=pos,
            filing_indicator=filing_indicator,
        )

        for code in codes:
            allowed, extra, _ = self._adjudicated_allowed(
                payer, code, service_date, claim_id
            )
            # Practices bill well above contracted rates; the gap becomes the
            # CO-45 contractual write-off.
            charge = (PROCEDURES[code][1] * Decimal("3.4")).quantize(Decimal("0.01"))
            if allowed > 0:
                pr = self._patient_share(allowed)
            else:
                pr = Decimal("0.00")
            modifiers = []
            if PROCEDURES[code][2] and self.random.random() < 0.5:
                modifiers = ["59"]
            claim.lines.append(
                GeneratedLine(
                    procedure=code,
                    modifiers=modifiers,
                    charge=charge,
                    allowed=allowed,
                    patient_responsibility=pr,
                    extra_reason=extra,
                )
            )
        return claim

    def _patient_share(self, allowed: Decimal) -> Decimal:
        """Deductible and coinsurance, which vary and must not fool detection."""
        roll = self.random.random()
        if roll < 0.45:
            return Decimal("0.00")
        if roll < 0.80:
            return (allowed * Decimal("0.20")).quantize(Decimal("0.01"))
        return (allowed * Decimal("0.35")).quantize(Decimal("0.01"))

    # -- X12 serialisation -------------------------------------------------

    def _segment(self, *elements: str) -> str:
        return "*".join(str(e) for e in elements) + "~"

    def _render_835(
        self,
        payer: str,
        payment_date: date,
        claims: List[GeneratedClaim],
        control: int,
    ) -> str:
        payer_id, _, _ = PAYERS[payer]
        total_paid = sum((c.paid for c in claims), Decimal("0"))
        stamp = payment_date.strftime("%y%m%d")
        full_stamp = payment_date.strftime("%Y%m%d")
        ctl = f"{control:09d}"

        out: List[str] = []
        # ISA is fixed-width; the padding matters because the delimiters are
        # read positionally out of this header.
        out.append(
            "ISA*00*          *00*          *ZZ*"
            + payer_id.ljust(15)
            + "*ZZ*"
            + "HEARTCARE".ljust(15)
            + f"*{stamp}*1200*^*00501*{ctl}*0*P*:~"
        )
        out.append(self._segment("GS", "HP", payer_id, "HEARTCARE", full_stamp, "1200", str(control), "X", "005010X221A1"))
        out.append(self._segment("ST", "835", "0001"))
        out.append(
            self._segment(
                "BPR", "I", f"{total_paid:.2f}", "C", "ACH", "CCP", "01",
                "999999999", "DA", "1234567", payer_id, "", "01",
                "888888888", "DA", "7654321", full_stamp,
            )
        )
        out.append(self._segment("TRN", "1", f"EFT{control:07d}", f"1{payer_id}"))
        out.append(self._segment("DTM", "405", full_stamp))
        out.append(self._segment("N1", "PR", payer))
        out.append(self._segment("N3", "PO BOX 14079"))
        out.append(self._segment("N4", "LEXINGTON", "KY", "40512"))
        out.append(self._segment("N1", "PE", "HEART CARE ASSOCIATES", "XX", "1487654321"))
        out.append(self._segment("REF", "TJ", "742315896"))

        out.append(self._segment("LX", "1"))
        for claim in claims:
            out.append(
                self._segment(
                    "CLP", claim.claim_id, "1", f"{claim.charge:.2f}",
                    f"{claim.paid:.2f}", f"{claim.patient_responsibility:.2f}",
                    claim.filing_indicator, claim.payer_claim_id,
                    claim.place_of_service, "1",
                )
            )
            # Patient identity is emitted because real files carry it; the
            # parser is responsible for never reading it.
            out.append(self._segment("NM1", "QC", "1", "PATIENT", "SAMPLE", "", "", "", "MI", "W000000000"))
            out.append(self._segment("NM1", "82", "1", "PATEL", "RAJ", "", "", "", "XX", "1234567893"))
            out.append(self._segment("DTM", "232", claim.service_date.strftime("%Y%m%d")))

            for line in claim.lines:
                composite = ":".join(["HC", line.procedure] + line.modifiers)
                out.append(
                    self._segment(
                        "SVC", composite, f"{line.charge:.2f}",
                        f"{line.paid:.2f}", "", str(line.units),
                    )
                )
                out.append(self._segment("DTM", "472", claim.service_date.strftime("%Y%m%d")))
                contractual = line.charge - line.allowed
                if contractual > 0:
                    out.append(self._segment("CAS", "CO", "45", f"{contractual:.2f}"))
                if line.extra_reason is not None:
                    group, carc = line.extra_reason
                    out.append(self._segment("CAS", group, carc, "0.00"))
                if line.patient_responsibility > 0:
                    out.append(
                        self._segment("CAS", "PR", "2", f"{line.patient_responsibility:.2f}")
                    )
                out.append(self._segment("AMT", "B6", f"{line.allowed:.2f}"))

        out.append(self._segment("SE", str(len(out) + 1), "0001"))
        out.append(self._segment("GE", "1", str(control)))
        out.append(self._segment("IEA", "1", ctl))
        return "".join(out)

    # -- entry point -------------------------------------------------------

    def generate(self, out_dir: str | Path) -> Dict[str, object]:
        """Write a year of 835 files plus the ground-truth answer key."""
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob("*.835"):
            stale.unlink()

        control = 1
        files: List[str] = []
        start = date(2026, 1, 1)

        for month in range(self.months):
            month_start = start + timedelta(days=30 * month)
            for payer in PAYERS:
                claims = [
                    self._build_claim(
                        payer, month_start + timedelta(days=self.random.randint(0, 27))
                    )
                    for _ in range(self.random.randint(8, 14))
                ]
                # Payers remit a few weeks after the service date.
                payment_date = month_start + timedelta(days=self.random.randint(30, 45))
                content = self._render_835(payer, payment_date, claims, control)
                name = f"{payer.replace(' ', '_').lower()}_{payment_date:%Y%m%d}_{control}.835"
                (directory / name).write_text(content, encoding="ascii")
                files.append(name)
                control += 1

        truth = {
            "generated": date.today().isoformat(),
            "files": files,
            "defect_count": len(self.defects),
            "total_planted_shortfall": str(
                sum((d.shortfall for d in self.defects), Decimal("0"))
            ),
            "by_kind": self._summarise(),
            "defects": [asdict(d) for d in self.defects],
        }
        (directory / "ground_truth.json").write_text(
            json.dumps(truth, indent=2), encoding="utf-8"
        )
        return truth

    def _summarise(self) -> Dict[str, Dict[str, str]]:
        summary: Dict[str, Dict[str, str]] = {}
        for defect in self.defects:
            entry = summary.setdefault(
                defect.kind, {"count": "0", "shortfall": "0"}
            )
            entry["count"] = str(int(entry["count"]) + 1)
            entry["shortfall"] = str(
                Decimal(entry["shortfall"]) + defect.shortfall
            )
        return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sample_data", help="output directory")
    parser.add_argument("--seed", type=int, default=20260101)
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args()

    truth = RemittanceGenerator(seed=args.seed, months=args.months).generate(args.out)
    print(f"wrote {len(truth['files'])} remittance files to {args.out}/")
    print(f"planted {truth['defect_count']} defects worth ${truth['total_planted_shortfall']}")
    for kind, stats in truth["by_kind"].items():
        print(f"  {kind:20s} {stats['count']:>4} defects  ${stats['shortfall']}")


if __name__ == "__main__":
    main()
