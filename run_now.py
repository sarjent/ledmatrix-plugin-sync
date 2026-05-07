#!/usr/bin/env python3
"""
Run Now action script for plugin-sync.
Executed by the LEDMatrix web UI when the user clicks "Run Sync Now".
Reads plugin config, performs an immediate sync, and restarts the display
service if changes are detected.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SYSTEM_KEYS = frozenset({
    "display", "schedule", "dim_schedule", "timezone",
    "location", "web_display_autostart", "plugin_system",
})


def load_config(ledmatrix_root: Path) -> dict:
    with open(ledmatrix_root / "config" / "config.json") as f:
        return json.load(f).get("plugin-sync", {})


def check_availability(ssh_key: str, user: str, host: str) -> bool:
    result = subprocess.run(
        [
            "ssh", "-i", ssh_key,
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            f"{user}@{host}", "true",
        ],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


def sync_plugins(ssh_key: str, user: str, host: str, source_path: str,
                 local_root: Path) -> bool:
    dest = str(local_root / "plugins") + "/"
    os.makedirs(local_root / "plugins", exist_ok=True)
    ssh_e = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no -o BatchMode=yes"
    result = subprocess.run(
        [
            "rsync", "-az", "--delete", "--itemize-changes",
            "-e", ssh_e,
            "--exclude", "plugin-sync",
            f"{user}@{host}:{source_path}/plugins/",
            dest,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"ERROR syncing plugins: {result.stderr.strip()}", file=sys.stderr)
        return False
    if result.stdout.strip():
        print(f"Plugins updated:\n{result.stdout.strip()}")
        return True
    print("Plugins: no changes")
    return False


def sync_config(ssh_key: str, user: str, host: str, source_path: str,
                local_root: Path, preserve_extra: list) -> bool:
    tmp = "/tmp/ledmatrix_sync_run_now_config.json"
    ssh_e = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no -o BatchMode=yes"
    result = subprocess.run(
        [
            "scp", "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            f"{user}@{host}:{source_path}/config/config.json",
            tmp,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"ERROR fetching config.json: {result.stderr.strip()}", file=sys.stderr)
        return False

    local_path = local_root / "config" / "config.json"
    try:
        with open(tmp) as f:
            source_cfg = json.load(f)
        with open(local_path) as f:
            local_cfg = json.load(f)

        preserve = SYSTEM_KEYS | set(preserve_extra) | {"plugin-sync"}
        merged = dict(local_cfg)
        synced_keys = []
        for key, value in source_cfg.items():
            if key not in preserve:
                merged[key] = value
                synced_keys.append(key)

        if json.dumps(merged, sort_keys=True) == json.dumps(local_cfg, sort_keys=True):
            print("Config: no changes")
            return False

        with open(local_path, "w") as f:
            json.dump(merged, f, indent=2)
        print(f"Config updated — synced keys: {synced_keys}")
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def sync_secrets(ssh_key: str, user: str, host: str, source_path: str,
                 local_root: Path) -> bool:
    dest = str(local_root / "config" / "config_secrets.json")
    try:
        with open(dest, "rb") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = None

    result = subprocess.run(
        [
            "scp", "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            f"{user}@{host}:{source_path}/config/config_secrets.json",
            dest,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"ERROR syncing secrets: {result.stderr.strip()}", file=sys.stderr)
        return False

    with open(dest, "rb") as f:
        new_content = f.read()
    if new_content != existing:
        print("Secrets: updated")
        return True
    print("Secrets: no changes")
    return False


def update_state(ledmatrix_root: Path, success: bool) -> None:
    state_file = ledmatrix_root / "config" / "plugin_sync_state.json"
    try:
        with open(state_file) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state["last_sync_time"] = datetime.now().isoformat()
    state["last_success"] = success
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def restart_service(service_name: str) -> None:
    print(f"Changes detected — restarting {service_name}...")
    result = subprocess.run(
        ["sudo", "systemctl", "restart", service_name],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        print(f"{service_name} restarted successfully")
    else:
        print(f"WARNING: failed to restart {service_name}: {result.stderr.strip()}", file=sys.stderr)


def main() -> None:
    ledmatrix_root = Path(os.environ.get("LEDMATRIX_ROOT", Path(__file__).parent.parent.parent))

    try:
        cfg = load_config(ledmatrix_root)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        sys.exit(1)

    source_host = cfg.get("source_host", "")
    source_user = cfg.get("source_user", "pi")
    source_path = cfg.get("source_ledmatrix_path", "/home/pi/LEDMatrix")
    ssh_key = os.path.expanduser(cfg.get("ssh_key_path", "~/.ssh/ledmatrix_sync_rsa"))
    do_plugins = cfg.get("sync_plugins", True)
    do_config = cfg.get("sync_config", True)
    do_secrets = cfg.get("sync_secrets", False)
    auto_restart = cfg.get("auto_restart", True)
    service_name = cfg.get("display_service_name", "ledmatrix.service")
    preserve_extra = cfg.get("preserve_local_keys", [])

    if not source_host:
        print("ERROR: source_host is not configured.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(ssh_key):
        print(f"SSH key not found at {ssh_key} — generating now...")
        key_dir = os.path.dirname(ssh_key)
        os.makedirs(key_dir, exist_ok=True)
        result = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", ssh_key, "-N", "", "-C", "ledmatrix-plugin-sync"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"ERROR: Failed to generate SSH key: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        print(f"SSH key generated.")

    pub_key_path = ssh_key + ".pub"
    if os.path.exists(pub_key_path):
        with open(pub_key_path) as f:
            pub_key = f.read().strip()
        print(f"\nPublic key (add this to {source_user}@{source_host}:~/.ssh/authorized_keys if not already done):\n{pub_key}\n")

    print(f"Connecting to {source_user}@{source_host}...")
    if not check_availability(ssh_key, source_user, source_host):
        print(f"ERROR: Cannot reach {source_host}. If the key was just generated, add the public key above to the source Pi's authorized_keys and try again.", file=sys.stderr)
        sys.exit(1)
    print("Connection OK")

    any_changes = False

    if do_plugins:
        any_changes |= sync_plugins(ssh_key, source_user, source_host, source_path, ledmatrix_root)

    if do_config:
        any_changes |= sync_config(ssh_key, source_user, source_host, source_path, ledmatrix_root, preserve_extra)

    if do_secrets:
        any_changes |= sync_secrets(ssh_key, source_user, source_host, source_path, ledmatrix_root)

    update_state(ledmatrix_root, success=True)

    if any_changes and auto_restart:
        restart_service(service_name)
    elif not any_changes:
        print("No changes detected — service not restarted")

    print("Done.")


if __name__ == "__main__":
    main()
