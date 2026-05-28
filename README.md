# agent-tmux-hub

A tmux-based control panel for watching and approving multiple agent CLI panes.

`agent-tmux-hub` is built for a specific but common workflow problem: you have several Copilot, Claude Code, or Codex panes running in tmux, and some of them are waiting for confirmation while others are still working. This tool gives you one place to scan, filter, jump to, and approve those panes.

## Requirements

- Python 3.11+
- tmux
- Agent CLIs running inside tmux panes

The tool relies on tmux to:

- discover panes
- capture recent pane output
- jump to a target pane
- send approval keystrokes
- show the pending count in the tmux window name

## Project layout

```text
agent-tmux-hub/
├── pyproject.toml
├── README.md
└── src/
    └── agent_tmux_hub/
        ├── __init__.py
        ├── app.py
        └── cli.py
```

## Features

- Scans tmux panes for known agent CLIs
- Currently recognizes:
  - GitHub Copilot CLI
  - Claude Code
  - Codex
- Detects common waiting-for-confirmation states
- Classifies panes as `waiting`, `running`, or `idle`
- Shows a detail panel with recent output and context
- Extracts lightweight decision metadata:
  - category
  - risk
  - target
- Supports approving one item, all waiting items, or low-risk waiting items
- Updates the tmux window name with the current waiting count

## Usage

Open or focus the dedicated hub window:

```bash
agent-tmux-hub --window
```

Run directly in the current tmux pane:

```bash
agent-tmux-hub --run
```

For local development:

```bash
python src/agent_tmux_hub/cli.py
```

## Key bindings

| Key | Action |
| --- | --- |
| `j` / `k` | Move selection down / up |
| `h` | Toggle help overlay |
| `tab` | Switch filter: `all`, `waiting`, `low-risk` |
| `a` | Approve the selected item with the detected action |
| `A` | Approve all waiting items |
| `l` | Approve all low-risk waiting items |
| `e` | Send `Enter` |
| `y` | Send `y` + `Enter` |
| `d` | Send `n` + `Enter` |
| `o` | Jump to the selected pane |
| `r` | Refresh immediately |
| `q` | Quit |

## Detail panel

The detail panel is designed to make approvals safer. Before you approve a pane, it shows:

- provider
- pane location
- current command
- suggested action
- category / risk / target summary
- recent real output from the pane

The goal is simple: before approving, you should know what you are approving.

## Notifications

When new waiting items appear, the tool:

- shows a tmux message
- rings the tmux bell
- appends the waiting count to the hub window name, for example:

```text
agent-tmux-hub[2]
```

## Recognized confirmation patterns

The current version recognizes common prompts such as:

- `1. Yes / 2. No` menu confirmations
- `Press Enter`
- `Allow ...`
- `Continue?`
- `y/N`

If the tool is not confident, it does not invent an action. You can still jump to the pane and handle it manually.

## Safety model

This is an operator-assist tool, not a full autopilot. It helps centralize attention and reduce context switching, but it still expects the user to review the detail panel before approving meaningful actions.
