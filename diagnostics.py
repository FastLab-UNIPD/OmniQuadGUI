"""Settings and read-only diagnostics shared by both desktop interfaces."""

import csv
import json
import os
import re
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
COMMANDS_FILE = APP_DIR / "commands.json"
CONTAINER_LIST = 'docker ps --format "{{.Names}}|{{.Status}}"'


def endpoint_file():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "OmniQuadGUI" / "connection.json"


def read_settings(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(path=CONFIG_FILE, state_path=None):
    settings = dict(user="rock", ip="", dry_run=True, demo_user="demo", demo_password="demo")
    stored = read_settings(path)
    for key, default in settings.items():
        if isinstance(stored.get(key), type(default)):
            settings[key] = stored[key]
    endpoint = read_settings(state_path or endpoint_file())
    if all(isinstance(endpoint.get(k), str) and endpoint[k].strip() for k in ("user", "ip")):
        settings.update(user=endpoint["user"], ip=endpoint["ip"])
    return settings


@contextmanager
def atomic_text_file(path, encoding):
    path = Path(path)
    temporary = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix="." + path.name + ".", mode="w", encoding=encoding,
        newline="", delete=False)
    try:
        with temporary as output:
            yield output
        os.replace(temporary.name, path)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def save_endpoint(user, host, path=None):
    path = path or endpoint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_file(path, "utf-8") as output:
        json.dump({"user": user, "ip": host}, output, ensure_ascii=False)


def export_csv(path, columns, rows):
    with atomic_text_file(path, "utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["'" + cell if cell.lstrip().startswith(("=", "+", "-", "@"))
                             else cell for cell in row])


def load_commands(path=COMMANDS_FILE):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Could not read {path.name}: {error}") from error
    if not isinstance(config, dict) or not isinstance(config.get("container_name"), str) or not config["container_name"].strip():
        raise ValueError("commands.json requires a non-empty container_name.")
    for group in ("host_buttons", "terminal_buttons"):
        if not isinstance(config.get(group), list):
            raise ValueError(f"commands.json requires a {group} list.")
        for index, item in enumerate(config[group]):
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip() for key in ("action", "label")
            ):
                raise ValueError(f"commands.json: {group}[{index}] requires a label and an action.")
            if "command" in item and not isinstance(item["command"], str):
                raise ValueError(f"commands.json: {group}[{index}].command must be text.")
    return config


def topic_list_command(commands):
    if not isinstance(commands, dict):
        raise ValueError("commands.json must contain an object.")
    container = commands.get("container_name")
    buttons = commands.get("terminal_buttons")
    if not isinstance(buttons, list):
        raise ValueError("Configure terminal_buttons in commands.json.")
    command = next((item.get("command") for item in buttons if isinstance(item, dict)
                    and item.get("action") == "list_topics"), None)
    if not isinstance(container, str) or not container or not isinstance(command, str) or not command:
        raise ValueError("Configure container_name and list_topics in commands.json.")
    # An interactive bash loads the same ROS setup as the container terminal.
    return shlex.join(["docker", "exec", "--", container, "bash", "-ic", command])


def topic_names(output):
    """Ignore shell startup messages, empty lines and duplicate topic names."""
    return list(dict.fromkeys(line for line in map(str.strip, output.splitlines())
                              if re.fullmatch(r"(?:/[A-Za-z_][A-Za-z_0-9]*)+", line)))
