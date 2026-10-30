# Claude Sessions Manager

A small local web UI to browse, create and resume [Claude Code](https://claude.com/claude-code) sessions.

## Features

- Reads your `.claude` folder (default `~/.claude`, changeable in the UI)
- Syncs all conversations into SQLite on app start and exit (plus a Refresh button)
- Search sessions by title, folder or message content
- Create a session (`claude -n <name>`) and open it in a terminal
- Resume any session (`claude --resume <id>`) in its original working directory

## Requirements

- Python 3.8+ (standard library only)
- `claude` available in your `PATH`

## Usage

```bash
python3 claude_sessions.py
```

Opens `http://127.0.0.1:8765` in your browser. Use the **Exit** button (or Ctrl+C) to run a final sync and stop the server.

Options: `--port 8765`, `--no-browser`, `--sync` (sync the database and exit).

## Notes

- Database: `~/.claude-sessions-manager/sessions.db` (override with the `CSM_HOME` env var)
- Terminals: Terminal.app / iTerm on macOS, a new console on Windows, common terminal emulators on Linux
- The server only listens on `127.0.0.1`, and write actions require a per-run token
- Windows and Linux support is untested
