"""Every pre-registered gate must parse and carry the fields the decision review relies on."""
from pathlib import Path

import pytest
import yaml

REGISTRY = Path(__file__).resolve().parent.parent / "research" / "registry"
GATES = sorted(REGISTRY.glob("G*.yaml"))


def test_all_seven_gates_registered():
    assert [p.name.split("-")[0] for p in GATES] == [f"G{i}" for i in range(1, 8)]


@pytest.mark.parametrize("path", GATES, ids=lambda p: p.stem)
def test_gate_has_required_fields(path):
    gate = yaml.safe_load(path.read_text())
    for field in ("id", "registered", "hypothesis", "go", "no_go", "owners"):
        assert field in gate, f"{path.name} missing {field}"
    assert gate["id"] == path.stem


def test_kill_criteria_parse():
    rules = yaml.safe_load((REGISTRY / "KILL-CRITERIA.yaml").read_text())["rules"]
    assert len(rules) >= 5
