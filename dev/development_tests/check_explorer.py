"""Headless render of explorer.html: exercise every control, in both schemes,
and fail on any console message or page error.

The trap this is guarding: chart code that reads colours from CSS custom
properties at draw time gets '' in a sandboxed frame, which becomes fill:black
and stroke:none — bars survive, curves vanish, and nothing errors. So we also
assert that curves actually carry a stroke, in both schemes.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
WORK = HERE / "_scratch"

HTML = WORK / "run" / "_development_test" / "explorer.html"
problems = []


def run(scheme):
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(color_scheme=scheme)
        page.on("console", lambda m: problems.append(
            f"[{scheme}] console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"[{scheme}] pageerror: {e}"))
        page.goto(HTML.as_uri())
        page.wait_for_timeout(250)

        def snapshot(tag):
            n = {sel: page.locator(sel + " svg").count()
                 for sel in ("#si", "#comp", "#size", "#qc")}
            for sel, c in n.items():
                if c != 1:
                    problems.append(f"[{scheme}/{tag}] {sel} has {c} svg(s)")
            # every panel must have drawn some marks
            for sel in ("#si", "#comp", "#size", "#qc"):
                marks = page.locator(f"{sel} svg path, {sel} svg rect, "
                                     f"{sel} svg circle").count()
                if marks < 5:
                    problems.append(f"[{scheme}/{tag}] {sel} drew {marks} marks")
            # the stroke trap
            strokes = page.eval_on_selector_all(
                "#size svg path",
                "els => els.map(e => e.getAttribute('stroke')).filter(Boolean)")
            if not strokes:
                problems.append(f"[{scheme}/{tag}] body-size curves have no stroke")
            bad = [s for s in strokes if not s.startswith("#")]
            if bad:
                problems.append(f"[{scheme}/{tag}] non-literal stroke(s): {bad[:3]}")
            fills = page.eval_on_selector_all(
                "#comp svg rect",
                "els => els.map(e => e.getAttribute('fill')).filter(Boolean)")
            if any(not f.startswith("#") for f in fills):
                problems.append(f"[{scheme}/{tag}] composition fill not a literal")

        snapshot("initial")

        # every control, every state
        for group, label in (("#six", "X axis"), ("#qck", "QC metric"),
                             ("#theme", "Theme")):
            buttons = page.locator(f"{group} button")
            for i in range(buttons.count()):
                buttons.nth(i).click()
                page.wait_for_timeout(120)
                snapshot(f"{label}[{i}]")
                pressed = page.locator(f"{group} button[aria-pressed=true]").count()
                if pressed != 1:
                    problems.append(
                        f"[{scheme}] {label}: {pressed} buttons pressed after "
                        f"clicking {i}")

        # hover a mark to exercise the tooltip path
        page.locator("#comp svg rect").first.hover()
        page.wait_for_timeout(120)
        if page.locator("#tt").evaluate("e => e.style.opacity") != "1":
            problems.append(f"[{scheme}] tooltip did not open on hover")

        page.screenshot(path=str(HTML.parent / f"explorer_{scheme}.png"),
                        full_page=True)
        b.close()


for scheme in ("light", "dark"):
    run(scheme)

if problems:
    print(f"EXPLORER CHECK FAILED ({len(problems)}):")
    for p in problems:
        print("  " + p)
    sys.exit(1)
print("Explorer check: clean in light and dark — zero console messages, every "
      "control exercised, all four panels drew marks with literal colours.")
