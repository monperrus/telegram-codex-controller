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

The first chat must pair using a secret pairing code. Once paired, messages
from all other chats are silently ignored.

## Requirements

- Python 3.9 or newer; this project uses only the standard library.
- `tmux` available on `PATH`.
- A Telegram bot token from [BotFather](https://t.me/BotFather).
- A local Codex CLI installation and its compatible Node runtime.
- A tmux session (default: `web`) and a workspace to give Codex.

## Install

1. Copy this release directory to a private location on the host.
2. Create a config file with restricted permissions:

   ```sh
   mkdir -p ~/.config ~/.local/state
   cp telegram-tmux-control.env.example ~/.config/telegram-tmux-control.env
   chmod 600 ~/.config/telegram-tmux-control.env
   ```

3. Set `BOT_TOKEN` and a long, random `PAIR_CODE` in that config file.
4. Set the paths and names for your host using environment variables. The
   defaults are intentionally shown in the service example; change them if
   your install lives elsewhere.
5. Run it manually first:

   ```sh
   TELEGRAM_TMUX_CONFIG="$HOME/.config/telegram-tmux-control.env" \
   TELEGRAM_TMUX_STATE="$HOME/.local/state/telegram-tmux-control.json" \
   TELEGRAM_TMUX_SESSION=web \
   TELEGRAM_TMUX_WORKSPACE="/path/to/workspace" \
   TELEGRAM_TMUX_CODEX_BIN="/path/to/codex" \
   TELEGRAM_TMUX_NODE_BIN="/path/to/node" \
   ./telegram-tmux-control.py
   ```

6. In Telegram, send `/pair <your pairing code>` from the one chat that should
   control the bot. Use `/help` to confirm it is working.

For a persistent service, see the [systemd service example](systemd/telegram-tmux-controller.service.example).

## Configuration

The config file accepts `KEY=value` lines. It must contain:

- `BOT_TOKEN`: Telegram HTTP API token. Treat it like a password.
- `PAIR_CODE`: one-time pairing secret. Use a unique, high-entropy value.

These optional environment variables configure the controller:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TMUX_CONFIG` | `/home/remote-tmux/.config/telegram-tmux-control.env` | Secret config file. |
| `TELEGRAM_TMUX_STATE` | `/home/remote-tmux/.local/state/telegram-tmux-control.json` | Pairing and Telegram update offset state. |
| `TELEGRAM_TMUX_SESSION` | `web` | tmux target session. |
| `TELEGRAM_TMUX_WORKSPACE` | `/home/remote-tmux` | Working directory passed to Codex. |
| `TELEGRAM_TMUX_CODEX_BIN` | `/home/remote-tmux/.local/bin/codex` | Codex CLI executable. |
| `TELEGRAM_TMUX_NODE_BIN` | `/home/remote-tmux/.local/node-v22.23.1/bin/node` | Node executable used to launch Codex. |

## Operational notes

- The bot uses long polling; do not run two controller instances with the same
  bot token, or they may consume each other's updates.
- The sender connection is warmed at startup and kept alive, which minimizes
  the delay before the `🫡` acknowledgement. Long polling uses a separate
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
- `telegram-tmux-control.env.example` — safe secret-config template.
- `systemd/` — optional service template.
- `RELEASE-NOTES.md` — release scope and known constraints.

No license is included. Add an explicit license before distributing this code
outside your organization.
