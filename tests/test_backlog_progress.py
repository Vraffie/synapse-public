import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backlog_progress.py"
spec = importlib.util.spec_from_file_location("backlog_progress", SCRIPT)
bp = importlib.util.module_from_spec(spec)
sys.modules["backlog_progress"] = bp
spec.loader.exec_module(bp)

SAMPLE = """# Title

<!-- progress:start -->
- [ ] stale content inside the block is ignored
<!-- progress:end -->

## M0 — Base
- [x] one
- [ ] two
  - [x] nested

```
- [ ] inside a fence is ignored
```

## Notes
no boxes here

## M1 — Next
- [ ] a
### M1-A
- [x] b
- F1 [fixed] done
- F2 [open] not done
- F3 [open] not done
- #5 [draft] x
- #6 [ready] y
- #6 [ready] duplicate counts once
"""


def test_counts_per_milestone_and_rolls_up_subheadings():
    assert bp.count_milestones(SAMPLE) == [("M0 — Base", 2, 3), ("M1 — Next", 1, 2)]


def test_findings_and_prs():
    assert bp.count_findings(SAMPLE) == (1, 2)
    assert bp.count_prs(SAMPLE) == (1, 1)


def test_percent_and_bar():
    assert bp.pct(1, 3) == 33 and bp.pct(0, 0) == 0 and bp.pct(3, 3) == 100
    assert bp.bar(1, 2) == "[" + "#" * 10 + "-" * 10 + "] 50%"


def test_render_has_overall_and_update_is_idempotent():
    block = bp.render(SAMPLE)
    assert "| **Overall** | **3** | **5** | **60%** |" in block
    assert "1 fixed, 2 open" in block
    updated = bp.update(SAMPLE)
    assert bp.update(updated) == updated
    assert updated.startswith("# Title") and "stale content" not in updated


def test_cli_check_detects_stale_and_write_fixes(tmp_path):
    f = tmp_path / "b.md"
    f.write_text(SAMPLE, encoding="utf-8")

    def run(*a):
        return subprocess.run([sys.executable, str(SCRIPT), "--file", str(f), *a], capture_output=True, text=True)

    assert run("--check").returncode == 1
    assert run("--write").returncode == 0
    assert run("--check").returncode == 0
    f.write_text(f.read_text(encoding="utf-8").replace("- [ ] two", "- [x] two"), encoding="utf-8")
    assert run("--check").returncode == 1


def test_real_backlog_table_is_current():
    text = (ROOT / "IMPLEMENTATION_BACKLOG.md").read_text(encoding="utf-8")
    assert bp.START in text and bp.END in text
    assert bp.update(text) == text, "run: python scripts/backlog_progress.py --write"
