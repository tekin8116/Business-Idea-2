"""Orchestrates the detectors and reconciles what they find.

Detectors overlap by design: a line repriced below contract is often also
below the payer's own baseline, and both detectors will report it. Reporting
it twice would double-count the recovery estimate, which is the fastest way to
lose a client's trust. So findings are reconciled to one per claim line,
keeping the best-evidenced explanation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .detect.baseline import BaselineDetector, RateChange
from .detect.bundling import BundlingDetector
from .detect.contract import ContractDetector, FeeSchedule
from .findings import (
    Category,
    Confidence,
    Finding,
    rank_findings,
    recoverable_shortfall,
    total_shortfall,
)
from .model import ZERO, Remittance
from .x12 import parse_835_file
from .x12.tokenizer import X12Error

# Which explanation wins when two detectors describe the same line. Contract
# variance is most defensible (it cites the agreement), then the payer's own
# history, then a reason-code inference.
_CATEGORY_PRIORITY = {
    Category.CONTRACT_VARIANCE: 0,
    Category.RATE_CHANGE: 1,
    Category.RATE_DRIFT: 2,
    Category.ZERO_ALLOWED: 3,
    Category.SUSPECT_BUNDLING: 4,
}
_CONFIDENCE_PRIORITY = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
}


@dataclass
class LoadResult:
    """Outcome of reading a directory of remittance files."""

    remittances: List[Remittance] = field(default_factory=list)
    files_read: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def claim_count(self) -> int:
        return sum(len(r.claims) for r in self.remittances)

    @property
    def line_count(self) -> int:
        return sum(r.line_count for r in self.remittances)

    @property
    def total_paid(self) -> Decimal:
        return sum((r.total_paid for r in self.remittances), ZERO)

    @property
    def date_range(self) -> Tuple[Optional[date], Optional[date]]:
        dates = [r.payment_date for r in self.remittances if r.payment_date]
        return (min(dates), max(dates)) if dates else (None, None)


@dataclass
class AuditResult:
    """Everything the report needs."""

    findings: List[Finding] = field(default_factory=list)
    rate_changes: List[RateChange] = field(default_factory=list)
    load: LoadResult = field(default_factory=LoadResult)
    as_of: date = field(default_factory=date.today)
    used_contracts: bool = False

    @property
    def total_shortfall(self) -> Decimal:
        return total_shortfall(self.findings)

    @property
    def recoverable(self) -> Decimal:
        return recoverable_shortfall(self.findings, self.as_of)

    @property
    def expired(self) -> Decimal:
        return self.total_shortfall - self.recoverable

    @property
    def appealable(self) -> List[Finding]:
        return [f for f in self.findings if f.is_appealable(self.as_of)]

    def by_payer(self) -> Dict[str, Tuple[int, Decimal]]:
        """Totals per payer, labelled the way the remittance spells it.

        Grouping uses the normalised key so that name variants collapse into
        one row, but the label shown is the name the payer actually uses. A
        report that calls Blue Cross "BLUE CROSS SHIELD" because a normaliser
        ate a word does not read as careful work.
        """
        counts: Dict[str, Tuple[int, Decimal]] = {}
        labels: Dict[str, Counter] = defaultdict(Counter)
        for f in self.findings:
            count, amount = counts.get(f.payer, (0, ZERO))
            counts[f.payer] = (count + 1, amount + f.shortfall)
            labels[f.payer][f.payer_label] += 1

        out = {
            labels[key].most_common(1)[0][0]: value
            for key, value in counts.items()
        }
        return dict(sorted(out.items(), key=lambda kv: kv[1][1], reverse=True))

    def by_procedure(self) -> Dict[str, Tuple[int, Decimal]]:
        out: Dict[str, Tuple[int, Decimal]] = {}
        for f in self.findings:
            count, amount = out.get(f.procedure_key, (0, ZERO))
            out[f.procedure_key] = (count + 1, amount + f.shortfall)
        return dict(
            sorted(out.items(), key=lambda kv: kv[1][1], reverse=True)
        )

    def by_category(self) -> Dict[str, Tuple[int, Decimal]]:
        out: Dict[str, Tuple[int, Decimal]] = {}
        for f in self.findings:
            count, amount = out.get(f.category.value, (0, ZERO))
            out[f.category.value] = (count + 1, amount + f.shortfall)
        return dict(
            sorted(out.items(), key=lambda kv: kv[1][1], reverse=True)
        )

    def expiring_within(self, days: int) -> List[Finding]:
        out = []
        for f in self.appealable:
            remaining = f.days_remaining(self.as_of)
            if remaining is not None and remaining <= days:
                out.append(f)
        return rank_findings(out, self.as_of)


def load_remittances(
    paths: Iterable[str | Path], pattern: str = "*.835"
) -> LoadResult:
    """Read every 835 under the given files or directories.

    A file that fails to parse is recorded and skipped rather than aborting
    the run: one malformed file out of two hundred should not cost a practice
    its whole audit.
    """
    result = LoadResult()
    for entry in paths:
        path = Path(entry)
        candidates: List[Path]
        if path.is_dir():
            candidates = sorted(
                p for p in path.rglob(pattern) if p.is_file()
            )
        else:
            candidates = [path]

        for candidate in candidates:
            try:
                result.remittances.extend(parse_835_file(candidate))
                result.files_read += 1
            except (X12Error, OSError, UnicodeError) as exc:
                result.errors.append((candidate.name, str(exc)))
    return result


def _dedupe_key(finding: Finding) -> Tuple[str, str, str, str]:
    """Identity of the underlying claim line, across detectors."""
    return (
        finding.claim_id,
        finding.payer_claim_id,
        finding.procedure_key,
        finding.service_date.isoformat() if finding.service_date else "",
    )


def reconcile(findings: Sequence[Finding]) -> List[Finding]:
    """Collapse overlapping findings to one per claim line.

    Where two detectors disagree on the expected amount, the surviving finding
    keeps the *lower* of the two estimates. Understating a recovery is
    recoverable; overstating one is not.
    """
    best: Dict[Tuple[str, str, str, str], Finding] = {}
    for finding in findings:
        key = _dedupe_key(finding)
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = finding
            continue

        challenger_rank = (
            _CATEGORY_PRIORITY.get(finding.category, 9),
            _CONFIDENCE_PRIORITY[finding.confidence],
        )
        incumbent_rank = (
            _CATEGORY_PRIORITY.get(incumbent.category, 9),
            _CONFIDENCE_PRIORITY[incumbent.confidence],
        )
        winner, loser = (
            (finding, incumbent)
            if challenger_rank < incumbent_rank
            else (incumbent, finding)
        )
        if loser.expected_allowed < winner.expected_allowed:
            winner.expected_allowed = loser.expected_allowed
        best[key] = winner
    return list(best.values())


def audit(
    remittances: Sequence[Remittance],
    schedule: Optional[FeeSchedule] = None,
    as_of: Optional[date] = None,
    load: Optional[LoadResult] = None,
) -> AuditResult:
    """Run every detector and reconcile the results."""
    baseline = BaselineDetector()
    raw: List[Finding] = list(baseline.run(remittances))
    raw.extend(BundlingDetector().run(remittances))

    if schedule is not None and schedule.terms:
        raw.extend(ContractDetector(schedule).run(remittances))

    material = [f for f in reconcile(raw) if f.is_material()]
    today = as_of or date.today()

    return AuditResult(
        findings=rank_findings(material, today),
        rate_changes=sorted(
            baseline.rate_changes, key=lambda rc: rc.exposure, reverse=True
        ),
        load=load or LoadResult(remittances=list(remittances)),
        as_of=today,
        used_contracts=bool(schedule and schedule.terms),
    )
