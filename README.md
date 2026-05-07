# Plugin Sync for LEDMatrix

Silently keeps plugins and plugin configuration in sync across multiple LEDMatrix Pi's by pulling from a designated source Pi on a configurable schedule.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-sarjent-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/sarjent)

## Features

- **Pull-based sync** — each destination Pi pulls from the source independently; no changes needed on the source when adding new destinations
- **Shared SSH key** — one key pair, one entry in the source's `authorized_keys`
- **Safe config merge** — only plugin config sections are synced; hardware settings, schedule, timezone, location, and other Pi-specific keys are always preserved locally
- **Availability check** — if the source is unreachable, the sync is skipped cleanly with no errors
- **Dry run mode** — log exactly what would change without touching anything
- **Zero display time** — runs entirely in the background, never interrupts your display rotation

## Installation

Install directly from the LEDMatrix web UI via the plugin store, or using the GitHub URL:

```text
https://github.com/sarjent/ledmatrix-plugin-sync
```

## One-Time Setup

After installing on each destination Pi:

1. Enable the plugin and set `source_host`, `source_user`, and `source_ledmatrix_path` in the web UI
2. On the next update cycle the plugin generates an SSH key and logs the public key
3. Copy that public key into `~/.ssh/authorized_keys` on the source Pi — this only needs to be done once and all destinations share the same key

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | boolean | `true` | Enable or disable the plugin |
| `source_host` | string | `""` | Hostname or IP of the source LEDMatrix Pi |
| `source_user` | string | `"pi"` | SSH username on the source Pi |
| `source_ledmatrix_path` | string | `"/home/pi/LEDMatrix"` | Absolute path to LEDMatrix on the source Pi |
| `ssh_key_path` | string | `"~/.ssh/ledmatrix_sync_rsa"` | Path to the shared SSH private key |
| `sync_plugins` | boolean | `true` | Sync the `plugins/` directory from source |
| `sync_config` | boolean | `true` | Sync plugin configuration sections from source `config.json` |
| `sync_secrets` | boolean | `false` | Sync `config_secrets.json` (API keys) from source |
| `sync_frequency_hours` | number | `24` | Hours between syncs — options: 1, 6, 12, 24, 48, 168 |
| `dry_run` | boolean | `false` | Log what would be synced without making changes |
| `preserve_local_keys` | array | `[]` | Additional `config.json` keys to always keep from local and never overwrite |

### Always preserved locally

The following `config.json` keys are **never** overwritten from source, regardless of settings:

`display` · `schedule` · `dim_schedule` · `timezone` · `location` · `web_display_autostart` · `plugin_system` · `plugin-sync`

## Requirements

- LEDMatrix v2.0.0 or higher
- `rsync` and `openssh-client` installed on the Pi (present by default on Raspberry Pi OS)
- Python 3.9+

## License

MIT License

## Support

If this plugin is useful to you, consider buying me a coffee!

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-sarjent-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/sarjent)

## Contributing

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/sarjent/ledmatrix-plugin-sync).
