# NiNog Raker v2.0 | THE RATTIKANS

A terminal toolkit for Discord guild administration, snapshots, restores, workflows, and bulk operations.

> **New to this?** Skip straight to your device: [Android (Termux)](#-android-termux) · [Windows](#-windows) · [Linux](#-linux) · [macOS](#-macos). Every step is copy-paste. You don't need to know how to code.

---

## Table of contents

1. [What you need](#what-you-need)
2. [Android (Termux)](#-android-termux)
3. [Windows](#-windows)
4. [Linux](#-linux)
5. [macOS](#-macos)
6. [Running it](#running-it)
7. [Folders it creates](#folders-it-creates)
8. [Updating](#updating)
9. [Troubleshooting](#troubleshooting)
10. [FAQ](#faq)
11. [Community](#community)

---

## What you need

| Thing | Why |
|---|---|
| **Python 3.10 or newer** | The source of this tool is written in Python |
| **4 packages:** `rich`, `pyfiglet`, `requests`, `websockets` | Terminal UI, network access, and presence sorting. |
| **The script files** | Downloaded from this repo |
| **A terminal** | The black window where you type commands (each section below tells you which one) |

**For new users:**

- **Terminal / console / shell:** a text window where you type commands and press Enter.
- **Repo:** this page and all the files on it.
- **Clone:** download the repo with Git.
- **pip:** Python's app installer, it grabs the 3 packages above.

---

## 📱 Android (Termux)

Works on most Android phones and tablets. No root needed.

### 1. Install Termux (the right one)

> ⚠️ **Do NOT install Termux from the Google Play Store.** That version is outdated and broken. Use F-Droid or GitHub.

**Option A: F-Droid (recommended)**
1. Open your browser and go to **f-droid.org**
2. Download and install the **F-Droid** app (Android will ask you to allow installs from your browser, tap *Allow*)
3. Open F-Droid, search **Termux**, tap **Install**

**Option B: GitHub**
1. Go to **github.com/termux/termux-app/releases**
2. Download the `.apk` matching your phone (most phones: `arm64-v8a`)
3. Install it

### 2. Set up Termux

Open Termux. Type each line or copy it and paste to the terminal, press Enter, and wait for it to finish. If it asks `Do you want to continue? [Y/n]`, type `y` and press Enter.

```bash
pkg update -y && pkg upgrade -y
pkg install -y python git
```

*(If it asks about replacing config files, just press Enter to keep the default.)*

### 3. Download the script

```bash
git clone https://github.com/therattikans/ninog-raider-v2.git
cd ninog-raider-v2
```

### 4. Install the packages

```bash
pip install -r requirements.txt
```

### 5. Run it

```bash
python ninog/main.py
```

### Termux tips

- **Keep it running with the screen off:** swipe down the Termux notification and tap **Acquire wakelock**.
- **Access your phone files:** run `termux-setup-storage` once and allow the permission. Your exports will then be reachable at `~/storage/`.
- **Copy/paste:** long-press the screen. Volume Down + `C`/`V` also works as a shortcut on many builds.
- **Extra keys (Ctrl, Tab, arrows):** there's a key row above your keyboard. Swipe it left for more.
- **Stop the script:** press `Ctrl + C`. Use the Termux key row or Volume Down + `C`.
- **Make the text bigger/smaller:** pinch to zoom.

---

## 🪟 Windows

Works on Windows 10 and 11.

### 1. Install Python

1. Go to **python.org/downloads** and click the big yellow **Download Python** button
2. Run the installer
3. ✅ **IMPORTANT: tick the box "Add python.exe to PATH" at the bottom of the first screen**
4. Click **Install Now**

### 2. Get the script

**Easy way (no Git):**
1. On this repo's page, click the green **Code** button → **Download ZIP**
2. Right-click the ZIP → **Extract All**
3. Remember where you extracted it (for example, your Downloads folder)

**Git way:** install Git from **git-scm.com**, then run the `git clone` command from the Termux section above.

### 3. Open a terminal in that folder

1. Open the extracted folder in File Explorer (the one that contains the `ninog` folder)
2. Click the address bar at the top, type `cmd`, and press Enter

A black window opens, already in the right place.

### 4. Install the packages

```bat
pip install -r requirements.txt
```

If `pip` isn't recognized, try:

```bat
py -m pip install -r requirements.txt
```

### 5. Run it

```bat
python ninog\main.py
```

or

```bat
py ninog\main.py
```

### Windows tips

- Use **Windows Terminal** (free in the Microsoft Store) instead of old `cmd` if you can. Colors and the ASCII banner look much better.
- If text looks garbled or boxes look weird, run `chcp 65001` first, then start the script.
- Windows Defender / SmartScreen popups: only ignore or disable if you downloaded the files from this official repo. (if you downloaded from somewhere else, do a malware scan immediately)

---

## 🐧 Linux

### 1. Install Python + Git + pip

**Debian / Ubuntu / Mint / Kali / Pop!_OS:**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

**Fedora:**
```bash
sudo dnf install -y python3 python3-pip git
```

**Arch / Manjaro:**
```bash
sudo pacman -S --needed python python-pip git
```

### 2. Download the script

```bash
git clone https://github.com/therattikans/ninog-raider-v2.git
cd ninog-raider-v2
```

### 3. Install the packages

**Simple way:**
```bash
pip3 install -r requirements.txt
```

**If you see `externally-managed-environment`** (new Debian/Ubuntu/Kali), use a virtual environment. It's cleaner anyway:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Every time you open a new terminal, run `source .venv/bin/activate` inside the repo folder before starting the script.

### 4. Run it

```bash
python3 ninog/main.py
```

---

## 🍎 macOS

1. Open **Terminal** (Cmd + Space → type "Terminal")
2. Install Homebrew if you don't have it: **brew.sh** (copy the one-line installer from the page)
3. Then:

```bash
brew install python git
git clone https://github.com/therattikans/ninog-raider-v2.git
cd ninog-raider-v2
pip3 install -r requirements.txt
python3 ninog/main.py
```

If pip complains about `externally-managed-environment`, use the virtual environment steps from the Linux section.

---

## Running it

From the repo root:

```bash
python ninog/main.py
```

> On Linux, macOS, and Termux you may need `python3` instead of `python`. On Windows, `py` also works.

**Stopping it:** `Ctrl + C`.

---

## Folders it creates

These are created automatically next to `main.py` the first time they're needed. You don't have to make them. They're git-ignored, so they never get uploaded.

| Folder | What's in it |
|---|---|
| `config/` | Your saved settings |
| `snapshots/` | Saved snapshots |
| `reportlog/` | Logs of reports |
| `exports/` | Anything you export |

Want a fresh start? Close the script and delete the folder you want to reset.

---

## Updating

If you cloned with Git:

```bash
cd ninog-raider-v2
git pull
pip install --upgrade -r requirements.txt
```

If you downloaded the ZIP, download the new ZIP and replace the files. Copy your old `config/` folder into the new one to keep your settings.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python: command not found` | Try `python3` (Linux/macOS/Termux) or `py` (Windows). On Windows, reinstall Python and tick **Add to PATH** |
| `pip: command not found` | Use `python -m pip install -r requirements.txt` (or `python3 -m pip ...`) |
| `ModuleNotFoundError: No module named 'rich'` (or `pyfiglet` / `requests`) | Packages didn't install. Rerun the `pip install` line. If you use a venv, make sure it's activated |
| `can't open file '.../ninog-raider-v2/main.py'` | Run from the repository root with `python ninog/main.py`. `ls` (or `dir`) should show both `ninog` and `requirements.txt`. |
| `externally-managed-environment` | Use the virtual environment steps in the Linux section |
| Banner/colors look broken | Use a modern terminal (Windows Terminal, Termux, any Linux terminal). On Windows try `chcp 65001` |
| `Permission denied` | Don't use `sudo` with pip. Use a venv or `pip install --user ...` |
| Termux: `pkg` errors / mirrors down | Run `termux-change-repo`, pick a mirror, then `pkg update` again |
| Termux closes / kills the script | Acquire the wakelock (see Termux tips) and disable battery optimization for Termux in Android settings |
| Connection / SSL errors | Check your internet. Update packages: `pip install --upgrade requests certifi` |
| Still stuck | Ask in the Discord (below) and paste the **full** error message directly into THE RATTIKANS's DMs, or into an Administrator's DMs. (Red Administrator role)|

---

## FAQ

**Do I need to know how to code?**
No. Follow the steps for your device and copy-paste the commands.

**Do I need root on Android?**
No.

**Can I run it on iPhone?**
Not officially. iOS has no real terminal. Apps like iSH may work, but they're unsupported. You can use Replit too.

**Which Python version?**
3.10 or newer. Check yours with `python --version`.

**I closed the terminal. How do I start again?**
Open the terminal, `cd` into the repo folder, and run the run command again. You only install once.

**Where are my settings?**
In the `config/` folder next to `main.py`.

`config/`, `snapshots/`, `reportlog/` and `exports/` are created on demand
next to `main.py` and are git-ignored.

## What's inside (v2.0)

- **Role Engine** (replaces Get Admin since it was ass): build a permission profile, can be admin,
  moderator, channel manager, custom bit-toggles, and apply it to *anyone* (that means you can give admin to everyone!)
- **Universal member picker**: every target prompt offers five methods so you don't get stuck jumping from Discord to terminal and having to import userids all the time.
- **Extract Webhooks**: pulls every webhook URL out of the guild (guild-wide
  endpoint + per-channel sweep) and delivers them to you so you can fuck someone up without them noticing. read only.
- **Ban detection**: Watchdog just watches for the bot exiting the server, lets you know so you don't slap a nonexistent cake.
- **Workflow engine** pretty simple: chain ops, pre set the actions, and it fires hands-free. 
- **Tool Management page**: Diagnoser (source hashes + compile check, config
  JSON/unicode validation, workflow file validation, all the good stuff)
  Report Explorer included.
  (session logs + crash bundles, paginated, with delete), and Support Bundle.
- **Crash reporter**: any unplanned error, could be inside an op or escaping the
  session, writes a full bundle to `reportlog/crashes/`. You can simply send it over to us, and we'll supply a fix.
- Snapshots before every destructive op, restore for roles/channels/settings/
  messages/bans, etcetera.

## Layout

```
ninog/
├── main.py          bootstrap + session loop
└── src/
    ├── core.py      paths, config, vault, REST client, watchdog, crash reporter
    ├── ui.py        themes, gradient, bouncing-R live displays, transitions
    ├── workflow.py  workflow engine: chained ops with pre-set inputs
    └── ops.py       every interactive flow and operation
```

- Partial assist with Opus 4.8, and our cybersecurity agent.
- Thanks for waiting.
