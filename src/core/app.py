"""Main VetFlowConnect application lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .api_client import VetFlowClient
from .auto_discover import discover_devices
from .autostart import ensure_autostart
from .config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigNotFoundError,
    app_dir,
    clear_config,
    load_config,
)
from .plugin_manifest import PluginStatus
from .plugin_loader import PluginLoader
from .tray import TrayApp

logger = logging.getLogger("vetflow_connect")
VERSION = "0.5.4"


def setup_logging(config: Config) -> None:
    """Configure console/file logging once per runtime."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger("vetflow_connect")
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # VETFL-671: log MUSI iść obok .exe (app_dir), nie względem CWD. Autostart (Run key)
    # startuje proces z CWD=C:\Windows\system32 → zapis względnej nazwy = PermissionError,
    # a że setup_logging leci PIERWSZY w runtime, ubijał CAŁY start Connecta (Skyla nie
    # łapana). Kotwiczymy ścieżkę do app_dir; plikowy log jest NIE-krytyczny — przy błędzie
    # degradujemy do samej konsoli zamiast wywalać runtime.
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = app_dir() / log_path
    try:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        root_logger.warning(
            "Nie udało się otworzyć pliku logu %s (%s) — loguję tylko na konsolę",
            log_path, exc,
        )


class RuntimeController:
    """Coordinates remote config, plugins, heartbeat, and tray state."""

    def __init__(
        self,
        config: Config,
        tray: TrayApp,
        *,
        config_path: Path,
        plugin_filter: str | None = None,
    ) -> None:
        self.config = config
        self.tray = tray
        self.config_path = config_path
        self.plugin_filter = plugin_filter
        self.client = VetFlowClient(config.url, config.api_key)
        self.loader = PluginLoader(server_url=config.url)
        self.clinic_info: dict = {}
        self.remote_config: dict = {}
        self.plugins: list[tuple[dict, object]] = []
        self.blocked_plugins: list[dict] = []
        self.exit_mode = "quit"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending_update: tuple[str, str, str] | None = None  # (wersja, url_zip, url_sha)

    def run_in_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._loop.create_task(self._run())
        self._loop.run_forever()
        pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def request_quit(self) -> None:
        self.exit_mode = "quit"
        self._signal_stop()

    def request_logout(self) -> None:
        self.exit_mode = "logout"
        clear_config(self.config_path)
        self._signal_stop()

    def open_settings(self) -> None:
        webbrowser.open(f"{self.config.url}/settings/devices")

    def _signal_stop(self) -> None:
        if self._loop and self._stop_event and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def _connect_with_retry(self) -> bool:
        """Ponawiaj register_device + get_device_config aż się uda. Autostart (Run key)
        przy logowaniu wstaje szybciej niż sieć — 1. próba pada, więc backoff 2→30s w
        nieskończoność (connect ma się w końcu połączyć). False = zażądano stopu w trakcie."""
        delay = 2
        attempt = 0
        while self._stop_event is None or not self._stop_event.is_set():
            attempt += 1
            try:
                self.clinic_info = await self.client.register_device()
                self.remote_config = await self.client.get_device_config()
                return True
            except Exception as exc:  # noqa: BLE001 — każdy błąd (sieć/DNS/serwer) = ponów
                logger.warning(
                    "Połączenie z VetFlow nieudane (próba %d): %s — ponawiam za %ds",
                    attempt, exc, delay,
                )
                self.tray.set_status(False, f"Łączenie z VetFlow… (próba {attempt})")
                if self._stop_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:  # przerywalny backoff — stop kończy natychmiast
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                        return False
                    except asyncio.TimeoutError:
                        pass
                delay = min(delay * 2, 30)
        return False

    async def _check_for_update(self) -> None:
        """Best-effort: nowsza wersja na GitHub → pokaż przycisk Zaktualizuj w tray'u.
        Retry 3× po 15s (wolne sieci klinik potrafią timeoutować 1. próbę). Nie wywala runtime."""
        from . import updater
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            try:
                result = await loop.run_in_executor(None, updater.check_latest, VERSION)
                if result:
                    version, zip_url, sha_url = result
                    self._pending_update = (version, zip_url, sha_url)
                    logger.info("Dostępna aktualizacja: v%s", version)
                    self.tray.set_update_available(version, zip_url)
                return  # sukces (także gdy brak nowszej wersji) — koniec
            except Exception as exc:  # noqa: BLE001
                logger.debug("Sprawdzenie aktualizacji nieudane (próba %d): %s", attempt + 1, exc)
                await asyncio.sleep(15)

    def trigger_update(self) -> None:
        """Przycisk „Zaktualizuj" z tray'u (wątek pystray). Pobiera+weryfikuje+podmienia+
        WYMUSZA zamknięcie i restart. Cała robota w osobnym wątku — nie blokuj tray'a."""
        pending = self._pending_update
        if not pending:
            return
        version, zip_url, sha_url = pending

        def _worker() -> None:
            from . import updater
            try:
                self.tray.notify("VetFlowConnect", f"Pobieram v{version}…")
                self.tray.set_status(False, f"Aktualizuję do v{version}…")
                staged = updater.download_and_stage(zip_url, sha_url)
                self.tray.notify("VetFlowConnect", "Aktualizuję i restartuję…")
                updater.launch_swap_and_exit(staged)
                self.request_quit()  # wymuś zamknięcie → updater podmieni folder i odpali nowy .exe
            except Exception as exc:  # noqa: BLE001
                logger.exception("Aktualizacja nieudana")
                self.tray.notify("VetFlowConnect", f"Aktualizacja nieudana: {exc}")
                self.tray.set_status(True, "Nasłuchuje")

        threading.Thread(target=_worker, daemon=True).start()

    async def _run(self) -> None:
        try:
            setup_logging(self.config)
            logger.info("=" * 60)
            logger.info("VetFlowConnect v%s", VERSION)
            logger.info("VetFlow URL: %s", self.config.url)
            logger.info("=" * 60)

            # VETFL-671: reconnect z backoffem — autostart (Run key) przy logowaniu wstaje
            # SZYBCIEJ niż sieć, więc register_device pada; zamiast poddać się (czerwony na
            # stałe) ponawiamy aż sieć wstanie. False = zażądano stopu w trakcie łączenia.
            if not await self._connect_with_retry():
                return
            clinic_name = (
                self.remote_config.get("clinic_name")
                or self.clinic_info.get("clinic_name")
                or self.clinic_info.get("name")
                or "VetFlow"
            )

            self.tray.set_connection(clinic_name, ok=True, text=f"Połączono z {clinic_name}")
            self.tray.set_settings_url(f"{self.config.url}/settings/devices")

            await self._start_plugins()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self.tray.set_plugins(await self._plugin_statuses())
            asyncio.create_task(self._check_for_update())  # sprawdź aktualizację przy starcie

            if self._stop_event is not None:
                await self._stop_event.wait()
        except Exception as exc:
            logger.exception("Runtime startup failed")
            self.tray.set_status(False, str(exc))
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            await self._stop_plugins()
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

    async def _start_plugins(self) -> None:
        available_plugins = self.loader.discover()
        self.blocked_plugins = self.loader.blocked_plugin_statuses()
        remote_devices = [
            device
            for device in self.remote_config.get("devices", [])
            if device.get("enabled", True)
        ]
        if self.plugin_filter:
            remote_devices = [device for device in remote_devices if device.get("plugin") == self.plugin_filter]

        if self.remote_config.get("settings", {}).get("auto_discover"):
            discover_ports = [int(device.get("port", 0)) for device in remote_devices if device.get("port")]
            await discover_devices(ports=discover_ports or None)

        for device in remote_devices:
            plugin_name = device.get("plugin")
            plugin_class = available_plugins.get(plugin_name)
            if plugin_class is None:
                logger.warning("No plugin found for '%s'", plugin_name)
                continue

            plugin = plugin_class()
            plugin.configure(api_client=self.client, lab_result_handler=self._handle_lab_result)
            await plugin.start(device)
            self.plugins.append((device, plugin))

        if not self.plugins:
            logger.warning("No active plugins started")
            self.tray.set_status(False, "Brak aktywnych pluginów")
        else:
            self.tray.set_status(True, "Nasłuchuje")

    async def _stop_plugins(self) -> None:
        for device, plugin in self.plugins:
            try:
                await plugin.stop()
            except Exception:
                logger.exception("Failed to stop plugin %s", device.get("plugin"))
        self.plugins.clear()

    async def _plugin_statuses(self) -> list[dict]:
        statuses = list(self.blocked_plugins)
        for device, plugin in self.plugins:
            try:
                healthy = await plugin.health_check()
            except Exception:
                healthy = False
            verification = self.loader.verification_for(plugin.name)
            statuses.append(
                {
                    "name": plugin.name,
                    "display_name": device.get("name", plugin.display_name),
                    "healthy": healthy,
                    "license_status": verification.status.value,
                    "status_text": self._plugin_status_text(device, plugin, verification.status),
                }
            )
        return statuses

    async def _heartbeat_loop(self) -> None:
        settings = self.remote_config.get("settings", {})
        interval = int(settings.get("heartbeat_interval", 60))
        beats = 0
        check_every = max(1, 3600 // max(1, interval))  # sprawdzaj aktualizację ~co godzinę
        while self._stop_event is not None and not self._stop_event.is_set():
            payload = {
                "clinic_id": self.remote_config.get("clinic_id"),
                "clinic_name": self.remote_config.get("clinic_name"),
                "plugins": await self._plugin_statuses(),
            }
            await self.client.send_heartbeat(payload)
            self.tray.set_plugins(payload["plugins"])
            beats += 1
            if beats % check_every == 0:
                await self._check_for_update()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _handle_lab_result(self, _data) -> None:
        """Reserved for cross-plugin hooks."""

    def _plugin_status_text(self, device: dict, plugin, status: PluginStatus) -> str:
        display_name = device.get("name", plugin.display_name)
        if status == PluginStatus.EXPIRED:
            verification = self.loader.verification_for(plugin.name)
            expires_at = verification.manifest.expires_at.strftime("%d.%m.%Y") if verification.manifest and verification.manifest.expires_at else "?"
            return f"Plugin {display_name}: licencja wygasla {expires_at}"
        if status == PluginStatus.DEV_MODE:
            return f"Plugin {display_name}: aktywny (dev mode)"
        if status == PluginStatus.OK:
            verification = self.loader.verification_for(plugin.name)
            if verification.manifest and verification.manifest.license_type == "open":
                return f"Plugin {display_name}: aktywny (licencja: bezterminowa)"
            return f"Plugin {display_name}: aktywny"
        return f"Plugin {display_name}: status {status.value}"


def _ensure_stdout() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115


def _load_or_setup_config(config_path: Path | None) -> Config | None:
    try:
        return load_config(config_path)
    except ConfigNotFoundError:
        try:
            from ..setup_wizard import run_setup_wizard
        except ImportError:
            from setup_wizard import run_setup_wizard

        result = run_setup_wizard(config_path=config_path)
        if not result.saved or result.config is None:
            return None

        # On Windows, Tkinter's mainloop leaves the GUI subsystem in a state
        # where pystray cannot create a tray icon in the same process.
        # Restart the process so the tray icon initialises cleanly.
        if sys.platform == "win32":
            import subprocess

            args = [sys.executable] + sys.argv
            subprocess.Popen(args)  # noqa: S603
            sys.exit(0)

        return result.config


async def run_discover() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    found = await discover_devices()
    if found:
        print(f"\nFound {len(found)} device(s):")
        for host, port in found:
            print(f"  {host}:{port}")
    else:
        print("\nNo devices found on local network.")


def main() -> None:
    _ensure_stdout()

    parser = argparse.ArgumentParser(
        prog="vetflow-connect",
        description="VetFlowConnect - plugin-based device connector",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--plugin", type=str, default=None)
    args = parser.parse_args()

    if args.discover:
        asyncio.run(run_discover())
        return

    # VETFL-658: zarejestruj autostart przy logowaniu (Windows .exe) — po restarcie/zaniku prądu
    # Connect wstaje sam, user nie musi go włączać. No-op w dev i poza Windows.
    ensure_autostart()

    while True:
        config = _load_or_setup_config(args.config)
        if config is None:
            return

        controller_holder: dict[str, RuntimeController] = {}

        def on_quit() -> None:
            controller_holder["controller"].request_quit()

        def on_logout() -> None:
            controller_holder["controller"].request_logout()

        tray = TrayApp(
            on_quit=on_quit,
            on_logout=on_logout,
            on_open_settings=lambda: controller_holder["controller"].open_settings(),
            on_update=lambda: controller_holder["controller"].trigger_update(),
            log_file=str(config.log_file),
        )

        controller = RuntimeController(
            config=config,
            tray=tray,
            config_path=args.config,
            plugin_filter=args.plugin,
        )
        controller_holder["controller"] = controller

        def on_tray_ready(_icon) -> None:
            thread = threading.Thread(target=controller.run_in_thread, daemon=True)
            thread.start()

        try:
            tray.run(setup=on_tray_ready)
        except Exception as exc:
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, f"Blad: {exc}", "VetFlowConnect", 0x10)
            else:
                raise

        if controller.exit_mode == "logout":
            continue
        break
