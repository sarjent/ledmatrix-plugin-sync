# Plugin Sync for LEDMatrix

Keeps plugins and configuration in sync across multiple LEDMatrix Pi's over HTTP. The source Pi runs a lightweight sync server; destination Pi's pull from it on a configurable schedule.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-sarjent-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/sarjent)

## Features

- **HTTP-based sync** — no SSH keys, no rsync, no extra tools required
- **Pull-based** — each destination Pi manages its own sync independently; add new destinations without touching the source
- **Lightweight built-in server** — the plugin starts a small HTTP server on the source Pi, no separate service needed
- **Run Now button** — trigger an immediate sync from the plugin's settings page in the web UI
- **Auto-restart** — automatically restarts the display service when changes are detected
- **Safe config merge** — only plugin config sections are synced; hardware settings, schedule, timezone, location, and other Pi-specific keys are always preserved locally
- **Optional token auth** — shared secret header for basic security on trusted local networks
- **Zero display time** — runs entirely in the background, never interrupts your display rotation

## How It Works

```
ledpi-test (source)              ledpi-ticker (destination)
┌─────────────────────┐          ┌──────────────────────────┐
│ plugin-sync         │          │ plugin-sync              │
│ server_mode: true   │◄─ HTTP ──│ server_mode: false       │
│ port 5001           │          │ source_host: ledpi-test  │
└─────────────────────┘          └──────────────────────────┘
```

## Installation

Install on **both** the source Pi and each destination Pi from the LEDMatrix Plugin Manager using the GitHub URL:

```text
https://github.com/sarjent/ledmatrix-plugin-sync
```

## Setup

**On the source Pi (`ledpi-test`):**
- Set `server_mode: true`
- Set `server_port: 5001` (or any available port)
- Optionally set `sync_token` to a shared secret string

**On each destination Pi (`ledpi-ticker`, etc.):**
- Leave `server_mode: false` (default)
- Set `source_host` to the source Pi's hostname or IP
- Set `source_port` to match the source
- Set `sync_token` to the same value as the source (if used)
- Click **Run Sync Now** to test

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | boolean | `true` | Enable or disable the plugin |
| `server_mode` | boolean | `false` | Run as sync server (set to `true` on source Pi only) |
| `server_port` | integer | `5001` | Port the sync server listens on or connects to |
| `sync_token` | string | `""` | Shared secret for basic auth — must match on source and all destinations |
| `source_host` | string | `""` | Hostname or IP of the source Pi (destination mode only) |
| `source_port` | integer | `5001` | Port of the sync server on the source Pi |
| `sync_plugins` | boolean | `true` | Sync the `plugins/` directory from source |
| `sync_config` | boolean | `true` | Sync plugin configuration sections from source `config.json` |
| `sync_secrets` | boolean | `false` | Sync `config_secrets.json` (API keys) from source |
| `sync_frequency_hours` | number | `24` | Hours between scheduled syncs — options: 1, 6, 12, 24, 48, 168 |
| `dry_run` | boolean | `false` | Log what would be synced without making changes |
| `auto_restart` | boolean | `true` | Restart the display service automatically when changes are detected |
| `display_service_name` | string | `"ledmatrix.service"` | Systemd service to restart when changes are detected |
| `preserve_local_keys` | array | `[]` | Additional `config.json` keys to always keep from local and never overwrite |

### Always preserved locally

The following `config.json` keys are **never** overwritten from source, regardless of settings:

`display` · `schedule` · `dim_schedule` · `timezone` · `location` · `web_display_autostart` · `plugin_system` · `plugin-sync`

## Firewall

If UFW is enabled on the source Pi (common on Raspberry Pi OS), port 5001 must be opened:

```bash
sudo ufw allow 5001/tcp comment "LEDMatrix plugin sync"
```

## Requirements

- LEDMatrix v2.0.0 or higher
- Python 3.9+
- Both Pi's on the same local network
- Port 5001 (or your configured port) open between Pi's
- Passwordless `sudo` for `systemctl restart` (default on Raspberry Pi OS)

## License

MIT License

## Support

If this plugin is useful to you, consider buying me a coffee!

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-sarjent-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/sarjent)

## Contributing

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/sarjent/ledmatrix-plugin-sync).
