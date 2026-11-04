"""Every pre-registered gate must parse and carry the fields the decision review relies on."""
from pathlib import Path

import pytest
import yaml

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "registry"
GATES = sorted(REGISTRY.glob("G*.yaml"))
ORIGINAL = [f"G{i}" for i in range(1, 8)]  # registered on Oct 6, before any data


def test_all_seven_original_gates_registered():
    ids = [p.name.split("-")[0] for p in GATES]
    assert [i for i in ids if i in ORIGINAL] == ORIGINAL
    assert len(set(ids)) == len(ids)


def test_a_gate_added_later_gives_its_reason():
    """A gate changes only by adding a new one, and the new one says why."""
    for path in GATES:
        if path.name.split("-")[0] not in ORIGINAL:
            gate = yaml.safe_load(path.read_text())
            assert str(gate["registered"]) > "2026-10-06" and gate.get("reason"), path.name


@pytest.mark.parametrize("path", GATES, ids=lambda p: p.stem)
def test_gate_has_required_fields(path):
    gate = yaml.safe_load(path.read_text())
    for field in ("id", "registered", "hypothesis", "go", "no_go", "owners"):
        assert field in gate, f"{path.name} missing {field}"
    assert gate["id"] == path.stem


def test_kill_criteria_parse():
    rules = yaml.safe_load((REGISTRY / "KILL-CRITERIA.yaml").read_text())["rules"]
    assert len(rules) >= 5
