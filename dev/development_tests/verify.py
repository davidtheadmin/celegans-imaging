"""Recalculate the harness workbook with LibreOffice, then cross-check EVERY
computed cell against the value Python already had.

A clean recalc proves the formulas evaluate. It does not prove they are right:
an off-by-one row range recalculates without error and reports the wrong
number. That happened once during this build — the qc sheet was dividing a
plate count by a quadrant count — and this is what caught it.

Run harness.py first; this reads the expected values it pickled.
"""
import pickle, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK = HERE / "_scratch"
sys.path.insert(0, str(REPO / "launcher"))
import survival_excel

xlsx = WORK / "run" / "_development_test" / "development_results.xlsx"
r = subprocess.run([sys.executable, str(HERE / "recalc.py"), str(xlsx), "180"],
                   capture_output=True, text=True, cwd=str(HERE))
print(r.stdout.strip()[:600], r.stderr.strip()[:400])
expected = pickle.load(open(WORK / "expected.pkl", "rb"))
msgs = []
problems = survival_excel.verify_workbook(xlsx, expected, msgs.append)
print("\n".join(msgs[:60]))
sys.exit(1 if problems else 0)
