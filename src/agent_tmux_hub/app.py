#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass


DISCOVERY_FORMAT = (
    "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}\t"
    "#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t"
    "#{pane_title}\t#{pane_active}\t#{window_active}"
)
CAPTURE_LINES = 120
DEFAULT_WINDOW_NAME = "agent-tmux-hub"
DETAIL_LINES = 12
FILTER_MODES = ("all", "waiting", "low-risk")

COLOR_HEADER = 1
COLOR_WAITING = 2
COLOR_RUNNING = 3
COLOR_IDLE = 4
COLOR_DETAIL = 5
COLOR_COPILOT = 6
COLOR_CLAUDE = 7
COLOR_CODEX = 8


WAIT_RULES = [
    (
        re.compile(r"(^|[^a-z])(y/N|Y/n|\[y/N\]|\[Y/n\]|yes/no)([^a-z]|$)", re.IGNORECASE),
        "yes",
        ["y", "Enter"],
    ),
    (
        re.compile(r"do you want to run this command\?", re.IGNORECASE),
        "enter",
        ["Enter"],
    ),
    (re.compile(r"press enter", re.IGNORECASE), "enter", ["Enter"]),
    (re.compile(r"allow( tool| directory| url)?", re.IGNORECASE), "enter", ["Enter"]),
    (re.compile(r"continue\?", re.IGNORECASE), "enter", ["Enter"]),
    (re.compile(r"do you want to allow this\?", re.IGNORECASE), "enter", ["Enter"]),
    (re.compile(r"enter to confirm", re.IGNORECASE), "enter", ["Enter"]),
]

MENU_OPTION_RE = re.compile(r"^(?P<marker>[❯>])?\s*(?P<number>\d+)\.\s+")
MENU_HINT_RE = re.compile(r"(enter to select|↑/↓ to select|up/down to select)", re.IGNORECASE)

PROVIDER_SIGNATURES = {
    "copilot": {
        "commands": {"copilot"},
        "window_names": {"copilot"},
        "title_terms": ("github copilot", "copilot"),
    },
    "claude": {
        "commands": {"claude"},
        "window_names": {"claude", "claude-code"},
        "title_terms": ("claude code", "claude"),
    },
    "codex": {
        "commands": {"codex"},
        "window_names": {"codex"},
        "title_terms": ("openai codex", "codex"),
    },
}


@dataclass
class PaneRecord:
    pane_id: str
    session_name: str
    window_index: str
    window_name: str
    pane_index: str
    current_command: str
    current_path: str
    pane_title: str
    provider: str
    pane_active: bool
    window_active: bool
    status: str = "idle"
    excerpt: str = ""
    category: str = "-"
    risk: str = "-"
    target: str = ""
    action_hint: str = "-"
    action_keys: list[str] | None = None
    fingerprint: str = ""
    detail_lines: list[str] | None = None

    @property
    def location(self) -> str:
        return f"{self.session_name}:{self.window_index}.{self.pane_index}"


def tmux(*args: str) -> str:
    try:
        result = subprocess.run(
            ["tmux", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("tmux not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise SystemExit(stderr or f"tmux command failed: {' '.join(args)}") from exc
    return result.stdout


def shorten(value: str, width: int) -> str:
    clean = " ".join(value.split())
    if width <= 0:
        return ""
    if len(clean) <= width:
        return clean
    if width <= 1:
        return clean[:width]
    return clean[: width - 1] + "…"


def safe_addnstr(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    width: int,
    attr: int = curses.A_NORMAL,
) -> None:
    if width <= 0 or y < 0 or x < 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_WAITING, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_RUNNING, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_IDLE, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_DETAIL, curses.COLOR_MAGENTA, -1)
    curses.init_pair(COLOR_COPILOT, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_CLAUDE, curses.COLOR_MAGENTA, -1)
    curses.init_pair(COLOR_CODEX, curses.COLOR_GREEN, -1)


def color_attr(pair_id: int) -> int:
    if not curses.has_colors():
        return curses.A_NORMAL
    return curses.color_pair(pair_id)


def provider_attr(provider: str) -> int:
    mapping = {
        "copilot": COLOR_COPILOT,
        "claude": COLOR_CLAUDE,
        "codex": COLOR_CODEX,
    }
    return color_attr(mapping.get(provider, COLOR_HEADER))


def extract_target(lines: list[str]) -> str:
    recent_lines = [line.strip() for line in lines if line.strip()][-20:]
    path_pattern = re.compile(r"(/Users/[^\s]+|~/[^\s]+)")
    command_pattern = re.compile(r"\b(cd|python3?|node|npm|pnpm|yarn|uv|go|cargo|git|bash|sh)\b")
    for line in recent_lines:
        path_match = path_pattern.search(line)
        if path_match:
            return path_match.group(1)
    for line in recent_lines:
        cleaned = re.sub(r"^[│❯•\-\s]+", "", line).strip()
        if command_pattern.search(cleaned):
            return shorten(cleaned, 90)
    return ""


def classify_prompt(lines: list[str]) -> tuple[str, str]:
    joined = "\n".join(line.lower() for line in lines if line.strip())
    if "allow directory access" in joined or "allowed directory list" in joined:
        return "path-access", "medium"
    if "do you want to run this command" in joined:
        if re.search(r"\b(rm\s+-rf|sudo|git push|git reset --hard|kubectl delete|terraform destroy)\b", joined):
            return "command-run", "high"
        return "command-run", "medium"
    if "allow tool" in joined:
        return "tool-access", "low"
    if "allow url" in joined:
        return "url-access", "medium"
    if re.search(r"(^|[^a-z])(y/n|yes/no)([^a-z]|$)", joined):
        return "yes-no", "low"
    if "press enter" in joined or "enter to confirm" in joined:
        return "continue", "low"
    return "review", "low"


def parse_numbered_menu(lines: list[str]) -> tuple[list[int], int | None] | None:
    if not any(MENU_HINT_RE.search(line) for line in lines):
        return None

    options: list[int] = []
    selected: int | None = None
    for line in lines:
        cleaned = re.sub(r"^[│\s]+", "", line).rstrip()
        match = MENU_OPTION_RE.match(cleaned)
        if not match:
            continue
        number = int(match.group("number"))
        options.append(number)
        if match.group("marker"):
            selected = number

    deduped_options = sorted(set(options))
    if len(deduped_options) < 2:
        return None
    return deduped_options, selected


def action_keys_for_menu_choice(lines: list[str], choice: int) -> list[str] | None:
    parsed = parse_numbered_menu(lines)
    if not parsed:
        return None

    options, selected = parsed
    if choice not in options:
        return None

    current = selected if selected in options else options[0]
    current_index = options.index(current)
    target_index = options.index(choice)
    delta = target_index - current_index
    keys: list[str] = []
    if delta > 0:
        keys.extend(["Down"] * delta)
    elif delta < 0:
        keys.extend(["Up"] * (-delta))
    keys.append("Enter")
    return keys


def format_action_hint(action_keys: list[str] | None, lines: list[str]) -> str:
    parsed = parse_numbered_menu(lines)
    if parsed:
        options, _ = parsed
        if options:
            return f"1-{options[-1]}/Ent"
    return " ".join(action_keys or ["-"])


def detect_action(lines: list[str]) -> tuple[str, str, list[str] | None, str, str, str]:
    recent_lines = [line for line in lines if line.strip()][-14:]
    parsed_menu = parse_numbered_menu(recent_lines)
    if parsed_menu:
        options, selected = parsed_menu
        category, risk = classify_prompt(recent_lines)
        selected_text = f" selected={selected}" if selected is not None else ""
        return "waiting", f"menu choice: {options[0]}-{options[-1]}{selected_text}", ["Enter"], category, risk, extract_target(recent_lines)
    for line in reversed(recent_lines):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, hint, keys in WAIT_RULES:
            if pattern.search(stripped):
                category, risk = classify_prompt(recent_lines)
                return "waiting", stripped, list(keys), category, risk, extract_target(recent_lines)
    for line in reversed(recent_lines):
        stripped = line.strip()
        if stripped:
            return "idle", stripped, None, "-", "-", ""
    return "idle", "", None, "-", "-", ""


def select_detail_lines(lines: list[str], limit: int = DETAIL_LINES) -> list[str]:
    detail = [line.rstrip() for line in lines if line.strip()]
    if not detail:
        return []
    return detail[-limit:]


def detect_provider(
    current_command: str,
    window_name: str,
    pane_title: str,
    captured_output: str,
) -> str | None:
    command = current_command.lower()
    window = window_name.lower()
    title = pane_title.lower()
    if window.startswith(DEFAULT_WINDOW_NAME) or "agent-tmux-hub" in title:
        return None

    for provider, signature in PROVIDER_SIGNATURES.items():
        if command in signature["commands"]:
            return provider
        if window in signature["window_names"]:
            return provider
        if any(term in title for term in signature["title_terms"]):
            return provider
    return None


def list_agent_panes(previous_fingerprints: dict[str, str]) -> list[PaneRecord]:
    raw = tmux("list-panes", "-a", "-F", DISCOVERY_FORMAT)
    current_hub_pane = os.environ.get("TMUX_PANE", "")
    panes: list[PaneRecord] = []

    for raw_line in raw.splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 10:
            continue
        pane_id = parts[0]
        if pane_id == current_hub_pane:
            continue

        captured = tmux("capture-pane", "-p", "-S", f"-{CAPTURE_LINES}", "-t", pane_id)
        provider = detect_provider(parts[5], parts[3], parts[7], captured)
        if not provider:
            continue

        lines = captured.splitlines()
        status, excerpt, action_keys, category, risk, target = detect_action(lines)
        fingerprint = hashlib.sha1(captured.encode("utf-8")).hexdigest()
        if status != "waiting":
            previous = previous_fingerprints.get(pane_id)
            status = "running" if previous is None or previous != fingerprint else "idle"

        panes.append(
            PaneRecord(
                pane_id=pane_id,
                session_name=parts[1],
                window_index=parts[2],
                window_name=parts[3],
                pane_index=parts[4],
                current_command=parts[5],
                current_path=parts[6],
                pane_title=parts[7],
                provider=provider,
                pane_active=parts[8] == "1",
                window_active=parts[9] == "1",
                status=status,
                excerpt=excerpt,
                category=category,
                risk=risk,
                target=target,
                action_hint=format_action_hint(action_keys, lines),
                action_keys=action_keys,
                fingerprint=fingerprint,
                detail_lines=select_detail_lines(lines),
            )
        )

    panes.sort(key=lambda pane: (status_rank(pane.status), pane.session_name, int(pane.window_index), int(pane.pane_index)))
    return panes


def status_rank(status: str) -> int:
    order = {"waiting": 0, "running": 1, "idle": 2}
    return order.get(status, 9)


def filter_label(filter_mode: str) -> str:
    labels = {
        "all": "all",
        "waiting": "waiting",
        "low-risk": "low-risk",
    }
    return labels.get(filter_mode, filter_mode)


def visible_panes_for_mode(panes: list[PaneRecord], filter_mode: str) -> list[PaneRecord]:
    if filter_mode == "waiting":
        return [pane for pane in panes if pane.status == "waiting"]
    if filter_mode == "low-risk":
        return [pane for pane in panes if pane.status == "waiting" and pane.risk == "low"]
    return panes


def send_keys(pane_id: str, keys: list[str]) -> None:
    tmux("send-keys", "-t", pane_id, *keys)


def jump_to_pane(pane: PaneRecord) -> None:
    tmux("select-window", "-t", f"{pane.session_name}:{pane.window_index}")
    tmux("select-pane", "-t", pane.pane_id)


def smart_approve_all(panes: list[PaneRecord]) -> int:
    approved = 0
    for pane in panes:
        if pane.status != "waiting" or not pane.action_keys:
            continue
        send_keys(pane.pane_id, pane.action_keys)
        approved += 1
    return approved


def smart_approve_low_risk(panes: list[PaneRecord]) -> int:
    approved = 0
    for pane in panes:
        if pane.status != "waiting" or pane.risk != "low" or not pane.action_keys:
            continue
        send_keys(pane.pane_id, pane.action_keys)
        approved += 1
    return approved


def notify_new_waiting(new_waiting: list[PaneRecord]) -> None:
    if not new_waiting:
        return
    lead = new_waiting[0]
    message = f"agent-tmux-hub: {len(new_waiting)} waiting · {lead.location} · {shorten(lead.excerpt, 80)}"
    try:
        tmux("display-message", message)
        tmux("bell")
    except SystemExit:
        pass


def is_real_hub_window(window_id: str) -> bool:
    try:
        raw = tmux("list-panes", "-t", window_id, "-F", "#{pane_current_command}\t#{pane_current_path}")
    except SystemExit:
        return False
    hub_path = os.path.expanduser("~/code/agent-tmux-hub")
    for line in raw.splitlines():
        command, current_path = line.split("\t", 1)
        if command.startswith("python") and current_path == hub_path:
            return True
    return False


def find_window_ids(name: str) -> list[str]:
    raw = tmux("list-windows", "-a", "-F", "#{window_id}\t#{window_name}")
    ids: list[str] = []
    for line in raw.splitlines():
        window_id, window_name = line.split("\t", 1)
        if (window_name == name or window_name.startswith(f"{name}[")) and is_real_hub_window(window_id):
            ids.append(window_id)
    return ids


def find_window_id(name: str) -> str | None:
    ids = find_window_ids(name)
    return ids[0] if ids else None


def cleanup_duplicate_hub_windows(name: str) -> None:
    ids = find_window_ids(name)
    for window_id in ids[1:]:
        try:
            tmux("kill-window", "-t", window_id)
        except SystemExit:
            pass


def update_window_badge(waiting_count: int) -> None:
    current_pane = os.environ.get("TMUX_PANE", "")
    window_id = ""
    if current_pane:
        try:
            window_id = tmux("display-message", "-p", "-t", current_pane, "#{window_id}").strip()
        except SystemExit:
            window_id = ""
    if not window_id:
        window_id = find_window_id(DEFAULT_WINDOW_NAME) or ""
    if not window_id:
        return
    new_name = DEFAULT_WINDOW_NAME if waiting_count <= 0 else f"{DEFAULT_WINDOW_NAME}[{waiting_count}]"
    try:
        tmux("rename-window", "-t", window_id, new_name)
    except SystemExit:
        pass


def spawn_or_focus_window(window_name: str) -> int:
    if not os.environ.get("TMUX"):
        print("agent-tmux-hub --window must run inside tmux", file=sys.stderr)
        return 1

    cleanup_duplicate_hub_windows(window_name)
    existing = find_window_id(window_name)
    if existing:
        tmux("select-window", "-t", existing)
        return 0

    launcher = os.path.expanduser("~/.local/bin/agent-tmux-hub")
    command = f"{shlex.quote(launcher)} --run"
    tmux("new-window", "-n", window_name, command)
    return 0


def draw_help(stdscr: curses.window, max_y: int, width: int) -> None:
    if max_y <= 0:
        return
    help_text = "j/k move  1-9 choose  h help  tab filter  a approve  A approve-all  l approve-low  e/y/d act  o jump  r refresh  q quit"
    safe_addnstr(stdscr, max_y - 1, 0, help_text.ljust(width), width, curses.A_REVERSE)


def draw_help_overlay(stdscr: curses.window, max_y: int, max_x: int) -> None:
    lines = [
        "agent-tmux-hub help",
        "",
        "j / k      move",
        "tab        switch filter (all / waiting / low-risk)",
        "a          approve current item",
        "A          approve all waiting items",
        "l          approve all low-risk waiting items",
        "1-9        choose numbered menu item",
        "e / y / d  send Enter / y+Enter / n+Enter",
        "o          jump to selected pane",
        "r          refresh",
        "h          toggle this help",
        "q          quit",
    ]
    box_width = min(max_x - 4, 64)
    box_height = min(max_y - 4, len(lines) + 2)
    if box_width <= 10 or box_height <= 4:
        return
    start_y = (max_y - box_height) // 2
    start_x = (max_x - box_width) // 2

    for y in range(start_y, start_y + box_height):
        safe_addnstr(stdscr, y, start_x, " " * box_width, box_width, curses.A_REVERSE)
    for index, line in enumerate(lines[: box_height - 2]):
        attr = curses.A_BOLD | color_attr(COLOR_HEADER) if index == 0 else curses.A_REVERSE
        safe_addnstr(stdscr, start_y + 1 + index, start_x + 2, shorten(line, box_width - 4), box_width - 4, attr)


def draw_header(stdscr: curses.window, panes: list[PaneRecord], visible_panes: list[PaneRecord], filter_mode: str, width: int) -> None:
    if width <= 0:
        return
    waiting = sum(1 for pane in panes if pane.status == "waiting")
    low_risk = sum(1 for pane in panes if pane.status == "waiting" and pane.risk == "low")
    running = sum(1 for pane in panes if pane.status == "running")
    idle = sum(1 for pane in panes if pane.status == "idle")
    providers = ",".join(sorted({pane.provider for pane in panes})) or "-"
    title = (
        f"agent-tmux-hub  filter={filter_label(filter_mode)}  shown={len(visible_panes)}  "
        f"actionable={waiting} low-risk={low_risk} running={running} idle={idle}  providers={providers}"
    )
    safe_addnstr(stdscr, 0, 0, title.ljust(width), width, curses.A_BOLD | color_attr(COLOR_HEADER))
    columns = "  status   provider pane        cwd                  action    prompt"
    safe_addnstr(stdscr, 1, 0, columns.ljust(width), width, curses.A_UNDERLINE | color_attr(COLOR_HEADER))


def draw_rows(stdscr: curses.window, panes: list[PaneRecord], selected: int, start_y: int, end_y: int, width: int) -> None:
    visible_rows = end_y - start_y + 1
    if visible_rows <= 0:
        return

    if not panes:
        safe_addnstr(stdscr, start_y, 0, "No agent panes found right now.".ljust(width), width, curses.A_DIM)
        return

    start = 0
    if selected >= visible_rows:
        start = selected - visible_rows + 1

    for line_no in range(visible_rows):
        y = line_no + start_y
        pane_index = start + line_no
        if pane_index >= len(panes):
            safe_addnstr(stdscr, y, 0, " ".ljust(width), width)
            continue

        if width <= 0:
            continue
        pane = panes[pane_index]
        status = pane.status.ljust(8)
        provider = pane.provider.ljust(8)
        location = shorten(pane.location, 11).ljust(11)
        cwd = shorten(pane.current_path.replace(os.path.expanduser("~"), "~"), 20).ljust(20)
        action = shorten(pane.action_hint, 9).ljust(9)
        prompt = shorten(pane.excerpt, max(0, width - 61))
        selected_attr = curses.A_BOLD if pane_index == selected else curses.A_NORMAL
        row_attr = curses.A_DIM if pane.status == "idle" and pane_index != selected else curses.A_NORMAL
        marker = "!"
        marker_attr = curses.A_BOLD | color_attr(COLOR_WAITING)
        if pane.status != "waiting":
            marker = "›" if pane_index == selected else " "
            marker_attr = color_attr(COLOR_HEADER) | selected_attr
        safe_addnstr(stdscr, y, 0, marker.ljust(2), 2, marker_attr)
        if pane.status == "waiting":
            status_attr = curses.A_BOLD | color_attr(COLOR_WAITING)
        elif pane.status == "running":
            status_attr = curses.A_BOLD | color_attr(COLOR_RUNNING)
        else:
            status_attr = row_attr | color_attr(COLOR_IDLE)

        safe_addnstr(stdscr, y, 2, status, 8, status_attr | selected_attr)
        safe_addnstr(stdscr, y, 10, " ", 1)
        safe_addnstr(stdscr, y, 11, provider, 8, provider_attr(pane.provider) | selected_attr)
        safe_addnstr(stdscr, y, 19, " ", 1)
        safe_addnstr(stdscr, y, 20, location, 11, row_attr | selected_attr)
        safe_addnstr(stdscr, y, 31, " ", 1)
        safe_addnstr(stdscr, y, 32, cwd, 20, row_attr | selected_attr)
        safe_addnstr(stdscr, y, 52, " ", 1)
        safe_addnstr(stdscr, y, 53, action, 9, row_attr | selected_attr)
        safe_addnstr(stdscr, y, 62, " ", 1)
        safe_addnstr(stdscr, y, 63, prompt.ljust(max(0, width - 63)), max(0, width - 63), row_attr | selected_attr)


def draw_detail(stdscr: curses.window, panes: list[PaneRecord], selected: int, start_y: int, end_y: int, width: int) -> None:
    if start_y > end_y or width <= 0:
        return
    if not panes:
        return

    pane = panes[selected]
    meta = (
        f"detail  provider={pane.provider}  pane={pane.location}  "
        f"cmd={pane.current_command}  action={pane.action_hint}"
    )
    safe_addnstr(stdscr, start_y, 0, meta.ljust(width), width, curses.A_UNDERLINE | color_attr(COLOR_DETAIL))
    summary = f"summary category={pane.category}  risk={pane.risk}"
    if pane.target:
        summary += f"  target={pane.target}"
    safe_addnstr(stdscr, start_y + 1, 0, shorten(summary, width).ljust(width), width, curses.A_BOLD | color_attr(COLOR_DETAIL))
    path_line = f"path    {pane.current_path}"
    safe_addnstr(stdscr, start_y + 2, 0, shorten(path_line, width).ljust(width), width, curses.A_DIM | color_attr(COLOR_DETAIL))

    body_start = start_y + 3
    available = end_y - body_start + 1
    if available <= 0:
        return

    detail_lines = pane.detail_lines or ["(no recent output)"]
    detail_lines = detail_lines[-available:]
    blank_rows = available - len(detail_lines)
    for index in range(blank_rows):
        safe_addnstr(stdscr, body_start + index, 0, " ".ljust(width), width)
    for index, line in enumerate(detail_lines):
        safe_addnstr(stdscr, body_start + blank_rows + index, 0, shorten(line, width).ljust(width), width)


def run_tui(refresh_interval: float) -> int:
    previous_fingerprints: dict[str, str] = {}
    panes = list_agent_panes(previous_fingerprints)
    for pane in panes:
        previous_fingerprints[pane.pane_id] = pane.fingerprint
    initial_waiting = [pane for pane in panes if pane.status == "waiting"]
    if initial_waiting:
        notify_new_waiting(initial_waiting)
    update_window_badge(len(initial_waiting))
    alerted_waiting: set[str] = {pane.pane_id for pane in initial_waiting}
    previous_waiting_count = len(initial_waiting)

    def inner(stdscr: curses.window) -> int:
        nonlocal previous_waiting_count

        init_colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.nodelay(True)
        stdscr.keypad(True)
        selected = 0
        filter_index = 0
        show_help_overlay = False
        last_refresh = 0.0

        while True:
            now = time.monotonic()
            if now - last_refresh >= refresh_interval:
                fresh = list_agent_panes(previous_fingerprints)
                previous_fingerprints.clear()
                for pane in fresh:
                    previous_fingerprints[pane.pane_id] = pane.fingerprint
                waiting_now = {pane.pane_id for pane in fresh if pane.status == "waiting"}
                new_waiting = [pane for pane in fresh if pane.status == "waiting" and pane.pane_id not in alerted_waiting]
                if new_waiting:
                    notify_new_waiting(new_waiting)
                elif waiting_now and len(waiting_now) > previous_waiting_count:
                    notify_new_waiting([pane for pane in fresh if pane.status == "waiting"][:1])
                alerted_waiting.intersection_update(waiting_now)
                alerted_waiting.update(waiting_now)
                previous_waiting_count = len(waiting_now)
                update_window_badge(previous_waiting_count)
                panes[:] = fresh
                current_visible = visible_panes_for_mode(panes, FILTER_MODES[filter_index])
                if current_visible:
                    selected = max(0, min(selected, len(current_visible) - 1))
                else:
                    selected = 0
                last_refresh = now

            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            current_filter = FILTER_MODES[filter_index]
            current_visible = visible_panes_for_mode(panes, current_filter)
            draw_header(stdscr, panes, current_visible, current_filter, max_x)
            detail_height = min(DETAIL_LINES + 2, max(0, max_y - 6))
            detail_start = max_y - detail_height - 1
            rows_end = max(2, detail_start - 1)
            draw_rows(stdscr, current_visible, selected, 2, rows_end, max_x)
            if detail_start >= 3:
                draw_detail(stdscr, current_visible, selected, detail_start, max_y - 2, max_x)
            if show_help_overlay:
                draw_help_overlay(stdscr, max_y, max_x)
            draw_help(stdscr, max_y, max_x)
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                time.sleep(0.05)
                continue
            if key in (ord("q"), 27):
                return 0
            if key == ord("h"):
                show_help_overlay = not show_help_overlay
                continue
            if key in (ord("j"), curses.KEY_DOWN):
                if current_visible:
                    selected = min(selected + 1, len(current_visible) - 1)
                continue
            if key in (ord("k"), curses.KEY_UP):
                if current_visible:
                    selected = max(selected - 1, 0)
                continue
            if key == 9:
                filter_index = (filter_index + 1) % len(FILTER_MODES)
                current_visible = visible_panes_for_mode(panes, FILTER_MODES[filter_index])
                selected = 0 if not current_visible else max(0, min(selected, len(current_visible) - 1))
                continue
            if key == ord("r"):
                last_refresh = 0.0
                continue
            if not current_visible:
                continue

            target = current_visible[selected]
            if key == ord("a") and target.action_keys:
                send_keys(target.pane_id, target.action_keys)
                last_refresh = 0.0
            elif ord("1") <= key <= ord("9"):
                choice = key - ord("0")
                menu_keys = action_keys_for_menu_choice(
                    tmux("capture-pane", "-p", "-S", f"-{CAPTURE_LINES}", "-t", target.pane_id).splitlines(),
                    choice,
                )
                if menu_keys:
                    send_keys(target.pane_id, menu_keys)
                    last_refresh = 0.0
            elif key == ord("e"):
                send_keys(target.pane_id, ["Enter"])
                last_refresh = 0.0
            elif key == ord("y"):
                send_keys(target.pane_id, ["y", "Enter"])
                last_refresh = 0.0
            elif key == ord("d"):
                send_keys(target.pane_id, ["n", "Enter"])
                last_refresh = 0.0
            elif key == ord("o"):
                jump_to_pane(target)
            elif key == ord("A"):
                if smart_approve_all(panes):
                    last_refresh = 0.0
            elif key == ord("l"):
                if smart_approve_low_risk(panes):
                    last_refresh = 0.0

    return curses.wrapper(inner)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Central approval hub for agent CLIs running in tmux.")
    parser.add_argument("--window", action="store_true", help="open or focus the dedicated hub tmux window")
    parser.add_argument("--run", action="store_true", help="run the TUI in the current pane")
    parser.add_argument("--window-name", default=DEFAULT_WINDOW_NAME, help="name for the dedicated tmux window")
    parser.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.window:
        return spawn_or_focus_window(args.window_name)
    if not os.environ.get("TMUX"):
        print("agent-tmux-hub must run inside tmux", file=sys.stderr)
        return 1
    return run_tui(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
