"""Settings and read-only diagnostics shared by both desktop interfaces."""

import json
import re
import shlex
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
COMMANDS_FILE = APP_DIR / "commands.json"
CONTAINER_LIST = 'docker ps --format "{{.Names}}|{{.Status}}"'


def load_config(path=CONFIG_FILE):
    settings = dict(user="rock", ip="", dry_run=True, demo_user="demo", demo_password="demo")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key, default in settings.items():
            if isinstance(stored.get(key), type(default)):
                settings[key] = stored[key]
    return settings


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
