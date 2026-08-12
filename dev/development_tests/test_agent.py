"""The agent boundary: the UI's call must match the agent's signature, and a
run must report BOTH outcomes back.

This file exists because of a bug that survived four rounds of debugging. The
dialog called ``start_analysis(..., force_reanalyze=...)`` while the agent had
no such parameter, so every Development run raised TypeError inside a Tk
callback — which pythonw.exe discards to a stderr that goes nowhere. The
symptom was a window flashing up empty and no completion message; the cause was
invisible. Every other test called ``analyze()`` directly and passed, because
nothing exercised the layer in between.

So: check the seam, and check that a failure is reported rather than swallowed.
"""
import ast, csv, inspect, math, random, shutil, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK = HERE / "_scratch_agent"
sys.path.insert(0, str(REPO / "launcher"))

import survival

if WORK.exists():
    shutil.rmtree(WORK)
ROOT = WORK / "data"
STAGES = ["L1", "L2", "L3", "L4", "adult"]
fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
# 1. Static: every keyword the UI passes must exist on the agent
# ---------------------------------------------------------------------------
print("1. the dialog's call matches the agent's signature")

tree = ast.parse((REPO / "launcher" / "ui.py").read_text(encoding="utf-8"))
calls = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    fn = node.func
    if not isinstance(fn, ast.Attribute) or fn.attr != "start_analysis":
        continue
    target = fn.value
    name = getattr(target, "attr", None) or getattr(target, "id", None)
    calls.append((name, {kw.arg for kw in node.keywords if kw.arg},
                  len(node.args), node.lineno))

check("the dialog calls start_analysis somewhere", bool(calls), "")

AGENTS = {"_survival_agent": survival.SurvivalAgent}
checked = 0
for name, kwargs, n_pos, lineno in calls:
    cls = AGENTS.get(name)
    if cls is None:
        continue                      # motility/crawling/counting: not ours
    checked += 1
    sig = inspect.signature(cls.start_analysis)
    accepted = set(sig.parameters) - {"self"}
    unknown = kwargs - accepted
    check(f"{name}.start_analysis accepts every keyword ui.py line {lineno} passes",
          not unknown, f"unknown: {sorted(unknown)}; accepts {sorted(accepted)}")
    try:
        sig.bind(None, *([None] * n_pos), **{k: None for k in kwargs})
        bound = True
        err = ""
    except TypeError as exc:
        bound, err = False, str(exc)
    check(f"the whole call at ui.py line {lineno} binds", bound, err)
check("a SurvivalAgent call site was actually found", checked > 0, "")


# ---------------------------------------------------------------------------
# 1b. Static, generalised: every wiring call in main.py must bind
# ---------------------------------------------------------------------------
#
# The same class of bug as above, one layer up: main.py constructs the agents
# and the window, and a keyword that does not exist there fails at start-up in
# a process with nowhere to print. Signatures are read straight out of the AST
# so this needs no imports — customtkinter and requests are not installed
# everywhere these tests run.
print("\n1b. main.py's wiring calls bind against the real signatures")

LAUNCHER = REPO / "launcher"


def _sig_from_ast(path, class_name, func_name="__init__"):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == func_name:
                    return _to_signature(f.args)
    return None


def _to_signature(a):
    P = inspect.Parameter
    params, posonly = [], list(getattr(a, "posonlyargs", []))
    positional = posonly + list(a.args)
    n_def = len(a.defaults)
    for i, arg in enumerate(positional):
        if arg.arg == "self":
            continue
        kind = P.POSITIONAL_ONLY if i < len(posonly) else P.POSITIONAL_OR_KEYWORD
        default = None if i >= len(positional) - n_def else P.empty
        params.append(P(arg.arg, kind, default=default))
    if a.vararg:
        params.append(P(a.vararg.arg, P.VAR_POSITIONAL))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        params.append(P(arg.arg, P.KEYWORD_ONLY,
                        default=(P.empty if d is None else None)))
    if a.kwarg:
        params.append(P(a.kwarg.arg, P.VAR_KEYWORD))
    return inspect.Signature(params)


# class name -> module file that defines it
WIRED = {
    "MainWindow": "ui.py",
    "AnalyzeWorker": "analyze_worker.py",
    "SurvivalAgent": "survival.py",
    "SurvivalStatus": "survival.py",
    "AnalyzeStatus": "analyze_worker.py",
}

main_tree = ast.parse((LAUNCHER / "main.py").read_text(encoding="utf-8"))
seen = 0
for node in ast.walk(main_tree):
    if not isinstance(node, ast.Call):
        continue
    fn = node.func
    name = getattr(fn, "id", None) or getattr(fn, "attr", None)
    if name not in WIRED:
        continue
    sig = _sig_from_ast(LAUNCHER / WIRED[name], name)
    if sig is None:
        check(f"{name}.__init__ was found in {WIRED[name]}", False, "")
        continue
    seen += 1
    kwargs = {kw.arg for kw in node.keywords if kw.arg}
    try:
        sig.bind(*([None] * len(node.args)), **{k: None for k in kwargs})
        ok, err = True, ""
    except TypeError as exc:
        ok, err = False, str(exc)
    check(f"main.py line {node.lineno}: {name}(...) binds", ok, err)
check("main.py's wiring calls were actually found", seen >= 3, str(seen))


# ---------------------------------------------------------------------------
# 2. Live: run the agent exactly as the dialog does
# ---------------------------------------------------------------------------
print("\n2. a real run through the agent reports success, with the reuse note")

for day, folder in (("20260805", "t0"),):
    d = ROOT / folder
    d.mkdir(parents=True)
    for strain in ("N2", "601"):
        for qi, q in enumerate(("NE", "NW", "SE", "SW")):
            (d / f"{strain}_T_0J_p01_{day}T0754{qi:02d}_{q}.png").write_bytes(b"x")

rng = random.Random(3)


def fake_run_inference(images, class_conf, *, exclude_classes, preview_dir,
                       soft_csv, rescore, write_log, progress_cb=None,
                       cancel_check=None):
    records, rows = [], []
    for img in images:
        counts = {s: rng.randint(1, 8) for s in STAGES}
        records.append({"path": str(img), "counts": counts,
                        "w": 4056, "h": 3040})
        for st, c in counts.items():
            for k in range(c):
                sz = math.exp(3.5 + STAGES.index(st) * 0.2)
                rows.append([img.name, k, 0, 0, sz, sz, sz, sz, round(sz, 2),
                             st, st, 0.7, 1.0, 0.5]
                            + [0.5] * len(STAGES) + [0.2] * len(STAGES))
        if progress_cb:
            progress_cb(len(records), len(images), img.name)
    with open(soft_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "det_index", "x1", "y1", "x2", "y2", "w_px",
                    "h_px", "size_px", "hard_call", "hard_call_raw",
                    "hard_score", "match_iou", "entropy"]
                   + [f"raw_{s}" for s in STAGES] + [f"p_{s}" for s in STAGES])
        w.writerows(rows)
    meta = {"names": STAGES + ["egg"], "class_conf": {}, "conf": 0.25,
            "overlap": 0.35, "seam": {}, "exclude_classes": ["egg"],
            "rescore": {"alpha": 2.0 if rescore else 0.0, "refs": {}}}
    return list(meta["names"]), records, meta


survival.run_inference = fake_run_inference


def run_agent(force=False, timeout=90):
    status = survival.SurvivalStatus()
    agent = survival.SurvivalAgent(object(), status)
    agent.start()
    plans = survival.resolve_timepoints([(ROOT / "t0", "")])
    # Exactly the keywords ui.py uses — that is the point of this test.
    agent.start_analysis(
        plans, class_conf={"L1": 0.4}, save_previews=False,
        exclude_classes=["egg"], rescore=True, force_reanalyze=force,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not status.is_running():
            break
        time.sleep(0.05)
    time.sleep(0.2)
    result = status.pop_completed()
    agent.stop()
    return status, result


status1, result1 = run_agent()
check("the run reported a result at all (not silently dead)",
      result1 is not None, "pop_completed() returned None")
if result1:
    check("it reported success", not result1.get("failed"),
          str(result1.get("error")))
    check("it processed plates", result1.get("n_ok", 0) > 0,
          str(result1.get("n_ok")))
    check("a run folder was created",
          result1.get("out_dir") and Path(result1["out_dir"]).is_dir())
    check("the completion result carries a note field",
          "note" in result1, str(sorted(result1)))

print("\n3. the second run through the agent reuses, and says so")
status2, result2 = run_agent()
check("the second run also reported", result2 is not None)
if result2:
    check("it succeeded", not result2.get("failed"), str(result2.get("error")))
    check("its note says nothing was analysed",
          "No images were analysed" in (result2.get("note") or ""),
          repr(result2.get("note")))

print("\n4. force_reanalyze reaches the run rather than being ignored")
log_before = list((ROOT / "t0").glob("_development_*"))
status3, result3 = run_agent(force=True)
newest = max((ROOT / "t0").glob("_development_*"), key=lambda p: p.stat().st_mtime)
text = (newest / "log.txt").read_text(encoding="utf-8", errors="replace")
check("the run log shows the re-analyse request",
      "Re-analyse requested" in text, text[:200])
check("and it has no reuse note", not (result3 or {}).get("note"),
      repr((result3 or {}).get("note")))

print("\n5. a crash is reported, not swallowed")
boom = survival.analyze


def exploding_analyze(*a, **kw):
    raise RuntimeError("synthetic failure for the test")


survival.analyze = exploding_analyze
try:
    status4, result4 = run_agent()
finally:
    survival.analyze = boom
check("a crashed run still reports a result", result4 is not None,
      "pop_completed() returned None — the failure would be invisible")
if result4:
    check("it is marked as failed", bool(result4.get("failed")))
    check("the error text is carried",
          "synthetic failure" in str(result4.get("error")),
          str(result4.get("error")))
    check("the run folder is offered so the log can be opened",
          bool(result4.get("out_dir")))
    if result4.get("out_dir"):
        crash_log = Path(result4["out_dir"]) / "log.txt"
        check("the traceback is written into the run's own log.txt",
              crash_log.is_file()
              and "RUN FAILED" in crash_log.read_text(encoding="utf-8",
                                                      errors="replace"))

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("agent boundary: all checks passed")
