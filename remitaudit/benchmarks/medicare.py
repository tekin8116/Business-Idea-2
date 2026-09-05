"""Medicare Physician Fee Schedule as a pricing benchmark.

Most commercial contracts a small practice signs are expressed as a percentage
of Medicare rather than as a standalone fee schedule - "135% of the current
Medicare Physician Fee Schedule" and similar. That is enormously convenient:
the Medicare rates are published, free, and updated annually, so one number
from the practice (their multiple) plus public data yields an expected allowed
amount for every code they bill.

Locality matters. Medicare rates are geographically adjusted, so a schedule
built for the wrong MAC locality will be wrong by several percent across the
board - enough to manufacture findings that are not real. The loader therefore
records which locality a schedule represents and refuses to silently blend two.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, Optional

# Modifiers that change which component of a service is being billed, and
# therefore which Medicare amount applies.
PROFESSIONAL_COMPONENT = "26"
TECHNICAL_COMPONENT = "TC"


def _to_decimal(value: str) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


@dataclass
class MedicareSchedule:
    """Locality-specific Medicare allowed amounts, keyed by procedure code.

    Separate facility and non-facility amounts are held because they differ
    substantially for procedures done in an office versus a hospital, and the
    place of service on the claim decides which one governs.
    """

    locality: str = ""
    year: str = ""
    non_facility: Dict[str, Decimal] = field(default_factory=dict)
    facility: Dict[str, Decimal] = field(default_factory=dict)

    # Places of service that Medicare prices at the facility rate. 11 (office)
    # and 49/50/71/72 are non-facility; hospital and ASC settings are facility.
    FACILITY_POS = frozenset({"19", "21", "22", "23", "24", "26", "31", "34",
                              "41", "42", "51", "52", "53", "56", "61"})

    def is_facility(self, place_of_service: str) -> bool:
        return (place_of_service or "").strip() in self.FACILITY_POS

    def allowed_for(
        self, procedure: str, place_of_service: str = ""
    ) -> Optional[Decimal]:
        """Medicare allowed amount for a code in a given setting."""
        code = (procedure or "").strip().upper()
        if not code:
            return None
        if self.is_facility(place_of_service):
            return self.facility.get(code) or self.non_facility.get(code)
        return self.non_facility.get(code) or self.facility.get(code)

    def __len__(self) -> int:
        return len(set(self.non_facility) | set(self.facility))

    @classmethod
    def from_csv(
        cls, path: str | Path, locality: str = "", year: str = ""
    ) -> "MedicareSchedule":
        """Load a schedule from CSV.

        Expected columns (case-insensitive, extra columns ignored):
            procedure          - CPT/HCPCS code
            non_facility_rate  - office / non-facility allowed amount
            facility_rate      - hospital / facility allowed amount

        A single ``rate`` column is accepted as a shorthand and applied to
        both settings, which is right for professional-component-only codes
        and acceptable as a starting point elsewhere.
        """
        schedule = cls(locality=locality, year=year)
        with Path(path).open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                return schedule
            headers = {name.lower().strip(): name for name in reader.fieldnames}

            code_col = headers.get("procedure") or headers.get("hcpcs") or headers.get("code")
            if code_col is None:
                raise ValueError(
                    "fee schedule CSV needs a 'procedure' (or 'hcpcs'/'code') column"
                )
            non_fac_col = headers.get("non_facility_rate") or headers.get("nonfacility")
            fac_col = headers.get("facility_rate") or headers.get("facility")
            flat_col = headers.get("rate") or headers.get("allowed")

            for row in reader:
                code = (row.get(code_col) or "").strip().upper()
                if not code:
                    continue
                flat = _to_decimal(row.get(flat_col)) if flat_col else None
                non_fac = _to_decimal(row.get(non_fac_col)) if non_fac_col else None
                fac = _to_decimal(row.get(fac_col)) if fac_col else None

                if non_fac is None:
                    non_fac = flat
                if fac is None:
                    fac = flat
                if non_fac is not None:
                    schedule.non_facility[code] = non_fac
                if fac is not None:
                    schedule.facility[code] = fac
        return schedule

    @classmethod
    def from_rows(
        cls, rows: Iterable[tuple[str, Decimal]], locality: str = ""
    ) -> "MedicareSchedule":
        """Build from (code, rate) pairs; useful in tests and quick checks."""
        schedule = cls(locality=locality)
        for code, rate in rows:
            key = code.strip().upper()
            schedule.non_facility[key] = rate
            schedule.facility[key] = rate
        return schedule
