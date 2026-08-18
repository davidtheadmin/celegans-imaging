# Building the WormScan installer

Maintainer notes. For the end-user side see [`../launcher/INSTALL.md`](../launcher/INSTALL.md).

---

## One-time setup on the build machine

Any Windows machine with internet access. It needs:

```powershell
winget install JRSoftware.InnoSetup
```

and a copy of `launcher/vision/models/staging.pt`, which is git-ignored and so
is absent from a fresh clone. Copy it in from a machine that has it.

Nothing else. The build script fetches its own Python, its own wheels and its
own ffmpeg.

---

## Building

```powershell
cd packaging
.\build_installer.ps1
```

Roughly 10–15 minutes on a first run, most of it downloading. The result is:

```
packaging\dist\WormScanSetup-<version>.exe
```

Copy that single file to the target machine and double-click it.

### Useful switches

| Switch | Effect |
|---|---|
| `-SkipDownload` | Reuse `_build\download\` — the fast path while iterating on the `.iss` file or the app sources. Seconds instead of minutes. |
| `-NoCompile` | Stage the payload but do not run Inno Setup. Use it to inspect exactly what would ship. |
| `-Version "1.2.0"` | Override the auto-generated date+sha version. |
| `-PythonSeries "3.13"` | Which CPython series to bundle. Both venvs are built from it. |

---

## What ends up in the installer

```
python\              a private CPython (nothing is required on the target)
wheels\              every dependency, for offline install — deleted after install
ffmpeg\bin\          ffmpeg.exe + ffprobe.exe
app\launcher\        the source tree, plus staging.pt and _build_info.json
tools\               postinstall.ps1, setup_engine.ps1
```

Approximate sizes: python 30 MB, wheels 450 MB, ffmpeg 90 MB, app 45 MB.
The compressed installer lands around 450–550 MB.

---

## How the install works on the target

1. Inno copies the payload to `%LOCALAPPDATA%\WormScan`. **Per-user, so there
   is no UAC prompt** and the directory stays writable.
2. `postinstall.ps1` builds **both** virtual environments from the bundled
   wheels with `--no-index`. No network at install time.
3. It verifies both environments can import their dependencies, then deletes
   `wheels\` to reclaim ~450 MB.
4. Shortcuts are created. The app shortcut targets
   `venv\Scripts\pythonw.exe`; `launcher/paths.py` resolves the vision venv
   the same way, so installed and development launches share one code path.

### Why the venvs sit at the install root, and why the path is checked

`torch` ships a licence tree **167 characters deep**:

```
torch-2.13.0.dist-info/licenses/third_party/kineto/libkineto/third_party/
dynolog/third_party/prometheus-cpp/3rdparty/civetweb/src/third_party/
duktape-1.5.2/LICENSE.txt
```

Windows caps a path at 260 characters unless long-path support is enabled
machine-wide, which needs administrator rights this installer deliberately
never asks for. So everything before `site-packages` has to fit in
**260 - 1 - 167 = 92 characters**.

The original layout nested the venv at `app\launcher\vision\.venv-vision`,
spending 32 characters on nothing, and a real install came to 98 - failing by
six, three minutes in, as an opaque `WinError 206`. Hence:

- venvs live at `<install>\venv` and `<install>\venv-vision`
- the default directory is `{localappdata}\WormScan`, not
  `{localappdata}\Programs\WormScan` (worth 9 characters)
- the Inno **directory page refuses** a folder that will not fit, before
  copying anything
- `postinstall.ps1` re-checks and fails in two seconds with the arithmetic

That leaves 24 characters of headroom for a typical username and still works
for a 26-character one. Anything worse gets told to use `C:\WormScan`.

`launcher/paths.py` resolves the venv in either layout - dev checkout first,
install second - so running from source is unchanged.

If `torch` is upgraded, re-measure: the constant lives in THREE places that must agree — `wormscan.iss` (inline in
the directory-page check), `paths.py`
(`MAX_PACKAGE_PATH`) and `postinstall.ps1` (`$MaxPackagePath`).

### Why the venvs are built on arrival rather than shipped ready-made

A virtualenv hard-codes the absolute path of its own interpreter. One created on
the build machine points at a directory that will not exist on the target. So
the ingredients ship and assembly happens locally.

### Why nothing is frozen

The app ships as `.py` files. That means:

- a fix can be delivered by replacing a file, without a rebuild
- a stack trace names a real file you can open
- no fight with PyInstaller over torch, scikit-image, `tables` and h5py hidden
  imports

The user experience is identical — they double-click a shortcut and a window
opens. They never see Python.

---

## Shipping updates without rebuilding

Four files are treated as **tunables** and are read from
`%APPDATA%\WormScan\` in preference to the copies that shipped:

```
staging.pt            the staging model
stage_conf.json       per-class floors, tiling, seam, size gate, rescore
motility_params.json  Tierpsy parameters, motility
crawling_params.json  Tierpsy parameters, crawling
```

Send someone one of those files, tell them to drop it in their data folder and
restart, and they are running it. No rebuild, no reinstall.

Nothing is ever seeded into that folder automatically, and that is deliberate:
if the installer both shipped a file *and* copied it into `%APPDATA%`, then the
next installer carrying a retrained model would be silently shadowed by the
stale copy from the first install. An empty data folder means the shipped
defaults win, always.

`launcher/paths.py` owns this. Every resolution is logged, and
`paths.overrides_in_use()` gives the pipelines something to stamp into
`run_info` — so a result produced with an overridden model says so in its own
output.

---

## The container engine

Deliberately **not** in the installer. Tierpsy's image is multi-gigabyte, and an
engine needs admin rights and possibly a reboot — none of which belongs inside a
per-user install that promises no UAC prompt.

Instead, `setup_engine.ps1` ships as a Start Menu shortcut. It detects an
existing engine and, if there is none, offers two routes — Podman as a per-user
MSI (the default when WSL is present, and it needs no administrator rights) or
Rancher Desktop via winget (which does). It then waits for
it, pulls the image and verifies. It handles the two predictable failures
explicitly: no winget, and exit code 1603 from the WSL dependency (which means
"restart and run me again").

Colony Survival and Development never touch a container, so a user who only
runs those never needs any of this.

### Engine support in the app

`launcher/analysis/engine.py` is the only place that knows about engines. It
supports **docker, podman and nerdctl** from one code path, and handles the
three things that actually differ:

- **`info --format` templates.** Docker exposes `{{.NCPU}}`/`{{.MemTotal}}`;
  Podman puts them under `{{.Host.Cpus}}`/`{{.Host.MemTotal}}`. Passing the
  wrong one does not raise anything useful — it just yields unparseable output
  and the caller falls back to a 2-CPU guess, which is a several-fold slowdown
  whose only symptom is one warning line. Hence a per-engine template.
- **Short image names.** Podman refuses to resolve them without a TTY, and every
  call here is a captured subprocess. So the default image is
  `docker.io/tierpsy/tierpsy-tracker`, fully qualified. Docker and nerdctl
  accept that form too, so there is no branch.
- **The "installed but not running" hint**, which names a different app per
  engine.

The engine's dialect is decided from its `--version` banner, not its filename,
because `docker` may be a shim — `podman-docker` installs a `docker` that is
Podman.

Rancher Desktop set to **dockerd (moby)** presents the genuine docker CLI, so it
needs none of the above; the Podman support is there so switching later is a
setting rather than a project.

`container_engine` in `config.json` pins one (`"auto"`, `"docker"`, `"podman"`,
`"nerdctl"`, or a full path). The legacy `docker_command` is still honoured when
it has been customised, so an existing `config.json` keeps working.

---

## Versioning

`build_installer.ps1` stamps `app\launcher\_build_info.json` with the version,
the git commit, whether the tree was dirty, the build time and the Python
version. `paths.version_string()` reads it back and `main.py` logs it on every
start, so a bug report from an installed copy names the build it came from.

A build from a dirty working tree prints a warning — it cannot be reproduced
from the commit it claims.

---

## Known gaps

- **Unsigned.** SmartScreen shows "Windows protected your PC" and the user has
  to click *More info → Run anyway*. `INSTALL.md` walks them through it. Fixing
  this properly needs an OV/EV code-signing certificate; check whether the
  university already holds one.
- **No auto-update.** Updating means running a newer installer over the top,
  which is supported and preserves settings and data.
- **Windows only.** `Scripts\python.exe`, `CREATE_NO_WINDOW`, the Segoe icon
  fonts and `%APPDATA%` are all assumed throughout the app, not just here.
- **Licensing — settled.** The project is released under AGPL-3.0 (`LICENSE`),
  which is the resolution this note used to recommend: the vision environment
  contains `ultralytics`, which is AGPL-3.0, and the installer ships it. Both
  `LICENSE` and `THIRD-PARTY-NOTICES.md` are copied into the install directory.
  The remaining open item is the **copyright holder**, not the licence — see the
  note at the end of `README.md`. Exporting the model to ONNX so no
  `ultralytics` ships remains an option if a permissive licence is ever wanted.
