# remitaudit

Finds the claim lines your insurers underpaid — the ones that posted as *paid*
and therefore nobody ever looked at again.

## The problem this solves

A practice signs a contract saying a payer will allow $612.40 for a procedure.
Some months later the payer allows $551.16 instead. The practice management
system posts the payment, closes the claim at a zero balance, and moves on. No
alert fires, because the software knows what *was* paid and has no idea what
*should* have been paid — the contracted rate lives in a PDF nobody has opened
since it was signed.

This is not the same problem as denials. Denials are loud, visible, and
already worked. Underpayments are silent, and they compound: a rate quietly
cut in April keeps being underpaid on every claim through December.

Appeal windows are typically 30–90 days from the remittance date. Money not
found inside that window is unrecoverable by any means.

## What it does

Reads X12 835 electronic remittance advice files — the standard format every
clearinghouse can export — and reports every service line that was allowed
less than it should have been, ranked by how soon the appeal window closes.

Three detectors run over the same data:

**Rate baseline** — needs no contract. Holds a payer to the rate it has
demonstrably paid the practice for the same code, in the same setting, with
the same modifiers. This is what makes the tool useful on day one, because a
practice can rarely produce its fee schedules on request but can always
produce its remittances. It is also the most appealable finding: the payer's
own adjudication history is the evidence.

**Rate change** — detects a dated step down in what a payer allows, which is
the highest-value pattern in the whole system. Not a misprocessed claim, but a
repricing that silently applies to every claim since.

**Adjudication review** — flags lines whose adjustment reason codes suggest
the payer applied the wrong rule: a designated add-on code bundled into its
primary procedure, a bilateral service treated as a duplicate, a
multiple-procedure reduction a modifier should have prevented.

**Contract variance** (optional) — when the practice can supply its terms,
compares against them directly. Terms may be explicit rates or, far more
commonly, a multiple of the Medicare Physician Fee Schedule.

## Usage

```bash
# Generate synthetic remittances with known planted defects
python -m remitaudit sample --out sample_data

# Analyse them
python -m remitaudit audit sample_data \
    --practice "Heart Care Associates" \
    --letters 25 \
    --out report

# With contracts, for the stronger finding
python -m remitaudit audit ./remits \
    --contracts contracts.csv \
    --medicare medicare_rates.csv \
    --out report
```

Produces `underpayment_review.html` (the client-facing report),
`worklist.csv` (every finding, for import into a billing system), and
`appeals/` (a pre-drafted dispute letter per finding).

### Contract file format

Two columns are enough for a practice with one blanket agreement per payer:

```csv
payer,medicare_multiple
AETNA,1.62
UNITED HEALTHCARE,1.48
```

Full form supports per-code carve-outs, place-of-service splits, plan classes,
and effective dates. The most specific matching term wins.

## Design decisions worth knowing

**Allowed amounts, not paid amounts.** What a payer sends is the allowed
amount minus patient responsibility. Comparing payments would flag every
high-deductible plan as an underpayment.

**Like compared with like.** Findings are bucketed by payer, plan class
(commercial vs Medicare Advantage vs Medicaid — from CLP06), place of service,
procedure and modifiers. Blending a professional-component reading with a
global service, or a commercial rate with an MA rate, manufactures variance
that is not real.

**Understate rather than overstate.** Where two detectors disagree on the
expected amount, the lower estimate survives. The headline number is what is
still recoverable, never the gross total including expired claims.

**Patient identity is never read.** `NM1*QC`, `DMG`, and patient address
segments are skipped at parse time rather than scrubbed afterwards, so patient
names, member IDs and dates of birth never enter the process. Claim and payer
control numbers are retained because they are unavoidable for filing an
appeal.

## Testing

```bash
python -m pytest tests/ -q
```

The synthetic generator plants defects of each kind and writes an answer key.
Tests match finding-by-finding on claim id and procedure code — not on totals,
because a false positive and a false negative can cancel out in a total and
hide both.

## Limitations

- A rate change needs history on both sides of it to be visible. Ask practices
  for **24 months** of remittances, not three.
- Findings marked `Lead` rest on a thin sample. They are worth checking, not
  worth asserting to a payer.
- Appeal windows are conservative defaults from the remittance date. Actual
  contractual dispute windows govern and some are shorter.
- A bundling finding means the adjudication looks questionable, not that it is
  certainly wrong. Clinical documentation decides those.
- If a rate was renegotiated downward and the practice agreed to it, the
  corresponding findings are correct arithmetic but not a recoverable claim.

## Handling real data

Running this against a live practice makes you a HIPAA business associate.
See [docs/OPERATIONS.md](docs/OPERATIONS.md) before touching real files.
