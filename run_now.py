#!/usr/bin/env python3
"""
Run Now action for plugin-sync (destination mode).
Immediately fetches plugins and config from the source Pi's HTTP sync server.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


SYSTEM_KEYS = frozenset({
    "display", "schedule", "dim_schedule", "timezone",
    "location", "web_display_autostart", "plugin_system",
})

PLUGIN_ID = "plugin-sync"


def load_config(ledmatrix_root: Path) -> dict:
    with open(ledmatrix_root / "config" / "config.json") as f:
        return json.load(f).get(PLUGIN_ID, {})


def http_get(host: str, port: int, path: str, token: str = "") -> bytes:
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Sync-Token", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def check_availability(host: str, port: int, token: str) -> bool:
    url = f"http://{host}:{port}/api/health"
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Sync-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def sync_plugins(host: str, port: int, token: str, local_root: Path) -> bool:
    data = http_get(host, port, "/api/plugins", token)
    local_plugins = local_root / "plugins"
    os.makedirs(local_plugins, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        archive_names = {m.name.split("/")[0] for m in tar.getmembers() if m.name}
        removed = []
        for item in list(local_plugins.iterdir()):
            if item.is_dir() and item.name not in archive_names and item.name != PLUGIN_ID:
                shutil.rmtree(item)
                removed.append(item.name)
        try:
            tar.extractall(local_plugins, filter="data")
        except TypeError:
            tar.extractall(local_plugins)

    if archive_names:
        print(f"Plugins synced: {sorted(archive_names)}")
    if removed:
        print(f"Plugins removed: {removed}")
    return True


def sync_config(host: str, port: int, token: str, local_root: Path, preserve_extra: list) -> bool:
    source_cfg = json.loads(http_get(host, port, "/api/config", token))
    local_path = local_root / "config" / "config.json"

    with open(local_path) as f:
        local_cfg = json.load(f)

    preserve = SYSTEM_KEYS | set(preserve_extra) | {PLUGIN_ID}
    merged = dict(local_cfg)
    synced_keys = []
    for key, value in source_cfg.items():
        if key not in preserve:
            merged[key] = value
            synced_keys.append(key)

    changed = json.dumps(merged, sort_keys=True) != json.dumps(local_cfg, sort_keys=True)
    if changed:
        with open(local_path, "w") as f:
            json.dump(merged, f, indent=2)
        print(f"Config updated — synced keys: {synced_keys}")
    else:
        print("Config: no changes")
    return changed


def sync_secrets(host: str, port: int, token: str, local_root: Path) -> bool:
    data = http_get(host, port, "/api/secrets", token)
    dest = local_root / "config" / "config_secrets.json"
    existing = dest.read_bytes() if dest.exists() else b""
    if data != existing:
        dest.write_bytes(data)
        print("Secrets: updated")
        return True
    print("Secrets: no changes")
    return False


def update_state(local_root: Path) -> None:
    for state_file in [
        local_root / "config" / "plugin_sync_state.json",
        Path("/tmp/plugin_sync_state.json"),
    ]:
        try:
            existing = json.loads(state_file.read_text()) if state_file.exists() else {}
            existing["last_sync_time"] = datetime.now().isoformat()
            existing["last_success"] = True
            state_file.write_text(json.dumps(existing, indent=2))
            return
        except PermissionError:
            continue


def restart_service(service_name: str) -> None:
    print(f"Restarting {service_name}...")
    result = subprocess.run(
        ["sudo", "systemctl", "restart", service_name],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        print(f"{service_name} restarted successfully")
    else:
        print(f"WARNING: restart failed: {result.stderr.strip()}", file=sys.stderr)


def main() -> None:
    ledmatrix_root = Path(os.environ.get("LEDMATRIX_ROOT", Path(__file__).parent.parent.parent))

    try:
        cfg = load_config(ledmatrix_root)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        sys.exit(1)

    server_port = int(cfg.get("server_port", 5001))

    def is_source() -> bool:
        if cfg.get("server_mode", False):
            return True
        try:
            with socket.create_connection(("localhost", server_port), timeout=1):
                return True
        except OSError:
            return False

    if is_source():
        print(f"This Pi is the sync source (server running on port {server_port}) — nothing to pull.")
        print("Run Sync Now is only used on destination Pi's.")
        sys.exit(0)

    source_host = cfg.get("source_host", "")
    source_port = int(cfg.get("source_port", server_port))
    sync_token = cfg.get("sync_token", "")
    do_plugins = cfg.get("sync_plugins", True)
    do_config = cfg.get("sync_config", True)
    do_secrets = cfg.get("sync_secrets", False)
    auto_restart = cfg.get("auto_restart", True)
    service_name = cfg.get("display_service_name", "ledmatrix.service")
    preserve_extra = cfg.get("preserve_local_keys", [])

    if not source_host:
        print("ERROR: source_host is not configured.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to http://{source_host}:{source_port}...")
    if not check_availability(source_host, source_port, sync_token):
        print(
            f"ERROR: Cannot reach {source_host}:{source_port}. "
            "Make sure plugin-sync is installed on the source Pi with server_mode: true.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Connection OK")

    any_changes = False

    try:
        if do_plugins:
            any_changes |= sync_plugins(source_host, source_port, sync_token, ledmatrix_root)

        if do_config:
            any_changes |= sync_config(source_host, source_port, sync_token, ledmatrix_root, preserve_extra)

        if do_secrets:
            any_changes |= sync_secrets(source_host, source_port, sync_token, ledmatrix_root)

    except urllib.error.URLError as e:
        print(f"ERROR: Connection failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    update_state(ledmatrix_root)

    if any_changes and auto_restart:
        restart_service(service_name)
    elif not any_changes:
        print("No changes detected — service not restarted")

    print("Done.")


if __name__ == "__main__":
    main()
