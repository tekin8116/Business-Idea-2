"""Command line entry point.

    remitaudit audit ./remits --practice "Heart Care Associates" --out ./report
    remitaudit audit ./remits --contracts contracts.csv --medicare pfs.csv
    remitaudit sample --out ./sample_data

Designed to be run by one person against a folder of files a practice emailed
over, and to produce, in a single pass, everything needed for the meeting that
follows: the report, the worklist, and the letters.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence

from .analysis import audit, load_remittances
from .benchmarks.medicare import MedicareSchedule
from .detect.contract import FeeSchedule
from .findings import Finding
from .report.html import render_report
from .report.letters import PracticeIdentity, write_appeal_letters, write_worklist_csv


def _parse_as_of(value: Optional[str]) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--as-of must be YYYY-MM-DD, got {value!r}")


def _build_schedule(
    contracts: Optional[str], medicare: Optional[str], locality: str
) -> Optional[FeeSchedule]:
    if not contracts:
        if medicare:
            print(
                "note: --medicare was supplied without --contracts, so there "
                "is nothing to price against; ignoring it.",
                file=sys.stderr,
            )
        return None
    schedule_medicare = (
        MedicareSchedule.from_csv(medicare, locality=locality) if medicare else None
    )
    schedule = FeeSchedule.from_csv(contracts, medicare=schedule_medicare)
    needs_medicare = any(t.medicare_multiple is not None for t in schedule.terms)
    if needs_medicare and schedule_medicare is None:
        print(
            "warning: contracts reference a Medicare multiple but no "
            "--medicare fee schedule was supplied. Those terms cannot be "
            "priced and will be skipped.",
            file=sys.stderr,
        )
    return schedule


def _print_summary(result, top: int = 8) -> None:
    load = result.load
    start, end = load.date_range
    span = f"{start} to {end}" if start and end else "unknown period"

    print(f"\n  Files parsed          {load.files_read:,}")
    if load.errors:
        print(f"  Files failed          {len(load.errors):,}")
        for name, message in load.errors[:5]:
            print(f"      {name}: {message}")
    print(f"  Remittance period     {span}")
    print(f"  Claims / lines        {load.claim_count:,} / {load.line_count:,}")
    print(f"  Total remitted        ${load.total_paid:,.2f}")
    print(f"\n  Findings              {len(result.findings):,}")
    print(f"  Total shortfall       ${result.total_shortfall:,.2f}")
    print(f"  Still recoverable     ${result.recoverable:,.2f}")
    print(f"  Window already closed ${result.expired:,.2f}")

    expiring = result.expiring_within(30)
    if expiring:
        amount = sum((f.shortfall for f in expiring), type(result.total_shortfall)(0))
        print(f"  Expiring in 30 days   {len(expiring):,} findings / ${amount:,.2f}")

    if result.rate_changes:
        print("\n  Rate changes detected:")
        for change in result.rate_changes[:top]:
            print(f"    - {change.describe()}")

    by_payer = result.by_payer()
    if by_payer:
        print("\n  By payer:")
        for payer, (count, amount) in list(by_payer.items())[:top]:
            print(f"    {payer:<34} {count:>4} lines   ${amount:>12,.2f}")


def _cmd_audit(args: argparse.Namespace) -> int:
    as_of = _parse_as_of(args.as_of)
    load = load_remittances(args.paths, pattern=args.pattern)

    if load.files_read == 0:
        print(
            f"No remittance files matched {args.pattern!r} under "
            f"{', '.join(str(p) for p in args.paths)}.",
            file=sys.stderr,
        )
        if load.errors:
            for name, message in load.errors:
                print(f"  {name}: {message}", file=sys.stderr)
        return 1

    schedule = _build_schedule(args.contracts, args.medicare, args.locality)
    result = audit(load.remittances, schedule=schedule, as_of=as_of, load=load)
    _print_summary(result)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "underpayment_review.html"
    report_path.write_text(
        render_report(
            result,
            practice_name=args.practice,
            prepared_by=args.prepared_by,
            worklist_limit=args.report_rows,
        ),
        encoding="utf-8",
    )

    csv_path = write_worklist_csv(
        result.findings, out_dir / "worklist.csv", as_of=as_of
    )

    print(f"\n  Report                {report_path}")
    print(f"  Worklist              {csv_path}")

    if args.letters:
        practice = PracticeIdentity(name=args.practice)
        appealable = [f for f in result.findings if f.is_appealable(as_of)]
        written = write_appeal_letters(
            appealable,
            out_dir / "appeals",
            practice=practice,
            limit=args.letters,
            as_of=as_of,
        )
        print(f"  Appeal letters        {len(written)} in {out_dir / 'appeals'}")

    print()
    return 0


def _cmd_sample(args: argparse.Namespace) -> int:
    from synth.generate835 import RemittanceGenerator

    truth = RemittanceGenerator(seed=args.seed, months=args.months).generate(args.out)
    print(f"Wrote {len(truth['files'])} remittance files to {args.out}/")
    print(
        f"Planted {truth['defect_count']} defects worth "
        f"${truth['total_planted_shortfall']}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remitaudit",
        description="Find claim lines your payers underpaid.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("audit", help="analyse a folder of 835 files")
    run.add_argument("paths", nargs="+", help="835 files or directories")
    run.add_argument("--out", default="report", help="output directory")
    run.add_argument("--practice", default="Your practice", help="practice name")
    run.add_argument("--prepared-by", default="", help="your name or firm")
    run.add_argument("--pattern", default="*.835", help="glob for directory scans")
    run.add_argument("--contracts", help="CSV of contracted terms")
    run.add_argument("--medicare", help="CSV of Medicare allowed amounts")
    run.add_argument("--locality", default="", help="Medicare locality label")
    run.add_argument(
        "--as-of", help="evaluate appeal windows as of this date (YYYY-MM-DD)"
    )
    run.add_argument(
        "--letters",
        type=int,
        default=0,
        metavar="N",
        help="draft appeal letters for the top N appealable findings",
    )
    run.add_argument(
        "--report-rows",
        type=int,
        default=60,
        help="how many findings to show in the HTML worklist",
    )
    run.set_defaults(func=_cmd_audit)

    sample = sub.add_parser(
        "sample", help="generate synthetic remittances with known defects"
    )
    sample.add_argument("--out", default="sample_data")
    sample.add_argument("--seed", type=int, default=20260101)
    sample.add_argument("--months", type=int, default=12)
    sample.set_defaults(func=_cmd_sample)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
