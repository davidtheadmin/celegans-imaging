# Installing WormScan

Written for someone who has never installed scientific software before.
You do **not** need Python, administrator rights, or any technical knowledge to
follow this.

If something goes wrong, jump to [When something goes wrong](#when-something-goes-wrong)
at the end.

---

## Before you start

- **About 3 GB of free disk space.** WormScan settles at roughly 2 GB, but it
  needs more than that *while* installing: the packages inside the installer
  are compressed and expand to around three times their size on disk. The
  installer checks this first and tells you exactly how much you are short.
- **No administrator rights.** Nothing here needs them.
- **No Python.** WormScan brings its own.

---

## What you will end up with

- **WormScan** on your desktop and in the Start Menu
- **Colony Survival** and **Worm Survival** working immediately
- **Motility** and **Crawling** working after one extra step, which is
  described further down and which you can do later or never

---

## Step 1 — Install WormScan

You will be sent one file: **`WormScanSetup-<date>.exe`**. It is around 500 MB,
so it will usually arrive as a download link rather than an email attachment.

1. Download it, and note where it went (usually your **Downloads** folder).
2. **Double-click** the file.

### If Windows shows a blue "Windows protected your PC" box

This is expected. It appears for any program that has not been through
Microsoft's paid signing process, which this one has not.

- Click **More info**
- Then click **Run anyway**

### Then

3. Click **Next** through the installer. The default location is fine.

   > If you change it and the installer says the folder is **too deep for
   > Windows**, pick something shorter — `C:\WormScan` always works. Windows
   > caps file paths at 260 characters and one file inside PyTorch uses 167 of
   > them on its own, so there is less room than you would expect. The
   > installer checks this before copying anything, so you lose nothing but a
   > click.
4. Tick **Create a desktop shortcut** if you want one.
5. Click **Install**.

The installer copies its files, then shows **"Setting up WormScan"** and opens a
black window with text scrolling past. **This is normal — leave it alone.**
It is building the Python environment WormScan runs in.

> This step takes **3 to 10 minutes** depending on the machine. The black window
> closes by itself when it is finished. Do not close it yourself.

6. When the installer says it is done, click **Finish**.

That is the whole installation. You will not be asked for an administrator
password at any point, and nothing else on your computer is changed.

---

## Step 2 — First time you open it

Double-click **WormScan**.

The dot in the corner will be **red**, because it does not yet know where the
imaging Pi is. To fix that:

1. Click **Settings** (bottom right)
2. Fill in the two values David gives you:
   - **Pi URL** — the address of the imaging Pi, e.g. `http://192.168.50.2:8000`
   - **Token** — the access code. Treat it like a password.
3. **Mirror folder** is where synced images will land on your laptop. It
   defaults to `Documents\WormScan`, which is usually what you want.
4. Click **Save**

Within a minute the dot should turn **green**, as long as your laptop is
connected to the Pi by ethernet and the Pi is switched on.

**You can now use Colony Survival and Worm Survival.** Nothing further is needed
for those.

---

## Step 3 — Only if you need Motility or Crawling

These two analyses use a program called Tierpsy, which runs inside a container.
That container needs a piece of software called a container engine, and it is
too large and too system-level to be included in the WormScan installer.

Setting it up is one shortcut:

1. Open the **Start Menu**
2. Find **WormScan — Set up video analysis**
3. Click it

It will walk you through the whole thing and tell you what is happening at each
stage. In summary, it:

- checks whether you already have a container engine (many people do)
- checks whether Windows Subsystem for Linux is present, because nothing works
  without it
- installs a container engine if you have none
- waits for it to start
- downloads Tierpsy (several GB, once only)
- checks everything works

### Two things to be ready for

**It will ask for administrator permission.** Installing a container engine
changes system settings, so Windows will show a permission prompt. If you do not
have administrator rights on this laptop, ask your IT department to run this one
step for you.

**Windows Subsystem for Linux has to be there first.** Every container engine
sits on WSL2, and none of them will install it for you - Rancher Desktop's
installer checks for it and simply stops with a message pointing at a Microsoft
page. Enabling it is the one genuinely administrator-only step:

```
wsl --install --no-distribution
```

followed by a **restart**. `--no-distribution` keeps it minimal: no Ubuntu is
added, because the container engine creates its own environment.

After that one step, nothing else needs administrator rights. If you do not have
them, this is the single thing to ask your IT department for - and it is worth
telling them it is one command plus a reboot, not an open-ended request.

**One setting you must choose yourself.** After Rancher Desktop installs, open
it and go to:

> Preferences → Container Engine → **dockerd (moby)**

This is the setting that lets WormScan talk to it. The script reminds you at
the right moment.

### After it is set up

Rancher Desktop needs to be **running** before you start a Motility or Crawling
analysis. It normally starts automatically with Windows. If WormScan tells you
the engine is not running, open Rancher Desktop, wait for it to settle, and try
again.

---

## Updating to a newer version

WormScan checks once a day whether a newer version has been released, and shows
a line at the top of the window if there is one:

> Update available: 2026.09.01 — click to download

Clicking it opens the download page in your browser. **Nothing is downloaded or
installed automatically** — you stay in control, and an update can never
interrupt an analysis.

Then run the new `WormScanSetup-<date>.exe` on top of the old one. Your
settings, your synced images and any custom model files are kept — they live
outside the program folder. You do not need to uninstall first.

### Turning the check off

The check is a single request to `github.com` when WormScan starts. It sends no
information about you or your data — it only asks what the latest version
number is, the same thing you would see by visiting the page yourself.

If your IT policy would rather it did not happen, untick **Check for updates on
startup** in **Settings**. Nothing else changes; you just have to be told about
new versions by hand.

---

## Getting a new model or new settings without reinstalling

Some things can be updated by dropping in a single file. If David sends you one
of these:

| File | What it changes |
|---|---|
| `staging.pt` | the worm staging model |
| `stage_conf.json` | staging detection thresholds |
| `motility_params.json` | motility analysis parameters |
| `crawling_params.json` | crawling analysis parameters |

Put it in your **WormScan data folder**, and restart WormScan.

To open that folder: **Start Menu → WormScan data folder**. (It is
`C:\Users\<you>\AppData\Roaming\WormScan`.)

A file placed there takes priority over the one that came with the program.
To go back to normal, delete the file you added. WormScan writes which files it
used into its log at the start of every run, so it is always possible to check.

---

## Uninstalling

**Settings → Apps → Installed apps → WormScan → Uninstall.**

Your data folder is deliberately left behind, so your settings and any custom
model files survive. Delete `C:\Users\<you>\AppData\Roaming\WormScan` by hand if
you want it gone too.

Rancher Desktop, if you installed it, is a separate program and is uninstalled
separately.

---

## When something goes wrong

### The installer's black window shows an error, or closes very fast

There is a log at:

```
C:\Users\<you>\AppData\Local\WormScan\install-log.txt
```

Send that file to David. It records every step and names the one that failed.

### WormScan will not start at all

Nothing appears when you double-click it — no window, no error. Check the log:

```
C:\Users\<you>\AppData\Roaming\WormScan\launcher.log
```

The quickest way to open it: press **Win + R**, paste
`%APPDATA%\WormScan`, press Enter.

### The dot stays red

WormScan cannot reach the Pi. Check, in this order:

1. Is the ethernet cable connected at both ends?
2. Is the Pi powered on?
3. Does the **Pi URL** in Settings exactly match what David gave you?
4. Is the **Token** correct? A wrong token looks the same as an unreachable Pi.

### Motility or Crawling says the engine is not running

Open Rancher Desktop and wait until it reports it is ready, then try again.
If it says the engine is not **installed**, run the **WormScan — Set up video
analysis** shortcut.

### Analysis is much slower than expected

Open Rancher Desktop → Preferences → Virtual Machine, and raise the CPU and
memory allocation. WormScan decides how many videos to process at once from
what the engine reports, so a starved engine means a slow analysis. The number
it chose is written at the top of every analysis log.

### Anything else

Send David:

1. `%APPDATA%\WormScan\launcher.log`
2. A description of what you clicked and what happened
3. The version number, from the top of that log file

**Contact**: [David's email — placeholder]

---

## For David — running from source

Unchanged, and still the right way to work day to day:

```bash
source launcher/.venv/Scripts/activate && python launcher/main.py
```

A source checkout and an installed copy resolve paths identically — the same
`launcher/` layout, the same tunable lookup, the same venvs in the same places.
The only differences in an installed copy are that `_build_info.json` exists
(so the log names a version instead of `dev`) and that `ffmpeg`/`ffprobe` are
bundled rather than taken from `PATH`.

To build an installer, see [`packaging/README.md`](../packaging/README.md).
