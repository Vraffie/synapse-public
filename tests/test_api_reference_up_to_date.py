"""docs/api/reference.md must match the generated API reference (keeps docs from drifting)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_api_reference_is_up_to_date():
    spec = importlib.util.spec_from_file_location("gen_api_reference", ROOT / "scripts" / "gen_api_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reference = ROOT / "docs" / "api" / "reference.md"
    assert reference.exists() and reference.read_text() == module.generate(), (
        "docs/api/reference.md is stale: run scripts/gen_api_reference.py and commit the result")
