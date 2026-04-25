# Installing WormScan on a Windows laptop

These instructions are written for someone with no Python or programming experience.
Follow them in order. If anything goes wrong, see [Troubleshooting](#troubleshooting) below.

---

## Prerequisites — install Python once

1. Open a web browser and go to **https://www.python.org/downloads/**
2. Click the large yellow **Download Python** button (any version 3.11 or newer is fine)
3. Run the downloaded installer
4. **Important**: on the very first screen of the installer, tick the box labelled
   **"Add Python to PATH"** before clicking *Install Now*

   [screenshot placeholder — installer first screen with PATH checkbox circled]

5. Wait for the installer to finish, then close it

You only need to do this once. Skip this section on any machine where Python is already installed.

---

## Get the software

David will send you a **.zip file** containing the WormScan software.

1. Save the zip somewhere you will leave it permanently — for example, create a folder
   called **WormScan** directly on your C: drive: `C:\WormScan`
   > Do **not** put it in Downloads or on the Desktop — Windows sometimes deletes or
   > moves files in Downloads, and the shortcut you create next will stop working
   > if the folder moves.
2. Right-click the zip file and choose **Extract All…**
3. Set the destination to `C:\WormScan` (or wherever you chose) and click **Extract**

[screenshot placeholder — Extract All dialog]

---

## Run setup

You only need to do this once (or again after receiving an update from David).

1. Open the `WormScan` folder in File Explorer
2. Open the `launcher` subfolder inside it
3. **Double-click** `setup.bat`

   [screenshot placeholder — setup.bat in File Explorer]

4. A black command window will open and run automatically. It installs the software
   and creates a **WormScan** shortcut on your Desktop.

### If Windows SmartScreen appears

Windows may show a blue warning saying *"Windows protected your PC"*. This is normal
for software that is not yet digitally signed.

- Click **"More info"**
- Then click **"Run anyway"**

[screenshot placeholder — SmartScreen dialog with More info / Run anyway]

### If setup fails

The window will print an error message explaining what went wrong. Take a screenshot
of the entire window and send it to David. Do not close the window before taking the
screenshot — it will close on its own once you press any key.

---

## Launch WormScan

Once setup has finished, you will see a **WormScan** icon on your Desktop.

Double-click it to start the launcher. No terminal window should appear — just the
WormScan window.

[screenshot placeholder — WormScan launcher window]

---

## First-time settings

The first time you open WormScan, the sync indicator will show a red dot because the
Pi address and access token have not been entered yet.

1. Click the **Settings** button (bottom-right of the window)
2. Fill in the two fields David gives you:
   - **Pi URL** — the address of the imaging Pi (e.g. `http://192.168.50.2:8000`)
   - **Token** — the access token (treat this like a password)
3. The **Mirror folder** defaults to `Documents\WormScan` on your laptop — this is
   where synced images will appear. You can leave it as-is or change it.
4. Click **Save**

The status dot should turn green within a minute once the Pi is reachable.

---

## Troubleshooting

### The status dot is red

The launcher cannot reach the Pi. Check that:
- Your laptop is connected to the Pi by ethernet cable
- The Pi is powered on
- The Pi URL in Settings matches what David gave you

### Something else is wrong

The launcher writes a log file to:

```
C:\Users\<your username>\AppData\Roaming\WormScan\launcher.log
```

You can open this path by pressing **Win + R**, typing the path above (replace
`<your username>` with your Windows username), and pressing Enter.

Send the log file to David along with a description of what happened.

**Contact**: [David's email / Slack — placeholder]

---

## Updating the app icon

The current icon is a placeholder. When a final icon is ready, David will send a
replacement `wormscan.ico` file.

1. Copy the new `wormscan.ico` into the `launcher\assets\` folder inside your
   WormScan installation, replacing the existing file
2. Double-click `setup.bat` again — it will recreate the desktop shortcut so the
   new icon appears

You may need to right-click the Desktop and choose **Refresh** before the new icon
shows up.

---

## For developers (David)

Both launch paths are supported and kept in sync:

| Path | Command |
|------|---------|
| Dev (Git Bash, manual venv) | `source launcher/.venv/Scripts/activate && python launcher/main.py` |
| End-user (desktop shortcut) | Double-click the WormScan icon |

`setup.bat` is additive — it does not touch anything outside `launcher\.venv\` and
`%USERPROFILE%\Desktop\WormScan.lnk`. Re-running it is always safe.

Admin rights are **not** required. The venv is created inside the repo folder, and the
shortcut is written to the current user's Desktop. Neither operation needs elevation.
