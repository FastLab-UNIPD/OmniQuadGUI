# OmniQuadGUI

A desktop app that connects over SSH to the onboard computer of a
drone (Radxa Rock 5B, ROS 2, Docker) for container and ROS diagnostics. Ubuntu includes an embedded terminal and
container management. Windows includes a native diagnostics window and a
separate interactive SSH terminal.

**It does not control flight.** Use it only while the drone sits on the desk,
with the battery disconnected or rotors removed.

---

## Windows with an existing Conda environment

From the repository folder in your activated Conda prompt, run:

```powershell
python main.py
```

The desktop window uses Python's Tkinter. Select **Offline demo** and click
**Running containers** or **ROS topics** to try it immediately. No network
connection or local Docker engine is required.
Use **Filter by name** to narrow the displayed results locally.

For live use, enter the SSH user and host, then choose **Open SSH terminal**.
Windows OpenSSH handles the password prompt or SSH key, and asks you to verify
a new host. The two list buttons run read-only queries in the background.
They require a previously verified host and a working SSH key or agent.
If you use password authentication, you can run commands in the interactive
terminal. Container start/stop, the embedded terminal and shutdown controls
are available in the Ubuntu interface.

## 1. Install on Ubuntu (once)

On a fresh Ubuntu desktop (tested style: 22.04 / 24.04), open a normal
terminal and run:

```bash
sudo apt update
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 sshpass \
    openssh-client
```

| Package              | What it is for                                            |
|----------------------|-----------------------------------------------------------|
| `python3`            | Python 3 itself (installed automatically with `python3-gi`) |
| `python3-gi`         | Lets Python use GTK (the graphical toolkit)               |
| `gir1.2-gtk-3.0`     | GTK 3 itself                                              |
| `gir1.2-vte-2.91`    | The embedded terminal widget (like GNOME Terminal's core) |
| `gir1.2-pango-1.0`   | Text rendering (coloured dots, fonts) — installed automatically together with `gir1.2-gtk-3.0` |
| `gir1.2-gdk-pixbuf-2.0` | Loading the logo image — installed automatically together with `gir1.2-gtk-3.0` |
| `sshpass`            | Feeds the SSH password without showing it on screen       |
| `openssh-client`     | The `ssh` program itself (used for every connection; installed automatically on Ubuntu, listed here so nothing is missing) |

The app itself uses **only the Python standard library** (json, os,
subprocess, threading, …) plus the GTK/VTE libraries above — there is
**nothing to install with `pip`**.

Ubuntu's package manager installs `gir1.2-pango-1.0`,
`gir1.2-gdk-pixbuf-2.0` and `openssh-client` automatically as
dependencies of the packages above, so the short `apt install` line is
enough — the table simply shows the complete list of libraries the app
touches.

On the drone side, SSH must be enabled and the user (default `rock`) must be
allowed to log in with a password.

## 2. Run on Ubuntu

```bash
cd OmniQuadGUI
python3 main.py
```

or double-click **`OmniQuadGUI.desktop`** inside the folder in the file
manager (right-click it once and choose **Allow Launching** first).
The launcher resolves the application path relative to its own folder
and uses Ubuntu's system Python, so the folder (including `launch.sh`) can
be copied to any PC, for any user, to any location. On the first start
`launch.sh` copies the app icon into the user's private icon folder, so
the icon shows up in the file manager and the taskbar from then on.
From a terminal, use `./launch.sh` (or `python3 main.py`) from this
directory; `launch.sh` resolves the application path itself.

The app also cleans ROS 2 leftovers (from a sourced `setup.bash`) out of
its own environment at startup, so it behaves the same whether it is
started from the launcher, from `launch.sh`, or from a terminal that has
ROS sourced.

- **Start window:** type the drone's IP (e.g. `192.168.1.50`), the user
  (default `rock`) and the password, then click **Connect**.
- If the connection fails you get a plain-English message, for example
  *"wrong password"* or *"could not reach the drone — check it is powered
  on and both machines are on the same network"*.
- On success the main window opens with the drone's terminal embedded.

### Demo view (no drone needed)
Log in with the **demo credentials** (default: user `demo`, password
`demo`, both changeable in `config.json`). The GUI opens normally —
title shows **DEMO (not connected)** — but:

- no SSH session is opened and no drone is contacted anywhere;
- the terminal only shows a demo banner (it is not interactive);
- all buttons work against **simulated drone answers**: the container
  list shows a fake container, Start/Stop/shutdown run their full
  sequences with a small pretend state, and the log area fills as
  usual. Perfect for trying the GUI before the drone is on your desk.

The last successfully used IP is remembered in `config.json`.
**The password is never saved anywhere** — you type it on every start.

## 3. Files in this folder

| File              | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| `main.py`         | Selects the native desktop for the current OS.   |
| `gtk_app.py`      | Ubuntu interface, VTE terminal and container management. |
| `windows_app.py`  | Native Windows diagnostics and SSH terminal launcher. |
| `diagnostics.py`  | Shared settings, diagnostic commands and parsing. |
| `config.json`     | Settings: user name, last-used IP, `dry_run` flag. Editable.   |
| `commands.json`   | The button definitions (labels + commands). Data only.         |
| `omniquadgui.png` | The app logo (login background, window + shortcut icon).       |
| `launch.sh`       | Portable launcher script used by `OmniQuadGUI.desktop`; finds its own folder, installs the icon once, then starts the app. |
| `OmniQuadGUI.desktop` | The double-clickable launcher icon inside the folder (portable: no user name or absolute path). |
| `PROGRESS.txt`    | Handoff/progress notes for continuing development on the app. |
| `omniquadgui.svg` | The same logo as vector graphic (source of the PNG).           |
| `README.md`       | This file.                                                     |

`config.json` looks like this:

```json
{
    "user": "rock",
    "ip": "",
    "dry_run": true,
    "demo_user": "demo",
    "demo_password": "demo"
}
```

- `user` — the login name on the drone.
- `ip` — remembered automatically after a successful connect.
- `dry_run` — leave `true` for now: the shutdown button (stage 2) will
  *print what it would run* instead of actually shutting the drone down.
- `demo_user` / `demo_password` — login that opens the GUI in demo mode
  (no drone connection, simulated answers). Not a security feature —
  just a preview mode.

## 4. How the SSH connection works (why there is only one password prompt)

1. When you click **Connect**, the app first runs a quick non-interactive
   check: `sshpass -e ssh rock@<ip> echo ok`.
   - `sshpass -e` reads the password from the `SSHPASS` environment
     variable, so the password is **never on a command line** and is
     **never written to disk or logs**.
   - If this check prints `ok`, the IP, password and host key are all good.
2. Then the app opens the real interactive session inside the embedded
   terminal:
   `sshpass -e ssh -o ControlMaster=yes -o ControlPath=<socket>
   -o StrictHostKeyChecking=accept-new rock@<ip>`
   - `ControlMaster=yes` + `ControlPath=<socket>` make this one session the
     **master connection**. It stays open as long as the app window is open.
   - Because the check already verified the password, this session logs in
     silently — no second password prompt.
3. In stage 2, every button that needs to *read* something from the drone
   (container lists, safety checks) runs its own short, non-interactive
   `ssh -o ControlPath=<socket> rock@<ip> "<command>"`. That reuses the
   master connection through the socket file, so **no password is needed
   again**. The GUI never freezes while these run: they happen in a
   background thread, and the window is only updated when the result is
   ready.
4. The socket lives in a private temporary folder that the app deletes
   when you close the window. Closing the window also politely closes the
   master SSH connection.

The terminal itself always stays visible: buttons that you "watch live"
simply type their command into that terminal, exactly as if you had typed it.

## 5. Stage 2: the host buttons

The right-hand column has four buttons (built from `commands.json`), and
the **left half** of the window is split 50/50 (drag the divider to
change it): the terminal on top, and below it the small
**Background command log** — every command the app runs remotely, and
its output, is appended there with a timestamp. The on-screen log keeps
the most recent 5,000 lines, up to 512,000 characters. The buttons
themselves never freeze the window — the ssh commands run in background
threads and reuse the master connection
(`ssh -o ControlPath=<socket> rock@<ip> "<command>"`), so no password is
typed a second time.

Pasting into the terminal: **right-click → Paste**, or the keyboard
shortcut **Ctrl+Shift+V** (plain Ctrl+V does nothing in terminals —
that is normal). Copy is right-click → Copy or Ctrl+Shift+C.

### Check running containers
Runs `docker ps` **without sudo**. The running containers appear as a
list in the window (name + status). If the user (e.g. `rock`) is not
allowed to use docker, you get a clear explanation with the exact
`sudo usermod -aG docker rock` command to fix it on the drone.

### Start Fast-LIO container
Runs `./nuc_helper.sh run --mode run_nodes --config ~/mid360_custom.yaml`
in the drone's home directory and logs the result.

### Stop Fast-LIO container (safe sequence)
1. Checks with `docker ps` whether `px4_fastlio_bridge` is running at all.
   If not: reports "already stopped" and does nothing else.
2. If you had entered the container shell with the app's own button
   (stage 3), it first sends Ctrl+C, waits 0.5 s and types `exit` — the
   app only ever sends a bare `exit` when it knows you are inside the
   container, because otherwise it would close the whole ssh session.
3. Runs `./nuc_helper.sh stoprm`. An error here is not fatal by itself.
4. Verifies with `docker ps` that the container is really gone and
   reports the result.

### Shutdown (two clicks)
- First click **arms** it: the button is replaced by
  **Confirm shutdown** / **Cancel**.
- **Confirm shutdown** always runs the stop sequence (steps 2–4 above),
  even if the container looked stopped.
- SAFETY-CRITICAL: if the verification shows the container still
  running, or the check itself fails, the shutdown is **aborted** with a
  loud warning popup. The drone keeps running.
- Only when the container is verified stopped, the app runs
  `sudo -S -p '' shutdown now`, sending the login password through the
  ssh process's standard input (never in the command text, never in the
  log). If SSH acknowledges the command, the controls are disabled.
  If the connection drops before SSH returns success, the GUI reports that
  shutdown could not be confirmed.

### dry_run (default ON)
With `"dry_run": true` in `config.json`, everything above happens
normally (stopping the container, verification) **except** the final
`sudo shutdown`: the log prints exactly what it *would* have run.
Set `"dry_run": false` in `config.json` only when you really want the
drone to shut down from the app.

## 6. Stage 3: container shell and ROS buttons

The right side of the window has **two button columns** to keep things
tidy: *Host commands* (docker, container start/stop, shutdown, and the
container list) on the left, and *Container shell / ROS* on the right.
The buttons carry small emojis (📦 enter, 🚪 leave, 📋 topics, 📡 LiDAR,
…) so you can find the right one at a glance — the labels, including the
emojis, live in `commands.json`. These buttons *type commands into the
terminal* (and the log notes each one), so they only work while you are
inside the container shell:

### Enter PX4 bridge container / Leave container
- **Enter** is only usable while a `docker ps` check says the
  `px4_fastlio_bridge` container is actually **running** (the app
  checks automatically after connecting, and after every
  start/stop/verify — so the button always reflects reality).
  When clicked it types `docker exec -it px4_fastlio_bridge bash` into
  the terminal and flips the app's `inside_container_shell` flag. Only
  then do the ROS buttons become usable.
- **Leave** sends Ctrl+C, waits 0.5 s, then types `exit`. A bare `exit`
  is *only ever* sent while the app knows you are inside the container —
  on the drone's normal shell it would close the whole ssh session.
  **Please always leave with the button**, not by typing `exit` by hand:
  the app cannot see manual exits, and its flag would go out of sync.

### Stop current command
Sends Ctrl+C into the terminal — use it to stop `ros2 topic echo` or
`ros2 topic hz`, which run forever until interrupted. It is the **big
button at the bottom of the left column** (easy to reach), and it is
usable only while inside the container shell, like the other ROS
buttons. In the separate echo windows, press Ctrl+C directly in that
window's terminal instead.

### List available topics
Reads the configured topic list command in a separate interactive Bash inside
the configured container, using the existing SSH master connection. Results
come from that command's output, so terminal width, scrollback and the VTE
version do not affect the list. The main terminal stays available while the
request runs. A refresh replaces the old list and selection. If the container
is still starting, refresh again after its nodes have come up.
Type part of a name into **Filter topics by name** to search the current list
without sending another SSH request.

### Echo selected topic

Select a topic with the mouse or keyboard, then click **Echo selected topic**
to open a dedicated echo terminal for it.

### The fixed ROS buttons
Each types its fixed command into the terminal (definitions live in
`commands.json`):

| Button | Command typed |
|---|---|
| Verify odometry | `ros2 topic echo /Odometry` |
| Specific position data | `ros2 topic echo /Odometry --field pose.pose.position` |
| Verify covariances | `grep -E "acc_cov\|gyr_cov" /root/ros2_ws/install/fast_lio/share/fast_lio/config/mid360_override.yaml` |
| Verify network buffer sizes | `cat /proc/sys/net/core/rmem_max` |
| Check Livox IMU frequency | `ros2 topic hz /livox/imu` |
| Check Livox LiDAR frequency | `ros2 topic hz /livox/lidar` |

Remember: echo/hz print continuously — press **Stop current command**
(or Ctrl+C in the terminal) when you have seen enough.

### Separate echo windows
Every `ros2 topic echo ...` button (🔁 Echo selected topic, 🧭 Verify
odometry, 📍 Specific position data) opens its **own terminal window**:
it starts an ssh session (reusing the master connection, no password),
enters the container and starts the echo inside it. Because each echo
has its own window, you can echo **several topics at the same time**.
Close a window (or press Ctrl+C inside it) to stop that echo. The `hz`
commands keep running in the main terminal as before.

## 7. Current status

- [x] **Stage 1** — start window, connection check with plain-English
      errors, main window with the always-visible SSH terminal.
- [x] **Stage 2** — host buttons: check containers (list in the GUI),
      start/stop the Fast-LIO container safely, two-step shutdown
      (with `dry_run` on), plus the background command log under the
      terminal.
- [x] **Stage 3** — container shell entry/exit with safety flag, ROS
      topic list (clickable), echo/hz buttons.

All three stages are complete.


## Run the offline checks

From the repository root, run `python -B -m unittest discover -s tests -v`.
On Ubuntu, use the system Python with a display, or run
`xvfb-run -a /usr/bin/python3 -B -m unittest discover -s tests -v`.
The pull request checks exercise the GTK demo on Ubuntu 22.04 and 24.04.
