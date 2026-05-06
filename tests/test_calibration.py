"""Tests for examples/calibration/calibrate.py — score subcommand math."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "examples/calibration/calibrate.py"


def _write_jsonl(rows: list[dict], path: Path) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows))


def _run_score(path: Path) -> tuple[str, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "score", "--in", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout, r.stderr


def test_score_perfect_precision(tmp_path):
    rows = [
        {"decision_action": "re-anchor", "confidence": 0.95, "ground_truth": "correct"},
        {"decision_action": "re-anchor", "confidence": 0.85, "ground_truth": "correct"},
        {"decision_action": "re-anchor", "confidence": 0.75, "ground_truth": "correct"},
    ]
    f = tmp_path / "in.jsonl"
    _write_jsonl(rows, f)
    out, _ = _run_score(f)
    assert "labelled correct   : 3" in out
    assert "labelled wrong     : 0" in out
    # All bands with rows should show 1.000 precision
    for line in out.splitlines():
        if line.startswith("[") and "/" not in line:
            cols = line.split()
            n = int(cols[2])
            if n > 0:
                precision = float(cols[-1])
                assert precision == 1.0, f"non-perfect precision in {line!r}"


def test_score_increasing_threshold_drops_kept(tmp_path):
    rows = [
        {"decision_action": "re-anchor", "confidence": c, "ground_truth": "correct"}
        for c in [0.55, 0.65, 0.75, 0.85, 0.95]
    ]
    f = tmp_path / "in.jsonl"
    _write_jsonl(rows, f)
    out, _ = _run_score(f)
    # T=0.50 should keep 5; T=0.95 should keep 1
    assert "T=0.50" in out
    assert "T=0.95" in out
    lines = [line for line in out.splitlines() if line.startswith("T=")]
    assert len(lines) >= 2
    kept_at_low = int(lines[0].split()[1])
    kept_at_high = int(lines[-1].split()[1])
    assert kept_at_high < kept_at_low


def test_score_per_action_breakdown(tmp_path):
    rows = [
        {"decision_action": "re-anchor", "confidence": 0.8, "ground_truth": "correct"},
        {"decision_action": "re-anchor", "confidence": 0.7, "ground_truth": "wrong"},
        {"decision_action": "delete", "confidence": 0.9, "ground_truth": "wrong"},
        {"decision_action": "delete", "confidence": 0.6, "ground_truth": "wrong"},
    ]
    f = tmp_path / "in.jsonl"
    _write_jsonl(rows, f)
    out, _ = _run_score(f)
    assert "precision by action type" in out
    # re-anchor 1/2 = 0.5; delete 0/2 = 0
    delete_line = next(line for line in out.splitlines() if line.strip().startswith("delete"))
    assert "0.000" in delete_line
    re_anchor_line = next(line for line in out.splitlines() if line.strip().startswith("re-anchor"))
    assert "0.500" in re_anchor_line


def test_score_skip_excluded_from_precision(tmp_path):
    rows = [
        {"decision_action": "re-anchor", "confidence": 0.9, "ground_truth": "correct"},
        {"decision_action": "re-anchor", "confidence": 0.8, "ground_truth": "skip"},
        {"decision_action": "re-anchor", "confidence": 0.7, "ground_truth": "skip"},
    ]
    f = tmp_path / "in.jsonl"
    _write_jsonl(rows, f)
    out, _ = _run_score(f)
    assert "skipped (can't tell): 2" in out
    assert "labelled correct   : 1" in out
    # Cumulative precision at T=0.50: 1/1 = 1.0
    t050 = next(line for line in out.splitlines() if line.startswith("T=0.50"))
    assert "1.000" in t050


def test_score_unlabelled_warning(tmp_path, capsys):
    rows = [
        {"decision_action": "re-anchor", "confidence": 0.9, "ground_truth": "correct"},
        {"decision_action": "re-anchor", "confidence": 0.8, "ground_truth": ""},
    ]
    f = tmp_path / "in.jsonl"
    _write_jsonl(rows, f)
    out, err = _run_score(f)
    assert "unlabelled         : 1" in out
    assert "WARNING" in err
