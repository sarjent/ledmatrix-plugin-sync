import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.plugin_system.base_plugin import BasePlugin


class PluginSyncPlugin(BasePlugin):

    _STATE_FILENAME = "plugin_sync_state.json"

    # Top-level config.json keys that are Pi-specific and always preserved locally.
    # Only keys NOT in this set (i.e. plugin config sections) are pulled from source.
    _SYSTEM_KEYS = frozenset({
        "display",
        "schedule",
        "dim_schedule",
        "timezone",
        "location",
        "web_display_autostart",
        "plugin_system",
    })

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager: Any,
        cache_manager: Any,
        plugin_manager: Any,
    ) -> None:
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        self.source_host: str = config.get("source_host", "")
        self.source_user: str = config.get("source_user", "pi")
        self.source_path: str = config.get("source_ledmatrix_path", "/home/pi/LEDMatrix")
        self.ssh_key_path: str = os.path.expanduser(
            config.get("ssh_key_path", "~/.ssh/ledmatrix_sync_rsa")
        )
        self.sync_plugins: bool = config.get("sync_plugins", True)
        self.sync_config: bool = config.get("sync_config", True)
        self.sync_secrets: bool = config.get("sync_secrets", False)
        self.sync_frequency_hours: float = float(config.get("sync_frequency_hours", 24))
        self.dry_run: bool = config.get("dry_run", False)
        self.auto_restart: bool = config.get("auto_restart", True)
        self.display_service_name: str = config.get("display_service_name", "ledmatrix.service")
        self._extra_preserve: frozenset = frozenset(config.get("preserve_local_keys", []))

        self._project_root = Path(__file__).resolve().parent.parent.parent
        self._state_file = self._project_root / "config" / self._STATE_FILENAME

    # ── BasePlugin interface ──────────────────────────────────────────────────

    def update(self) -> None:
        if not self.source_host:
            return

        if not self._is_sync_due():
            return

        if not os.path.exists(self.ssh_key_path):
            self._generate_ssh_key()
            self.logger.info(
                "Plugin sync: SSH key generated at %s — add the public key to "
                "%s@%s:~/.ssh/authorized_keys, then the next sync will proceed.",
                self.ssh_key_path,
                self.source_user,
                self.source_host,
            )
            return

        self.logger.info("Plugin sync: starting sync from %s@%s", self.source_user, self.source_host)

        if not self._check_availability():
            self.logger.warning(
                "Plugin sync: %s@%s unreachable — skipping this cycle",
                self.source_user,
                self.source_host,
            )
            return

        success, any_changes = self._run_sync()
        self._update_state({"last_sync_time": datetime.now().isoformat(), "last_success": success})

        if success and any_changes and self.auto_restart:
            self._restart_display_service()

    def display(self, force_clear: bool = False) -> None:
        pass

    def get_info(self) -> Dict[str, Any]:
        state = self._load_state()
        return {
            "last_sync_time": state.get("last_sync_time", "Never"),
            "last_success": state.get("last_success"),
            "source_host": self.source_host,
            "public_key": self._read_public_key(),
            "sync_due": self._is_sync_due(),
        }

    # ── Sync orchestration ────────────────────────────────────────────────────

    def _is_sync_due(self) -> bool:
        last = self._load_state().get("last_sync_time")
        if not last:
            return True
        try:
            return datetime.now() - datetime.fromisoformat(last) >= timedelta(
                hours=self.sync_frequency_hours
            )
        except (ValueError, TypeError):
            return True

    def _check_availability(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-i", self.ssh_key_path,
                    "-o", "ConnectTimeout=3",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no",
                    f"{self.source_user}@{self.source_host}",
                    "true",
                ],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception as e:
            self.logger.error("Plugin sync: availability check error: %s", e)
            return False

    def _run_sync(self) -> Tuple[bool, bool]:
        results: List[bool] = []
        any_changes = False

        if self.sync_plugins:
            local_plugins = str(self._project_root / "plugins") + "/"
            os.makedirs(self._project_root / "plugins", exist_ok=True)
            success, changed = self._rsync(
                f"{self.source_user}@{self.source_host}:{self.source_path}/plugins/",
                local_plugins,
                exclude=[self.plugin_id],
            )
            results.append(success)
            any_changes |= changed

        if self.sync_config:
            success, changed = self._sync_config_selective()
            results.append(success)
            any_changes |= changed

        if self.sync_secrets:
            success, changed = self._scp_pull_with_change_detection(
                f"{self.source_user}@{self.source_host}:{self.source_path}/config/config_secrets.json",
                str(self._project_root / "config" / "config_secrets.json"),
            )
            results.append(success)
            any_changes |= changed

        success = all(results) if results else True
        if success:
            self.logger.info("Plugin sync: completed successfully%s", " — changes detected" if any_changes else " — nothing changed")
        else:
            self.logger.error("Plugin sync: completed with one or more errors — check logs above")
        return success, any_changes

    # ── Transfer helpers ──────────────────────────────────────────────────────

    def _ssh_e_flag(self) -> str:
        return (
            f"ssh -i {self.ssh_key_path} "
            "-o StrictHostKeyChecking=no "
            "-o BatchMode=yes"
        )

    def _rsync(self, source: str, dest: str, exclude: Optional[List[str]] = None) -> Tuple[bool, bool]:
        cmd = ["rsync", "-az", "--delete", "--itemize-changes", "-e", self._ssh_e_flag()]
        for ex in (exclude or []):
            cmd += ["--exclude", ex]
        if self.dry_run:
            cmd.append("--dry-run")
        cmd += [source, dest]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                self.logger.error("Plugin sync: rsync failed: %s", result.stderr.strip())
                return False, False
            changed = bool(result.stdout.strip())
            return True, changed
        except subprocess.TimeoutExpired:
            self.logger.error("Plugin sync: rsync timed out after 120s")
            return False, False
        except Exception as e:
            self.logger.error("Plugin sync: rsync exception: %s", e)
            return False, False

    def _scp_pull(self, source: str, dest: str) -> bool:
        if self.dry_run:
            self.logger.info("Plugin sync [dry-run]: would copy %s -> %s", source, dest)
            return True
        cmd = [
            "scp",
            "-i", self.ssh_key_path,
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            source,
            dest,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                self.logger.error("Plugin sync: scp failed: %s", result.stderr.strip())
                return False
            return True
        except Exception as e:
            self.logger.error("Plugin sync: scp exception: %s", e)
            return False

    def _scp_pull_with_change_detection(self, source: str, dest: str) -> Tuple[bool, bool]:
        existing_content: Optional[bytes] = None
        try:
            with open(dest, "rb") as f:
                existing_content = f.read()
        except FileNotFoundError:
            pass

        if not self._scp_pull(source, dest):
            return False, False

        if self.dry_run:
            return True, False

        try:
            with open(dest, "rb") as f:
                new_content = f.read()
            return True, new_content != existing_content
        except Exception:
            return True, True

    def _sync_config_selective(self) -> Tuple[bool, bool]:
        """Pull source config.json and apply only plugin config sections.

        System-level keys (schedule, timezone, display hardware, etc.) are always
        kept from the local config. Only keys that are not in _SYSTEM_KEYS and not
        in preserve_local_keys are considered plugin configs and synced from source.
        """
        tmp = "/tmp/ledmatrix_sync_source_config.json"

        if not self._scp_pull(
            f"{self.source_user}@{self.source_host}:{self.source_path}/config/config.json",
            tmp,
        ):
            return False, False

        local_path = self._project_root / "config" / "config.json"
        try:
            with open(tmp) as f:
                source_cfg: Dict[str, Any] = json.load(f)
            with open(local_path) as f:
                local_cfg: Dict[str, Any] = json.load(f)

            # Build the full set of keys to leave untouched on this Pi
            preserve = self._SYSTEM_KEYS | self._extra_preserve | {self.plugin_id}

            # Start from local config, then apply only plugin-config keys from source
            merged = dict(local_cfg)
            synced_keys: List[str] = []
            for key, value in source_cfg.items():
                if key not in preserve:
                    merged[key] = value
                    synced_keys.append(key)

            changed = json.dumps(merged, sort_keys=True) != json.dumps(local_cfg, sort_keys=True)

            if not self.dry_run:
                if changed:
                    with open(local_path, "w") as f:
                        json.dump(merged, f, indent=2)
                    self.logger.info(
                        "Plugin sync: config.json updated — synced plugin keys: %s", synced_keys
                    )
            else:
                self.logger.info(
                    "Plugin sync [dry-run]: would sync plugin keys: %s", synced_keys
                )

            return True, changed
        except Exception as e:
            self.logger.error("Plugin sync: config merge error: %s", e)
            return False, False
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ── Service restart ───────────────────────────────────────────────────────

    def _restart_display_service(self) -> None:
        self.logger.info("Plugin sync: restarting %s due to detected changes", self.display_service_name)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.display_service_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                self.logger.info("Plugin sync: %s restarted successfully", self.display_service_name)
            else:
                self.logger.error(
                    "Plugin sync: failed to restart %s: %s",
                    self.display_service_name,
                    result.stderr.strip(),
                )
        except Exception as e:
            self.logger.error("Plugin sync: restart exception: %s", e)

    # ── SSH key helpers ───────────────────────────────────────────────────────

    def _generate_ssh_key(self) -> None:
        os.makedirs(os.path.dirname(self.ssh_key_path), exist_ok=True)
        subprocess.run(
            [
                "ssh-keygen",
                "-t", "ed25519",
                "-f", self.ssh_key_path,
                "-N", "",
                "-C", "ledmatrix-plugin-sync",
            ],
            check=True,
            capture_output=True,
        )

    def _read_public_key(self) -> str:
        try:
            with open(self.ssh_key_path + ".pub") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    # ── State helpers ─────────────────────────────────────────────────────────

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _update_state(self, updates: Dict[str, Any]) -> None:
        state = self._load_state()
        state.update(updates)
        try:
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.logger.error("Plugin sync: failed to write state: %s", e)
