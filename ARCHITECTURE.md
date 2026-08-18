# WormScan architecture

**What this document is:** how the system is put together and why — the shape of
it, the boundaries between parts, and the rules that must hold. It is written to
stay true across ordinary development.

**What this document deliberately does not contain:** parameter values,
thresholds, class lists, dependency versions, file-by-file trees, or anything
else that changes when someone tunes a knob. Those belong next to the code that
owns them, and §10 says where each one lives. A document that restates them goes
stale the week it is written — that is exactly what happened to its predecessor,
`docs/history/CURRENT_STATE.md`, which is archived and should not be trusted.

If this document and the code disagree, the code is right and this document is a
bug. Fix it.

---

## 1. What the system is

A worm-imaging rig and its analysis chain. A Raspberry Pi with an HQ camera sits
over agar plates and captures stills and video. A Windows laptop pulls that data
down and runs the analyses that turn it into numbers: motility, crawling, colony
survival, and developmental staging.

Two properties drive most of the design:

- **The Pi has no internet.** Code and dependencies reach it over SSH from the
  laptop. Nothing on the Pi can `pip install` at deploy time.
- **The rig is used by biologists mid-experiment.** A failure that is silent, or
  that looks like success, costs a plate set. The system is repeatedly biased
  toward failing loudly and toward refusing to report a number it cannot stand
  behind.

---

## 2. Topology

```
  Raspberry Pi  ──HTTP (shared bearer token)──▶  Windows laptop  ──▶  container
  capture/                                       launcher/            engine
  FastAPI + picamera2                            CustomTkinter        (Tierpsy)
  writes files to disk                           mirrors + analyses
```

The Pi is a **pure HTTP server**. The laptop is a **pure HTTP client** — it
never listens, and nothing on the Pi ever initiates a connection to it. This is
why the "analyse this frame on the laptop" feature is built as a long-poll from
the laptop rather than a push from the Pi (§3.5): it keeps that asymmetry intact
and means the laptop needs no inbound firewall rule.

Deployment is over SSH (`scripts/deploy_local.sh`), not `git pull` on the Pi.
The Pi has no internet; the git-pull path only works if it is ever given one.

---

## 3. The Pi capture service (`capture/`)

A FastAPI app under systemd, plus a retention timer. Both live in `deploy/`.

### 3.1 Files are the contract

There is **no database anywhere in this system.** State is files on disk, and
that is deliberate: an experiment that survives a power cut is worth more than
one with tidy transactions.

- A session's structure is a `session.json` written atomically (temp file, then
  `os.replace`).
- Every captured file gets a `.sha256` sidecar at capture time.
- Sync is acknowledged by the existence of a `.acked` sidecar.
- Thumbnails cache under `.thumbs/`.

Sidecars are excluded from listings, manifests, retention and unsynced counts.
Anything that adds a new sidecar kind must be added to those filters too.

### 3.2 The camera is a single locked resource

One `Picamera2` instance behind one process-wide manager. A capture lock
serialises every state-changing operation on the main stream; a separate frame
lock guards the preview buffers.

**The preview thread deliberately does not take the capture lock.** This looks
like a bug and is not — holding it would deadlock recording start, which has to
kick the camera back into delivering preview frames. There is a long comment at
the site explaining the history. **Do not change the locking or threading in
`capture/app/camera.py`** without reading it.

### 3.3 Auth

One shared bearer token, constant-time compared, accepted as a header or a query
parameter. The query-parameter path exists because an `<img>` tag streaming MJPEG
cannot send headers. Only the health endpoint is unauthenticated.

This is a lab-network trust model, not a public one. It is adequate for a
direct-cabled rig and is not adequate for exposure to a shared network.

### 3.4 Retention and the disk guard

Two independent mechanisms, easily confused:

- **Retention** runs on a timer and reclaims space by deleting files that are
  already synced (or already in the recycle bin). It has a trigger threshold and
  a higher target it reclaims to, and it separately expires old synced files.
- **The disk guard** runs at the *start* of each capture and refuses with HTTP
  507 if space is short after a reclaim attempt.

The guard checks before a capture, not during one, so a long recording started
above the floor can still fill the card.

### 3.5 The analyse-on-laptop relay

A single job slot on the Pi holding one full-resolution frame in memory. The
laptop long-polls for it. A new press replaces the parked frame, so the laptop
never processes a stale capture. The frame is never written to Pi disk.

**The Pi does not interpret analysis options** — it relays them (currently as a
response header) and the laptop decides what they mean. That is the point: a new
analysis option needs a change at the two ends and no Pi deploy.

---

## 4. The Windows launcher (`launcher/`)

A CustomTkinter desktop app. `main.py` starts a set of background daemon threads
and hands them, with their status objects, to the main window.

### 4.1 The thread/status contract

This is the single most important rule in the launcher, and every agent states
it in its module docstring:

> **The worker thread only writes**, via `status.update()` / `mark_completed()`.
> **The UI thread only reads**, via `status.snapshot()` / `pop_completed()`.
> **No widget is touched off the main thread.**

The UI polls on Tk `after()` timers. Every status object is lock-guarded. The
theme and widget layers are strictly view-side and never touch an agent.

Violating this does not fail loudly — Tk from a worker thread corrupts state in
ways that surface much later. Do not.

### 4.2 The agents

Each analysis pipeline is an agent following the same lifecycle
(`start()` / `stop()` / `join()`), the same status object shape, the same cancel
flag, and the same progress callback. A new pipeline should be a new agent
following that contract rather than a special case.

Two threads are not analysis agents: the sync agent, and a worker that services
the Pi's analyse-on-laptop relay. There is also a one-shot update check.

### 4.3 Sync and the mirror

The sync agent polls the Pi's manifest and, for anything not yet acked,
downloads to a `.partial` file named with the expected hash, verifies, atomically
renames, then acks. A partial download can therefore never be mistaken for a
complete file.

The mirror is laid out for **humans browsing in Explorer**, not for the Pi's
convenience: experiment name, condition name, plate label. Name collisions are
resolved by suffixing a short id rather than by silently merging.

### 4.4 Settings propagation

Settings are a dataclass persisted as JSON in the user's app-data directory.
Unknown keys are filtered on load, so an older config file still loads.

When settings are saved they are pushed to the agents that hold a copy. **Any
component that caches a setting must either be on that propagation list or
re-read the config itself** — a component that does neither will run on stale
settings until the app restarts, and the symptom is silence.

---

## 5. The analysis pipelines

Four modes, sharing discovery, concurrency, container invocation and the agent
contract; diverging in everything else.

| Mode (UI label) | Internal name | Input | Engine | Output folder |
|---|---|---|---|---|
| Motility | `motility` | video | Tierpsy in a container | `_analysis_<ts>/` |
| Crawling | `crawling` | video | Tierpsy in a container | `_crawling_analysis_<ts>/` |
| Colony Survival | `counting` | stills | pure Python CV | `_counting_analysis_<ts>/` |
| Development | `survival` | stills | YOLO in the vision venv | `_development_<ts>/` |

Note the **Development** mode's internal name is still `survival`, as are its
module, its classes and its config fields. That is a deliberate decision not to
churn config persistence and every call site for a user-facing rename; see the
module docstring in `launcher/survival.py`.

Every run writes a new timestamped folder and never overwrites a previous one.

### 5.1 Motility and crawling are not variants of each other

They both run Tierpsy and they share transcoding, but they diverge in ways that
matter: different Tierpsy parameter files, a different fragment linker, a
different quality gate, and a different output schema. Crawling additionally
carries body-length-normalised companion metrics, because plate magnification
drifts substantially between days and raw pixel metrics are not comparable
across them.

### 5.2 Development mode reports stage, not survival

The headline readout is **mean stage index**, stage composition and body size.

Body size is reported in **micrometres**, converted from each image's own TIFF
calibration tags, and falls back to pixels for the **whole run** if any image is
uncalibrated — never a mixture, and never a substituted default. The scale is
read on the launcher side (`survival_scale.py`) rather than in the vision
subprocess, so the detection cache key is unaffected and detections replayed
from an earlier run are scaled exactly like fresh ones. It is an *apparent*
size — `sqrt(w·h)` of an axis-aligned box — not a body length.

A survival percentage is still computed and written to the workbook, but it
appears in **no figure**, deliberately. The reason is recorded in
`launcher/survival.py`: in a full dose experiment the denominator collapses at
high dose, and survival % then *rose* with dose — an inverted dose response that
was an artefact of the shrinking denominator, not biology. The survivor cutoff
also sits on the model's weakest class boundary.

Anything that reintroduces survival % as a headline number needs to answer that
first.

---

## 6. The two-venv boundary

**The launcher must never import `ultralytics` or `torch`.**

Staging inference lives in `launcher/vision/`, which has its own virtual
environment, and the launcher reaches it by running a subprocess and reading
JSON from its stdout. Two reasons, in order:

1. **Licence containment and swappability.** The AGPL-coupled stack sits in one
   detachable folder that could be replaced (e.g. by an ONNX runtime) without
   touching the launcher. Note this does *not* by itself do legal work — both
   environments ship in the same installer, and the project is AGPL as a whole
   (see `THIRD-PARTY-NOTICES.md`).
2. **Weight.** Torch is enormous and most users of most modes never need it.

The split is **not** a Python-version split. Both environments are on the same
Python version; older comments describing a version split are wrong.

The contract across the boundary is the CLI's stdout JSON, plus a shared
defaults file that both sides read. Because the 3-way consumers (batch pipeline,
UI sliders, analyse-on-laptop button) all read that same file, a retrain that
renames a class requires updating it — the launcher cannot load the model to ask.

---

## 7. Caching

Two caches, with different designs and different failure modes. Both were
tightened on 2026-08-18; the reasoning is worth keeping.

### 7.1 Tierpsy cache (motility, crawling)

Per video, next to the source file. Contains the transcoded AVI and Tierpsy's
output. A cache entry is reusable only when a stamp file next to it matches
**both** the current Tierpsy parameters **and** the pipeline that wrote it.

The pipeline tag is load-bearing: motility and crawling share the same cache
directory and their parameters diverge substantially, so without it, running one
pipeline and then the other on the same folder made the second silently reuse the
first's tracking — and which answer you got depended on the order you ran them in.

Excluded from the fingerprint, on purpose: the probed frame rate (a property of
the video, not of the settings) and the WormScan-only keys that are consumed
after Tierpsy and therefore do not invalidate its output.

An unstamped cache entry is **not** reusable. That is a one-time re-analysis cost
for caches written before the stamp existed, taken deliberately.

### 7.2 Detection cache (Development)

Per image, keyed on a digest of everything that decides which boxes exist: the
shared defaults file, the per-class confidences, the excluded classes, the model
file's identity, and the rescoring reference values.

Rescoring **alpha** is excluded from the digest, because it is recomputable from
the per-class score vectors stored in the run's CSV — changing it relabels
cached detections rather than re-running the model. Rescoring **refs** are *not*
recomputable and are hashed.

The manifest records which images a run actually produced a record for, not
which images the folder contained. This distinction is the whole point: the CSV
is per-detection, so once a run is over, an image with no rows is
indistinguishable from an image that was never analysed — and treating the
second as the first reports plates as having zero animals. A run that does not
finish marks its folders not reusable.

**Rule for both caches:** if you add a parameter that changes what the analysis
produces, it must enter the cache key. The safe direction is to hash the whole
settings blob rather than an enumerated list, so that a new parameter invalidates
by construction.

---

## 8. Paths, tunables and the installed layout

`launcher/paths.py` is the single place that knows where things are, and the
only place that should.

- **Tunable files** — the staging model, the shared inference defaults, and both
  Tierpsy parameter files — are looked up in the user's app-data directory
  first, then next to the code. This lets a retrained model or a recalibrated
  configuration be handed over as a file instead of a new installer.
- Nothing is ever **seeded** into the user directory. A seeded copy would
  silently shadow an updated file shipped by a later installer, which is the
  worst of both worlds.
- Whichever copy wins is logged once at startup and recorded in run provenance.

A source checkout and an installed copy differ in where the environments and
bundled tools live; `paths.py` resolves both. Any component that reaches a
tunable file by a hardcoded relative path is a bug — it will work in a checkout
and quietly use the wrong file when installed.

---

## 9. Packaging and distribution

End users get a single Windows installer (`packaging/`). It is per-user and
needs no administrator rights. It bundles a private Python, ffmpeg, and wheels
for both environments, and builds the environments offline at install time.

Two constraints shape it:

- **Path length.** Torch's licence tree is deep enough that the install
  directory has a hard character budget, which is why the environments sit at
  the install root rather than under a conventional Programs path. The same
  constant is enforced in three places; they must agree.
- **No internet at install time.** Everything is bundled. The container engine
  is the exception: it is set up by a separate, explicitly-launched script,
  because installing one is a system-level change the user should choose.

The container engine is **not** assumed to be Docker. Three engines are
supported behind one abstraction that dispatches on the engine's own version
banner rather than the command name. Code that shells out to `docker` directly
regresses the other two.

---

## 10. Where the values live

This document names no thresholds on purpose. When you need the actual number,
it is here:

| What | Where it lives |
|---|---|
| Staging thresholds, size gates, merge and rescoring parameters | `launcher/vision/stage_conf.json` — including `_README` blocks that record how each was measured |
| Tierpsy parameters (motility, crawling) | `launcher/motility_params.json`, `launcher/crawling_params.json` |
| Post-Tierpsy analysis constants (gates, filters, linker) | Module constants at the top of the relevant `launcher/analysis/*.py` |
| Launcher settings and their defaults | The settings dataclass in `launcher/config.py` |
| Pi service configuration | `capture/.env` (see `.env.example`); retention knobs come from the environment |
| Camera resolutions, bitrate, capture duration | `capture/app/camera.py`, `capture/app/capture_ops.py` |
| Dependency versions | The three `requirements.txt` files; the resolved set per build is recorded by the installer |
| Survivor / stage mapping | `SURVIVAL_CONFIG` and `STAGE_INDEX` in `launcher/survival.py` |
| Body-size unit and the µm/px it came from | Nowhere — it is read per image from that image's TIFF tags by `launcher/survival_scale.py`, and the range actually used is recorded in each run's `run_info`. There is deliberately no constant to look up: `dev/parked/canonical_scale.json` is a capture recommendation, not a conversion factor |

The `_README` blocks in `stage_conf.json` are the model to copy: they record not
just the value but how it was measured, what it removed, and what is still not
known. That is why they have stayed accurate while prose documentation drifted.

---

## 11. Invariants

Things that must keep being true. Each has cost real debugging.

1. The launcher never imports `ultralytics` or `torch` (§6).
2. Worker threads never touch widgets; the UI never writes status (§4.1).
3. The camera's locking and threading in `capture/app/camera.py` is not changed
   without reading the comment that explains the deadlock it avoids (§3.2).
4. Anything that changes what an analysis produces enters the relevant cache key
   (§7).
5. A component that caches settings is either on the propagation list or
   re-reads them (§4.4).
6. Downloads land at their final path only after their hash is verified (§4.3).
7. An excluded or unmeasured quantity is reported as **not measured**, never as
   zero. A zero is a claim about the plate; a blank is a claim about the run.
8. New sidecar file kinds are added to the manifest, retention, listing and
   unsynced-count filters (§3.1).
9. Paths to tunable files go through `paths.py` (§8).
10. Analysis code shells out through the engine abstraction, not to `docker`
    (§9).

---

## 12. Keeping this document true

The failure mode of its predecessor was not that it was wrong — it was that it
was *authoritative* and wrong, having been hand-regenerated against a commit and
then left behind by 36 more.

Two habits prevent a repeat:

- **Do not add values to this document.** If you find yourself writing a number,
  it belongs in §10's table as a pointer instead.
- **Update it when a boundary moves, not when a value changes.** A new pipeline,
  a new process boundary, a new cache, a new invariant — those are the events
  that make this document wrong. A retuned threshold is not.

Last reviewed against the code: **2026-08-18**.
