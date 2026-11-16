# OmniQuadGUI

A small Ubuntu desktop app that connects over SSH to the onboard computer of a
drone (Radxa Rock 5B, ROS 2, Docker) and shows that connection in a large,
always-visible terminal, with buttons for container management and ROS topic
inspection.

**It does not control flight.** Use it only while the drone sits on the desk,
with the battery disconnected or rotors removed.

---

## 1. Install (once)

On a fresh Ubuntu desktop (tested style: 22.04 / 24.04), open a normal
terminal and run:

```bash
sudo apt update
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 sshpass
```

| Package            | What it is for                                            |
|--------------------|-----------------------------------------------------------|
| `python3-gi`       | Lets Python use GTK (the graphical toolkit)               |
| `gir1.2-gtk-3.0`   | GTK 3 itself                                              |
| `gir1.2-vte-2.91`  | The embedded terminal widget (like GNOME Terminal's core) |
| `sshpass`          | Feeds the SSH password without showing it on screen       |

On the drone side, SSH must be enabled and the user (default `rock`) must be
allowed to log in with a password.

## 2. Run

```bash
cd OmniQuadGUI
python3 main.py
```

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

| File            | Purpose                                                        |
|-----------------|----------------------------------------------------------------|
| `main.py`       | The whole app (start window, connection, terminal).            |
| `config.json`   | Settings: user name, last-used IP, `dry_run` flag. Editable.   |
| `commands.json` | The button definitions (labels + commands). Data only.         |
| `README.md`     | This file.                                                     |

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

## 4. How the SSH connection works (why there is only one password prompt)1. When you click **Connect**, the app first runs a quick non-interactive
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
its full output, is appended there with a timestamp. The buttons
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
  log). The connection dropping afterwards is expected — you'll see
  "Shutdown sent, connection closed" and all drone buttons become
  disabled, since the drone is off.

### dry_run (default ON)
With `"dry_run": true` in `config.json`, everything above happens
normally (stopping the container, verification) **except** the final
`sudo shutdown`: the log prints exactly what it *would* have run.
Set `"dry_run": false` in `config.json` only when you really want the
drone to shut down from the app.

## 6. Current status

- [x] **Stage 1** — start window, connection check with plain-English
      errors, main window with the always-visible SSH terminal.
- [x] **Stage 2** — host buttons: check containers (list in the GUI),
      start/stop the Fast-LIO container safely, two-step shutdown
      (with `dry_run` on), plus the background command log under the
      terminal.
- [ ] **Stage 3** — container shell entry/exit with safety flag, ROS topic
      list, echo/hz buttons.
