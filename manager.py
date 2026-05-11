import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.plugin_system.base_plugin import BasePlugin


class _SyncHTTPServer(HTTPServer):
    """HTTPServer subclass that carries shared plugin state for the request handler."""

    def __init__(self, server_address, handler_class, *, plugin_root: Path,
                 sync_token: str, system_keys: frozenset, preserve_keys: frozenset,
                 plugin_id: str) -> None:
        super().__init__(server_address, handler_class)
        self.plugin_root = plugin_root
        self.sync_token = sync_token
        self.system_keys = system_keys
        self.preserve_keys = preserve_keys
        self.plugin_id = plugin_id


class _SyncHandler(BaseHTTPRequestHandler):
    """Serves plugins archive and config over HTTP for pull-based sync."""

    def log_message(self, fmt, *args) -> None:
        pass  # suppress default stdout access log

    def do_GET(self) -> None:
        if self.server.sync_token and self.headers.get("X-Sync-Token") != self.server.sync_token:
            self._json(401, {"error": "Unauthorized"})
            return

        routes = {
            "/api/health":  self._health,
            "/api/plugins": self._plugins_archive,
            "/api/config":  self._config,
            "/api/secrets": self._secrets,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self._json(404, {"error": "Not found"})

    def _health(self) -> None:
        self._json(200, {"status": "ok"})

    def _plugins_archive(self) -> None:
        plugins_dir = self.server.plugin_root / "plugins"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if plugins_dir.exists():
                for item in sorted(plugins_dir.iterdir()):
                    if item.is_dir() and item.name != self.server.plugin_id:
                        tar.add(item, arcname=item.name)
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _config(self) -> None:
        path = self.server.plugin_root / "config" / "config.json"
        try:
            with open(path) as f:
                full = json.load(f)
            preserve = self.server.system_keys | self.server.preserve_keys | {self.server.plugin_id}
            self._json(200, {k: v for k, v in full.items() if k not in preserve})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _secrets(self) -> None:
        path = self.server.plugin_root / "config" / "config_secrets.json"
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._json(404, {"error": "config_secrets.json not found"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PluginSyncPlugin(BasePlugin):

    _STATE_FILENAME = "plugin_sync_state.json"

    # Top-level config.json keys that are Pi-specific and never synced from source.
    _SYSTEM_KEYS = frozenset({
        "display", "schedule", "dim_schedule", "timezone",
        "sync",
        "location", "web_display_autostart", "plugin_system",
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

        self.server_mode: bool = config.get("server_mode", False)
        self.server_port: int = int(config.get("server_port", 5001))
        self.sync_token: str = config.get("sync_token", "")
        self.source_host: str = config.get("source_host", "")
        self.source_port: int = int(config.get("source_port", 5001))
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

        if self.server_mode:
            self._start_server()

    # ── BasePlugin interface ──────────────────────────────────────────────────

    def update(self) -> None:
        if self.server_mode or not self.source_host:
            return

        if not self._is_sync_due():
            return

        self.logger.info(
            "Plugin sync: starting sync from http://%s:%d", self.source_host, self.source_port
        )

        if not self._check_availability():
            self.logger.warning(
                "Plugin sync: http://%s:%d unreachable — skipping this cycle",
                self.source_host, self.source_port,
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
        info: Dict[str, Any] = {
            "mode": "server" if self.server_mode else "destination",
            "last_sync_time": state.get("last_sync_time", "Never"),
            "last_success": state.get("last_success"),
        }
        if self.server_mode:
            info["server_port"] = self.server_port
        else:
            info["source_host"] = self.source_host
            info["source_port"] = self.source_port
            info["sync_due"] = self._is_sync_due()
        return info

    # ── Server mode ───────────────────────────────────────────────────────────

    def _start_server(self) -> None:
        try:
            server = _SyncHTTPServer(
                ("", self.server_port),
                _SyncHandler,
                plugin_root=self._project_root,
                sync_token=self.sync_token,
                system_keys=self._SYSTEM_KEYS,
                preserve_keys=self._extra_preserve,
                plugin_id=self.plugin_id,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.logger.info("Plugin sync: server listening on port %d", self.server_port)
        except OSError as e:
            self.logger.error(
                "Plugin sync: failed to start server on port %d: %s", self.server_port, e
            )

    # ── Pull mode (destination) ───────────────────────────────────────────────

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
        url = f"http://{self.source_host}:{self.source_port}/api/health"
        req = urllib.request.Request(url)
        if self.sync_token:
            req.add_header("X-Sync-Token", self.sync_token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _run_sync(self) -> Tuple[bool, bool]:
        results: List[bool] = []
        any_changes = False

        if self.sync_plugins:
            ok, changed = self._pull_plugins()
            results.append(ok)
            any_changes |= changed

        if self.sync_config:
            ok, changed = self._pull_config()
            results.append(ok)
            any_changes |= changed

        if self.sync_secrets:
            ok, changed = self._pull_secrets()
            results.append(ok)
            any_changes |= changed

        success = all(results) if results else True
        suffix = " — changes detected" if any_changes else " — nothing changed"
        (self.logger.info if success else self.logger.error)(
            "Plugin sync: %s%s",
            "completed successfully" if success else "completed with errors",
            suffix,
        )
        return success, any_changes

    def _pull_plugins(self) -> Tuple[bool, bool]:
        try:
            data = self._http_get("/api/plugins")
        except Exception as e:
            self.logger.error("Plugin sync: failed to fetch plugins archive: %s", e)
            return False, False

        local_plugins = self._project_root / "plugins"
        os.makedirs(local_plugins, exist_ok=True)
        before = self._dir_snapshot(local_plugins)

        if not self.dry_run:
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                    archive_names = {m.name.split("/")[0] for m in tar.getmembers() if m.name}
                    for item in list(local_plugins.iterdir()):
                        if item.is_dir() and item.name not in archive_names and item.name != self.plugin_id:
                            shutil.rmtree(item)
                    try:
                        tar.extractall(local_plugins, filter="data")
                    except TypeError:
                        tar.extractall(local_plugins)
            except Exception as e:
                self.logger.error("Plugin sync: failed to extract plugins archive: %s", e)
                return False, False

        after = self._dir_snapshot(local_plugins)
        return True, before != after

    def _pull_config(self) -> Tuple[bool, bool]:
        try:
            source_cfg: Dict[str, Any] = json.loads(self._http_get("/api/config"))
        except Exception as e:
            self.logger.error("Plugin sync: failed to fetch config: %s", e)
            return False, False

        local_path = self._project_root / "config" / "config.json"
        try:
            with open(local_path) as f:
                local_cfg = json.load(f)

            preserve = self._SYSTEM_KEYS | self._extra_preserve | {self.plugin_id}
            merged = {k: v for k, v in local_cfg.items() if k in preserve}
            synced_keys: List[str] = []
            for key, value in source_cfg.items():
                if key not in preserve:
                    merged[key] = value
                    synced_keys.append(key)

            changed = json.dumps(merged, sort_keys=True) != json.dumps(local_cfg, sort_keys=True)
            if not self.dry_run and changed:
                with open(local_path, "w") as f:
                    json.dump(merged, f, indent=2)
                self.logger.info("Plugin sync: config updated — synced keys: %s", synced_keys)
            elif self.dry_run:
                self.logger.info("Plugin sync [dry-run]: would sync config keys: %s", synced_keys)
            return True, changed
        except Exception as e:
            self.logger.error("Plugin sync: config merge error: %s", e)
            return False, False

    def _pull_secrets(self) -> Tuple[bool, bool]:
        try:
            new_data = self._http_get("/api/secrets")
        except Exception as e:
            self.logger.error("Plugin sync: failed to fetch secrets: %s", e)
            return False, False

        dest = self._project_root / "config" / "config_secrets.json"
        try:
            existing = dest.read_bytes() if dest.exists() else b""
            changed = new_data != existing
            if not self.dry_run and changed:
                dest.write_bytes(new_data)
            return True, changed
        except Exception as e:
            self.logger.error("Plugin sync: failed to write secrets: %s", e)
            return False, False

    # ── HTTP client ───────────────────────────────────────────────────────────

    def _http_get(self, path: str) -> bytes:
        url = f"http://{self.source_host}:{self.source_port}{path}"
        req = urllib.request.Request(url)
        if self.sync_token:
            req.add_header("X-Sync-Token", self.sync_token)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    # ── Service restart ───────────────────────────────────────────────────────

    def _restart_display_service(self) -> None:
        self.logger.info("Plugin sync: restarting %s", self.display_service_name)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.display_service_name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.logger.info("Plugin sync: %s restarted successfully", self.display_service_name)
            else:
                self.logger.error("Plugin sync: restart failed: %s", result.stderr.strip())
        except Exception as e:
            self.logger.error("Plugin sync: restart exception: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _dir_snapshot(path: Path) -> Dict[str, float]:
        return {str(p.relative_to(path)): os.path.getmtime(p) for p in path.rglob("*")}

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
