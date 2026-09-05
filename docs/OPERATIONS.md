# Operating notes

Practical guidance for running this against a real practice. Read before the
first engagement.

## You become a HIPAA business associate

835 files contain protected health information. Analysing them on behalf of a
practice makes you a business associate under HIPAA, which is a legal status,
not a formality.

Before receiving a single file:

- **Signed BAA in place.** Not a handshake, not "we'll do it after." The
  practice's own compliance obligations require it and a practice manager will
  ask.
- **Encrypted transfer.** Never plain email attachments. A shared folder with
  link expiry, or SFTP.
- **Encrypted at rest**, on a machine with full-disk encryption and a screen
  lock.
- **Defined retention.** Agree in writing how long you keep files and delete
  on that schedule.
- **No PHI in this repository, ever.** `.gitignore` covers `*.835` and
  `report/`, but that is a safety net, not a policy.

This tool never reads patient names, member identifiers, or dates of birth —
that reduces the surface but does not remove the obligation, because claim
numbers and service dates remain identifying in combination.

## What to ask a practice for

**Minimum, to produce a first report:**

- 835 / ERA files for the last **24 months**. Twelve is workable; twenty-four
  is much better, because a rate change is only visible with history on both
  sides of it.
- Nothing else. This is the whole point of the baseline detector — the first
  conversation should ask for exactly one thing.

**To upgrade findings from "the payer's own history" to "your contract says":**

- The payer contracts, or just the fee schedule exhibits.
- Or, far easier and usually sufficient: **the Medicare multiple** for each
  payer. Most small-practice commercial contracts are written as a percentage
  of Medicare, and a practice administrator usually knows these numbers off the
  top of their head.
- The practice's Medicare locality, so the right geographic rates are used.
  Wrong locality means every expected amount is off by a few percent, which
  manufactures findings that are not real.

## Where the files come from

Practices receive 835s through their clearinghouse (Availity, Optum, Change,
Waystar, Trizetto) or direct from payer portals. Most billing staff can export
a date range in a few minutes. If they have never done it, the clearinghouse
support line will walk them through it — setting up *new* ERA delivery takes
about 30 days, but downloading files already received is immediate.

Ask for raw `.835` / `.era` / `.txt` files, **not** the human-readable PDF
remittance summaries. The PDFs have already thrown away the fields this
depends on.

## Reading the output honestly

The report is the entire product. Its credibility is the business.

- Quote **recoverable**, never total. Money past its appeal window is real but
  uncollectable, and quoting it as recoverable is the mistake that ends an
  engagement.
- Treat `Lead` findings as leads. Do not put them in a headline.
- Expect the first real file to break something. Every clearinghouse has
  quirks. Run against the practice's actual files before promising a number.
- If the practice knowingly agreed to a lower rate, that is not a finding.
  Ask before asserting.

## Sequencing an engagement

1. BAA signed, files received.
2. Run the audit. Read the output yourself before showing anyone.
3. Present recoverable dollars, the rate-change narrative first — it is the
   most persuasive and the least arguable.
4. Work the expiring-soonest findings first, regardless of size.
5. Re-run monthly. Payer rates drift continuously, which is what makes this
   recurring rather than a one-time project.
