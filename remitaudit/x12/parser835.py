"""Assemble an X12 835 interchange into the domain model.

Deliberately tolerant: real remittance files from real clearinghouses contain
segments in orders the implementation guide does not strictly contemplate, and
a parser that raises on the first surprise is useless in production. Unknown
segments are ignored; malformed numbers become zero rather than an exception.

Patient identifiers are never read. The NM1*QC (patient), DMG (demographics),
and patient address segments are skipped at parse time rather than scrubbed
afterwards, so patient names and member IDs never enter the process at all.
Claim and payer control numbers *are* retained: they are unavoidable for
filing an appeal, and the practice already owns them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Sequence

from ..model import (
    ZERO,
    Adjustment,
    Claim,
    Remittance,
    ServiceLine,
)
from .tokenizer import Delimiters, Segment, X12Error, iter_transactions, tokenize

# CAS carries up to six (reason, amount, quantity) triplets after the group
# code, at elements 2-4, 5-7, ... 17-19.
_CAS_TRIPLET_STARTS = (2, 5, 8, 11, 14, 17)

# Entity identifiers in N1 loops.
_ENTITY_PAYER = "PR"
_ENTITY_PAYEE = "PE"

# DTM qualifiers we care about.
_DTM_SERVICE = "472"
_DTM_STATEMENT_FROM = "232"
_DTM_STATEMENT_TO = "233"

# AMT qualifier B6 is "Allowed - Actual".
_AMT_ALLOWED = "B6"

# Segments that would carry patient identity. Never read.
_PHI_SEGMENTS = frozenset({"DMG", "PER"})
_PATIENT_ENTITY_CODES = frozenset({"QC", "IL"})


def _dec(value: str) -> Decimal:
    """Parse a monetary element, tolerating blanks and junk."""
    if not value:
        return ZERO
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ArithmeticError, ValueError):
        return ZERO


def _parse_date(value: str) -> Optional[date]:
    """Parse an X12 date element (CCYYMMDD, or YYMMDD on older feeds)."""
    if not value:
        return None
    text = value.strip()
    # Range formats (RD8) arrive as CCYYMMDD-CCYYMMDD; take the start.
    if "-" in text:
        text = text.split("-", 1)[0]
    for fmt, length in (("%Y%m%d", 8), ("%y%m%d", 6)):
        if len(text) >= length:
            try:
                return datetime.strptime(text[:length], fmt).date()
            except ValueError:
                continue
    return None


def _parse_service_line(seg: Segment) -> ServiceLine:
    """Build a ServiceLine from an SVC segment.

    SVC01 is a composite: qualifier, procedure code, then up to four
    modifiers. The qualifier (HC, HP, AD, ...) is discarded; only the code and
    modifiers affect pricing.
    """
    parts = seg.components(1)
    procedure = parts[1].strip().upper() if len(parts) > 1 else ""
    modifiers = [p.strip().upper() for p in parts[2:6] if p and p.strip()]

    units = _dec(seg[5])
    if units <= 0:
        units = Decimal("1")

    return ServiceLine(
        procedure=procedure,
        modifiers=modifiers,
        charge=_dec(seg[2]),
        paid=_dec(seg[3]),
        units=units,
    )


def _parse_adjustments(seg: Segment) -> List[Adjustment]:
    """Expand a CAS segment into individual adjustments."""
    group = seg[1].strip().upper()
    out: List[Adjustment] = []
    for start in _CAS_TRIPLET_STARTS:
        reason = seg[start].strip().upper()
        if not reason:
            continue
        amount = _dec(seg[start + 1])
        quantity_raw = seg[start + 2]
        quantity = _dec(quantity_raw) if quantity_raw else None
        out.append(
            Adjustment(
                group=group, reason=reason, amount=amount, quantity=quantity
            )
        )
    return out


def _parse_transaction(
    segments: Sequence[Segment], source_file: str = ""
) -> Remittance:
    """Assemble one ST/SE transaction set into a Remittance."""
    remit = Remittance(source_file=source_file)
    claim: Optional[Claim] = None
    line: Optional[ServiceLine] = None
    # Tracks whether the N1 loop we are inside describes a patient, so that
    # trailing address segments belonging to it can be skipped.
    in_patient_loop = False

    for seg in segments:
        tag = seg.tag

        if tag in _PHI_SEGMENTS:
            continue

        if tag == "BPR":
            remit.total_paid = _dec(seg[2])
            remit.payment_date = _parse_date(seg[16])

        elif tag == "TRN":
            remit.trace_number = seg[2].strip()

        elif tag == "N1":
            entity = seg[1].strip().upper()
            in_patient_loop = entity in _PATIENT_ENTITY_CODES
            if entity == _ENTITY_PAYER:
                remit.payer_name = seg[2].strip()
                remit.payer_id = seg[4].strip()
            elif entity == _ENTITY_PAYEE:
                remit.payee_name = seg[2].strip()
                remit.payee_npi = seg[4].strip()

        elif tag in ("N3", "N4"):
            # Address segments; only ever needed for the payer/payee, and not
            # needed by this tool at all.
            continue

        elif tag == "CLP":
            claim = Claim(
                claim_id=seg[1].strip(),
                status_code=seg[2].strip(),
                charge=_dec(seg[3]),
                paid=_dec(seg[4]),
                patient_responsibility=_dec(seg[5]),
                filing_indicator=seg[6].strip(),
                payer_claim_id=seg[7].strip(),
                place_of_service=seg[8].strip(),
            )
            remit.claims.append(claim)
            line = None
            in_patient_loop = False

        elif tag == "NM1":
            entity = seg[1].strip().upper()
            in_patient_loop = entity in _PATIENT_ENTITY_CODES
            if in_patient_loop:
                continue
            # NM1*82 is the rendering provider; its NPI is useful for
            # attributing variance to a physician in a multi-provider group.
            if entity == "82" and claim is not None and seg[9]:
                claim.rendering_npi = seg[9].strip()

        elif tag == "SVC":
            if claim is None:
                continue
            line = _parse_service_line(seg)
            claim.lines.append(line)

        elif tag == "CAS":
            adjustments = _parse_adjustments(seg)
            if line is not None:
                line.adjustments.extend(adjustments)
            elif claim is not None:
                # Claim-level adjustment with no service line to attach to.
                # Park it on a synthetic line so the money is not lost from
                # the totals.
                orphan = ServiceLine(procedure="", charge=ZERO, paid=ZERO)
                orphan.adjustments.extend(adjustments)
                claim.lines.append(orphan)

        elif tag == "AMT":
            if seg[1].strip().upper() == _AMT_ALLOWED and line is not None:
                line.reported_allowed = _dec(seg[2])

        elif tag == "DTM":
            qualifier = seg[1].strip()
            parsed = _parse_date(seg[2])
            if parsed is None:
                continue
            if qualifier == _DTM_SERVICE and line is not None:
                line.service_date = parsed
            elif claim is not None:
                if qualifier == _DTM_STATEMENT_FROM:
                    claim.service_from = parsed
                elif qualifier == _DTM_STATEMENT_TO:
                    claim.service_to = parsed

    _backfill_service_dates(remit)
    return remit


def _backfill_service_dates(remit: Remittance) -> None:
    """Give every line a service date.

    Many payers omit line-level DTM*472 when every line shares the claim's
    statement date. Appeal deadlines are computed from payment date, but
    service date drives timely-filing questions and grouping, so fill it in
    rather than leaving holes.
    """
    for claim in remit.claims:
        fallback = claim.service_from or claim.service_to
        if fallback is None:
            continue
        for line in claim.lines:
            if line.service_date is None:
                line.service_date = fallback


def parse_835(
    raw: str,
    source_file: str = "",
    delimiters: Delimiters | None = None,
) -> List[Remittance]:
    """Parse an 835 interchange into one Remittance per transaction set."""
    segments = tokenize(raw, delimiters)
    remittances = [
        _parse_transaction(txn, source_file=source_file)
        for txn in iter_transactions(segments)
    ]
    if not remittances:
        raise X12Error(
            "no ST/SE transaction sets found - is this an 835, or a 999/277?"
        )
    return remittances


def parse_835_file(path: str | Path) -> List[Remittance]:
    """Parse an 835 from disk.

    Read as latin-1 rather than utf-8: X12 is byte-oriented and some payers
    emit high-bit characters in name fields that would otherwise raise.
    """
    p = Path(path)
    raw = p.read_text(encoding="latin-1")
    return parse_835(raw, source_file=p.name)
