"""Windows diagnostics window using Tk and the system OpenSSH client."""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

from diagnostics import COMMANDS_FILE, CONTAINER_LIST, load_config, topic_list_command, topic_names


def ssh_argv(user, host, command=None):
    user, host = user.strip(), host.strip()
    if not user or not host or any(ch.isspace() for ch in user + host):
        raise ValueError("Enter an SSH user and an IP address or host name.")
    if host.startswith("-") or "@" in host or "@" in user:
        raise ValueError("Enter the user and host in their separate fields.")
    exe = Path(os.environ["SystemRoot"]) / "System32" / "OpenSSH" / "ssh.exe"
    if not exe.is_file():
        raise ValueError("The Windows OpenSSH Client is unavailable. Offline demo is available.")
    args = [str(exe), "-o", "ConnectTimeout=10"]
    if command is not None:
        args.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"])
    args.extend(["-l", user, "--", host])
    if command is not None:
        args.append(command)
    return args


def run_query(args):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "The request timed out after 30 seconds."
    except OSError as error:
        return False, str(error)
    if result.returncode:
        return False, result.stderr.strip() or f"SSH exited with code {result.returncode}."
    return True, result.stdout


class DiagnosticsWindow:
    def __init__(self, root):
        self.root = root
        root.title("OmniQuadGUI · Diagnostics")
        root.geometry("760x480")
        root.minsize(560, 340)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        settings = load_config()
        self.user = tk.StringVar(value=settings["user"])
        self.host = tk.StringVar(value=settings["ip"])
        self.demo = tk.BooleanVar()
        self.status = tk.StringVar(value="Connect once in the terminal to verify the host. The lists use SSH key authentication.")
        self.results = queue.Queue()

        target = ttk.LabelFrame(frame, text="SSH connection", padding=10)
        target.pack(fill="x")
        ttk.Label(target, text="User").grid(row=0, column=0, sticky="w")
        ttk.Label(target, text="IP or host name").grid(row=0, column=1, sticky="w")
        user = ttk.Entry(target, textvariable=self.user, width=16)
        user.grid(row=1, column=0, padx=(0, 8), sticky="ew")
        host = ttk.Entry(target, textvariable=self.host)
        host.grid(row=1, column=1, padx=(0, 8), sticky="ew")
        host.focus_set()
        target.columnconfigure(1, weight=1)
        self.terminal = ttk.Button(target, text="Open SSH terminal", command=self.open_terminal)
        self.terminal.grid(row=1, column=2)

        actions = ttk.Frame(frame, padding=(0, 12))
        actions.pack(fill="x")
        containers = ttk.Button(actions, text="Running containers", command=lambda: self.query("containers"))
        topics = ttk.Button(actions, text="ROS topics", command=lambda: self.query("topics"))
        containers.pack(side="left", padx=(0, 6))
        topics.pack(side="left")
        demo = ttk.Checkbutton(actions, text="Offline demo", variable=self.demo, command=self.change_mode)
        demo.pack(side="right")
        self.inputs = (user, host)
        self.actions = (containers, topics, demo)

        panel = ttk.Frame(frame)
        panel.pack(fill="both", expand=True)
        self.table = ttk.Treeview(panel, columns=("name", "status"), show="headings")
        self.table.heading("name", text="Name")
        self.table.heading("status", text="Status")
        scrollbar = ttk.Scrollbar(panel, command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        ttk.Label(frame, textvariable=self.status, wraplength=520, justify="left").pack(
            fill="x", pady=(8, 0))

    def change_mode(self):
        self.table.delete(*self.table.get_children())
        self.status.set("Offline demo · simulated data" if self.demo.get() else "Ready")
        self.set_busy(False)

    def set_busy(self, busy):
        for widget in self.actions:
            widget.state(["disabled" if busy else "!disabled"])
        for widget in (*self.inputs, self.terminal):
            widget.state(["disabled" if busy or self.demo.get() else "!disabled"])

    def open_terminal(self):
        try:
            args = ssh_argv(self.user.get(), self.host.get())
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except (ValueError, OSError) as error:
            self.status.set(str(error))
            return
        self.status.set("SSH terminal opened. Complete authentication in that window.")

    def query(self, kind):
        self.table.delete(*self.table.get_children())
        self.table["displaycolumns"] = ("name",) if kind == "topics" else ("name", "status")
        self.table.heading("name", text="ROS topic" if kind == "topics" else "Container")
        if self.demo.get():
            output = "/Odometry\n/livox/imu\n/livox/lidar" if kind == "topics" else "px4_fastlio_bridge|Up 2 minutes"
            self.show_result(kind, True, output)
            return
        try:
            command = CONTAINER_LIST
            if kind == "topics":
                command = topic_list_command(json.loads(COMMANDS_FILE.read_text(encoding="utf-8")))
            args = ssh_argv(self.user.get(), self.host.get(), command)
        except (OSError, ValueError) as error:
            self.status.set(str(error))
            return
        self.set_busy(True)
        self.status.set("Reading " + kind + "…")

        def worker():
            self.results.put(run_query(args))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(80, self.poll, kind)

    def poll(self, kind):
        try:
            success, output = self.results.get_nowait()
        except queue.Empty:
            self.root.after(80, self.poll, kind)
            return
        self.set_busy(False)
        self.show_result(kind, success, output)

    def show_result(self, kind, success, output):
        if not success:
            self.status.set(output)
            return
        if kind == "topics":
            rows = [(name,) for name in topic_names(output)]
        else:
            rows = [line.split("|", 1) for line in output.splitlines() if "|" in line]
        for row in rows:
            self.table.insert("", "end", values=row)
        source = "Offline demo" if self.demo.get() else self.user.get() + "@" + self.host.get()
        self.status.set(f"{source} · {len(rows)} {kind}")


def main():
    root = tk.Tk()
    DiagnosticsWindow(root)
    root.mainloop()
