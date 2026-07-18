# Telegram Codex Controller

A private, single-user Telegram bot that can either send text to a named tmux
session or submit a prompt to a local Codex app-server. It is designed for a
machine you control; it is **not** a multi-user bot or a hardened public
service.

## What it does

| Telegram input | Result |
| --- | --- |
| Normal text or `/rc <prompt>` | Sends the prompt to a dedicated local Codex app-server thread. |
| `/tmux <text>` | Types text and Enter into the configured tmux session, then returns its recent output. |
| `/screen` | Returns recent output from the tmux session. |
| `/status` | Reports whether the tmux session exists. |
| `/interrupt` | Sends Ctrl-C to the tmux session. |

For direct Codex requests (normal text or `/rc`), the bot reacts to the
incoming message with 🫡, then sends a live `✏️ Changed` message when Codex
first changes each file in the turn. It uses Codex app-server patch events;
terminal-bridge (`/tmux`) edits are not tracked.

The first chat must pair using a secret pairing code. Once paired, messages
from all other chats are silently ignored.

## Requirements

- Python 3.9 or newer; this project uses only the standard library.
- `tmux` available on `PATH`.
- A Telegram bot token from [BotFather](https://t.me/BotFather).
- A local Codex CLI installation and its compatible Node runtime.
- A tmux session (default: `web`) and a workspace to give Codex.

## Install

For a user-level systemd service, clone the repository and run the installer:

```sh
git clone https://github.com/monperrus/telegram-codex-controller.git
cd telegram-codex-controller
./install.sh
```

The installer securely prompts for the bot token, creates a mode-`600` config
file, generates a pairing code, discovers local executables, and installs and
starts `telegram-codex-controller.service`. It prints the `/pair` command at
the end. To automate a non-interactive installation, provide `BOT_TOKEN` and
optionally `PAIR_CODE` as environment variables:

```sh
BOT_TOKEN='your-token' ./install.sh
```

Use `./install.sh --no-start` to install without starting, or `--force` to
replace an existing config. The installed files live in
`~/.local/share/telegram-codex-controller`; secrets remain in
`~/.config/telegram-tmux-control.env`.

The service runs while you are logged in. To keep it running after logout or
reboot, enable lingering once:

```sh
loginctl enable-linger "$USER"
```

To validate an installed setup without starting its polling loop:

```sh
~/.local/share/telegram-codex-controller/telegram-tmux-control.py --check
```

For a manually managed system service, adapt the
[system service example](systemd/telegram-tmux-controller.service.example).

## Configuration

The config file accepts `KEY=value` lines. It must contain:

- `BOT_TOKEN`: Telegram HTTP API token. Treat it like a password.
- `PAIR_CODE`: one-time pairing secret. Use a unique, high-entropy value.

These optional environment variables configure the controller:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TMUX_CONFIG` | `~/.config/telegram-tmux-control.env` | Secret config file. |
| `TELEGRAM_TMUX_STATE` | `~/.local/state/telegram-tmux-control.json` | Pairing and Telegram update offset state. |
| `TELEGRAM_TMUX_SESSION` | `web` | tmux target session. |
| `TELEGRAM_TMUX_WORKSPACE` | `~` | Working directory passed to Codex. |
| `TELEGRAM_TMUX_CODEX_BIN` | `codex` found on `PATH` | Codex CLI executable. |
| `TELEGRAM_TMUX_NODE_BIN` | `node` found on `PATH` | Node executable used to launch Codex. |
| `TELEGRAM_TMUX_TURN_TIMEOUT` | `180` | Seconds before an unresponsive Codex turn is stopped and the app-server is reset. |

## Operational notes

- The bot uses long polling; do not run two controller instances with the same
  bot token, or they may consume each other's updates.
- The sender connection is warmed at startup and kept alive, which minimizes
  the delay before the 🫡 acknowledgement. Long polling uses a separate
  connection, so it cannot block an acknowledgement or final reply.
- The state file is written with mode `0600` and records the paired chat ID.
  Delete that file to allow pairing a different chat.
- `/tmux` and the Codex API can execute actions on your host. Give access only
  to a Telegram account you fully trust, and keep the token and pairing code
  out of source control and logs.
- The controller limits outgoing Telegram text to the API's 4096-character
  limit. The tmux response is limited to recent output.

## Troubleshooting

- **Bot does not respond:** check the process logs, token, network access, and
  ensure no second polling process is using the same bot.
- **`tmux session is unavailable`:** create the configured session or set
  `TELEGRAM_TMUX_SESSION` to the actual session name.
- **Codex API request failed:** verify both executable paths, the workspace,
  and that `node codex app-server --listen stdio://` works as the service user.

## Release contents

- `telegram-tmux-control.py` — controller program.
- `install.sh` — portable user-service installer.
- `telegram-tmux-control.env.example` — safe secret-config template.
- `systemd/` — user and system service templates.

No license is included. Add an explicit license before distributing this code
outside your organization.
