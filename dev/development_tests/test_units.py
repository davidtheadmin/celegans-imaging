"""Unit checks for the parts the harness does not reach:
timepoint resolution, the rescore switch, and the analyze() orchestrator."""
import shutil, sys, types
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Scratch lives beside the tests, never in the repo tree that ships.
WORK = HERE / "_scratch"
sys.path.insert(0, str(REPO / "launcher"))
import survival

ROOT = WORK / "tp"
if ROOT.exists():
    shutil.rmtree(ROOT)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        fails.append(name)


def mkfolder(name, files):
    d = ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_bytes(b"")
    return d


print("timepoint resolution")

# --- typed only ---
a = mkfolder("a", ["601_T_0J_p01_0001.png"])
b = mkfolder("b", ["601_T_0J_p01_0001.png"])
plans = survival.resolve_timepoints([(a, "0"), (b, "24")])
check("typed values are taken literally",
      [p.hours for p in plans] == [0.0, 24.0], str([p.hours for p in plans]))
check("typed method recorded", all(p.method == "typed" for p in plans))

# --- single folder, blank ---
plans = survival.resolve_timepoints([(a, "")])
check("single folder with no timepoint is 0 h, not an error",
      plans[0].hours == 0.0 and not plans[0].error and
      plans[0].method == "single folder", plans[0].error)

# --- derived from ISO stamps ---
c = mkfolder("c", ["20260805T075438_NE.tif", "20260805T075512_NW.tif",
                   "20260805T075550_SE.tif"])
d = mkfolder("d", ["20260806T075438_NE.tif", "20260806T081438_NW.tif",
                   "20260806T075550_SE.tif"])
plans = survival.resolve_timepoints([(c, ""), (d, "")])
check("derived relative to the earliest folder",
      plans[0].hours == 0.0 and plans[1].hours == 24.0,
      str([p.hours for p in plans]))
check("derivation uses the median, not the max",
      "median 2026-08-06 07:55" in plans[1].detail, plans[1].detail)
check("method recorded as filenames",
      all(p.method == "filenames" for p in plans))

# --- median not min: one stray file must not move the folder ---
e = mkfolder("e", ["20260806T075438_NE.tif", "20260806T075512_NW.tif",
                   "20200101T000000_SE.tif"])
plans = survival.resolve_timepoints([(c, ""), (e, "")])
check("a stray old file does not redefine the folder's clock",
      plans[1].hours == 24.0, str(plans[1].hours))

# --- no stamp, no typed value, >1 folder -> refuse ---
plans = survival.resolve_timepoints([(a, "0"), (b, "")])
check("unresolvable folder is refused, and names itself",
      bool(plans[1].error) and "b" in plans[1].error, plans[1].error)
check("the resolvable folder is untouched",
      plans[0].hours == 0.0 and not plans[0].error)

# --- non-numeric typed value ---
plans = survival.resolve_timepoints([(a, "tomorrow"), (b, "24")])
check("a non-numeric timepoint is an error, not a silent 0",
      bool(plans[0].error) and "tomorrow" in plans[0].error, plans[0].error)

# --- mixed: typed + derived, anchored ---
plans = survival.resolve_timepoints([(c, "6"), (d, "")])
check("mixed run anchors derived hours onto the typed clock",
      plans[1].hours == 30.0, str(plans[1].hours))
check("the anchor is disclosed in the detail line",
      "anchored" in plans[1].detail, plans[1].detail)

# --- mixed with no anchor -> refuse rather than invent an offset ---
plans = survival.resolve_timepoints([(a, "6"), (d, "")])
check("mixed run with no common folder refuses",
      bool(plans[1].error) and "clocks" in plans[1].error, plans[1].error)

# --- comma decimal ---
plans = survival.resolve_timepoints([(a, "24,5"), (b, "0")])
check("comma decimals accepted", plans[0].hours == 24.5, str(plans[0].hours))

print("\nfilename / quadrant parsing")
check("ISO stamp parsed",
      survival.parse_capture_time("20260805T075438_NW.tif")
      == datetime(2026, 8, 5, 7, 54, 38))
check("capture-style name has no stamp",
      survival.parse_capture_time("601_Train_survival_0J_p01_0001.png") is None)
check("quadrant read off the suffix",
      survival.quadrant_of("20260805T075438_NW.tif") == "NW")
check("no suffix -> the stem, so units stay distinct",
      survival.quadrant_of("601_T_0J_p01_0001.png") == "601_T_0J_p01_0001")

print("\nstage index")
check("L1..adult map to 1..5",
      [survival.stage_index_of(s) for s in ("L1", "L2", "L3", "L4", "adult")]
      == [1.0, 2.0, 3.0, 4.0, 5.0])
check("eggs have no index", survival.stage_index_of("egg") is None)
check("case and space tolerant", survival.stage_index_of(" Adult ") == 5.0)
si, n = survival._mean_stage_index({"egg": 100, "L1": 1, "adult": 1})
check("eggs excluded from the mean and its n", si == 3.0 and n == 2,
      f"{si} {n}")

print("\nrescore switch (command construction)")
calls = {}


class FakeProc:
    returncode = 0

    def __init__(self, cmd, **kw):
        calls["cmd"] = cmd
        self.stdin = types.SimpleNamespace(write=lambda s: None,
                                           close=lambda: None)
        meta = ('{"names":["L1","adult"],"class_conf":{},"rescore":'
                '{"alpha":%s,"refs":{}},"exclude_classes":[]}\n')
        alpha = "0.0" if "--rescore-alpha" in cmd else "2.0"
        self.stdout = iter([meta % alpha])
        self.stderr = iter([])

    def wait(self):
        return 0

    def terminate(self):
        pass


import subprocess
real_popen, real_exists = subprocess.Popen, Path.exists
subprocess.Popen = FakeProc
Path.exists = lambda self: True
try:
    for rescore, want_flag in ((True, False), (False, True)):
        survival.run_inference([Path("x.png")], {}, preview_dir=None,
                               soft_csv=None, rescore=rescore,
                               write_log=lambda m: None)
        cmd = calls["cmd"]
        has = "--rescore-alpha" in cmd
        check(f"rescore={rescore} -> alpha flag present is {want_flag}",
              has == want_flag, " ".join(cmd))
        if has:
            check("the only alpha we ever pass is 0",
                  cmd[cmd.index("--rescore-alpha") + 1] == "0",
                  cmd[cmd.index("--rescore-alpha") + 1])
    # The point of the switch is that no alpha VALUE lives in Python. Strip
    # comments and docstring prose, then look for one.
    import re as _re
    for mod in ("survival.py", "ui.py", "config.py", "survival_excel.py"):
        text = (REPO / "launcher" / mod).read_text(encoding="utf-8")
        code = "\n".join(l.split("#")[0] for l in text.splitlines())
        code = _re.sub(r'"""[\s\S]*?"""', "", code)
        code = _re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", code)
        check(f"no rescore alpha value hardcoded in {mod}",
              not _re.search(r"alpha\s*[=:]\s*(?!0\b)[0-9]", code), mod)
finally:
    subprocess.Popen = real_popen
    Path.exists = real_exists

print("\nsoft-scores CSV is unconditional")
src = (REPO / "launcher" / "survival.py").read_text()
check("analyze always passes a soft_csv path",
      "soft_csv=soft_csv" in src and "soft_scores" not in src.split("def analyze")[1],
      "")
ui = (REPO / "launcher" / "ui.py").read_text()
check("the soft-scores checkbox is gone from the UI",
      "_surv_soft_scores" not in ui)
check("the rescore checkbox is in the UI",
      "_surv_rescore" in ui and "Correct for uneven class confidence" in ui)

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("all unit checks passed")
