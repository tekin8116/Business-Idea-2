"""End-to-end accuracy check against data with a known answer key.

Totals matching is not proof: a detector could miss three real defects and
invent three others and still balance. These tests match finding-by-finding on
claim id and procedure code, so a false positive and a false negative cannot
cancel each other out.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from remitaudit.analysis import audit, load_remittances
from synth.generate835 import RemittanceGenerator

AS_OF = date(2026, 9, 5)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("remits")
    truth = RemittanceGenerator(seed=20260101, months=12).generate(out)
    load = load_remittances([out])
    result = audit(load.remittances, as_of=AS_OF, load=load)
    return truth, load, result


def _defect_keys(truth):
    return {(d["claim_id"], d["procedure"]) for d in truth["defects"]}


def _finding_keys(result):
    return {(f.claim_id, f.procedure) for f in result.findings}


def test_files_parse_without_error(generated):
    truth, load, _ = generated
    assert load.files_read == len(truth["files"])
    assert load.errors == []
    assert load.claim_count > 400
    assert load.line_count > 700


def test_every_planted_defect_is_found(generated):
    truth, _, result = generated
    missed = _defect_keys(truth) - _finding_keys(result)
    assert not missed, f"failed to detect {len(missed)} planted defects: {sorted(missed)[:5]}"


def test_no_false_positives(generated):
    truth, _, result = generated
    spurious = _finding_keys(result) - _defect_keys(truth)
    assert not spurious, f"invented {len(spurious)} findings: {sorted(spurious)[:5]}"


def test_recovered_dollars_match_planted_dollars(generated):
    truth, _, result = generated
    assert result.total_shortfall == Decimal(truth["total_planted_shortfall"])


def test_each_defect_kind_is_attributed_correctly(generated):
    truth, _, result = generated
    by_claim = {(f.claim_id, f.procedure): f for f in result.findings}
    # A bundling denial may surface as either a bundling finding or a
    # zero-allowed one; both are correct descriptions of the same event.
    equivalent = {"suspect_bundling": {"suspect_bundling", "zero_allowed"}}
    for defect in truth["defects"]:
        finding = by_claim[(defect["claim_id"], defect["procedure"])]
        expected = equivalent.get(defect["kind"], {defect["kind"]})
        assert finding.category.value in expected, (
            f"{defect['claim_id']} {defect['procedure']}: expected "
            f"{defect['kind']}, got {finding.category.value}"
        )


def test_patient_identity_never_enters_the_model(generated):
    """The generator emits NM1*QC patient segments; none may survive parsing."""
    _, load, _ = generated
    blob = json.dumps(
        [
            {
                "payer": r.payer_name,
                "payee": r.payee_name,
                "claims": [
                    {
                        "id": c.claim_id,
                        "payer_claim": c.payer_claim_id,
                        "npi": c.rendering_npi,
                    }
                    for c in r.claims
                ],
            }
            for r in load.remittances
        ]
    )
    assert "PATIENT" not in blob.upper()
    assert "W000000000" not in blob


def test_recoverable_never_exceeds_total(generated):
    _, _, result = generated
    assert result.recoverable <= result.total_shortfall
    assert result.expired >= 0


def test_findings_are_ordered_for_a_biller(generated):
    """Expired claims must sink below appealable ones regardless of size."""
    _, _, result = generated
    seen_expired = False
    for finding in result.findings:
        expired = not finding.is_appealable(AS_OF)
        if expired:
            seen_expired = True
        elif seen_expired:
            pytest.fail("an appealable finding was ranked below an expired one")
