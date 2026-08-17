# Third-party notices

WormScan redistributes the components below. Each is governed by its own
licence, reproduced or linked here as those licences require. Nothing in this
file changes WormScan's own licence, which is the GNU Affero General Public
License v3.0 — see [`LICENSE`](LICENSE).

Two entries decide WormScan's own licence, and are the reason this project is
AGPL rather than something more permissive:

| Component | Licence | Why it matters |
|---|---|---|
| **ultralytics** | **AGPL-3.0** | Linked into the staging-inference environment and shipped inside the installer. Distributing a work that includes it obliges us to offer the complete corresponding source of the whole under AGPL-3.0. That is what `LICENSE` does. |
| **ffmpeg** (GPL build) | **GPL-3.0** | Bundled as `ffmpeg.exe` / `ffprobe.exe`. A GPL build is required rather than incidental: `render_video.py` encodes with **libx264**, which is GPL-only. An LGPL build would install and then fail at the first render. |

Both are invoked as separate processes or as a separate interpreter, but both
travel inside the same installer, so they are distributed together.

---

## Shipped in the installer

### Staging inference environment (`venv-vision`)

| Package | Licence |
|---|---|
| ultralytics | AGPL-3.0 |
| ultralytics-thop | AGPL-3.0 |
| torch, torchvision | BSD-3-Clause |
| numpy | BSD-3-Clause |
| opencv-python | Apache-2.0 |
| pillow | MIT-CMU |
| matplotlib | PSF-based (matplotlib licence) |
| polars, polars-runtime-32 | MIT |
| sympy, mpmath | BSD-3-Clause |
| networkx | BSD-3-Clause |
| jinja2, markupsafe | BSD-3-Clause |
| filelock, fsspec | Unlicense / BSD-3-Clause |
| pyyaml | MIT |
| psutil | BSD-3-Clause |
| requests, urllib3, idna, certifi, charset-normalizer | Apache-2.0 / MIT / MPL-2.0 |
| nvidia-ml-py | BSD-3-Clause |
| setuptools, packaging, six, python-dateutil | MIT / Apache-2.0 / BSD |

### Launcher environment (`venv`)

| Package | Licence |
|---|---|
| customtkinter | MIT |
| pandas | BSD-3-Clause |
| numpy, scipy | BSD-3-Clause |
| matplotlib | matplotlib licence (PSF-based) |
| scikit-image | BSD-3-Clause |
| opencv-python | Apache-2.0 |
| tables (PyTables), blosc2 | BSD-3-Clause |
| h5py | BSD-3-Clause |
| openpyxl, et-xmlfile | MIT |
| tifffile, imagecodecs | BSD-3-Clause |
| imageio, imageio-ffmpeg | BSD-2-Clause |
| pydantic, pydantic-core | MIT |
| httpx, httpcore, h11, h2, hpack, hyperframe, anyio | BSD-3-Clause / MIT |
| rich, pygments, markdown-it-py, mdurl | MIT / BSD-2-Clause |
| numexpr, ndindex, msgpack, threadpoolctl, py-cpuinfo, lazy-loader, darkdetect, tzdata | MIT / BSD |

The exact version of every package in a given build is recorded in
`_wheels-manifest.txt` inside that installation.

### Other bundled components

| Component | Licence |
|---|---|
| **CPython** (the bundled interpreter) | Python Software Foundation License 2.0 |
| **ffmpeg / ffprobe** (GPL build, BtbN or gyan.dev) | GPL-3.0 — source at <https://ffmpeg.org/download.html> |
| **Inno Setup** (build tool only, not redistributed) | Inno Setup licence |

---

## Used at run time but not redistributed

| Component | Licence | Note |
|---|---|---|
| **Tierpsy Tracker** (`docker.io/tierpsy/tierpsy-tracker`) | MIT | Pulled from Docker Hub by the user, never shipped inside the installer. |
| **Rancher Desktop** | Apache-2.0 | Installed by the user via winget, never shipped. |

---

## The staging model

`staging.pt` was trained with ultralytics. Ultralytics takes the position that
AGPL-3.0 extends to models produced with their software. That reading is
contested — model weights are arguably not a derivative work of the training
code — but WormScan being AGPL-3.0 makes the question moot for this project:
the weights are distributed on the same terms as everything else here.

---

## Corresponding source

AGPL-3.0 requires that recipients can obtain the complete corresponding source
of the work. For WormScan that is:

**<https://github.com/davidtheadmin/celegans-imaging>**

If you received an installer without a working link to that repository, please
open an issue there or contact the maintainer.
