"""System tray integration for VetFlowConnect."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from collections.abc import Callable

logger = logging.getLogger("vetflow_connect")

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional in test environments
    pystray = None
    Image = ImageDraw = ImageFont = None

COLOR_GREEN = "#22c55e"
COLOR_RED = "#ef4444"
COLOR_YELLOW = "#f59e0b"


COLOR_UPDATE = "#f97316"  # pomarańczowy badge = dostępna aktualizacja


def create_status_icon(color: str, update: bool = False):
    """Ikona tray'a: kolorowe kółko + „V". Gdy update=True dorysowuje pomarańczowy
    badge ze strzałką „↑" w prawym-górnym rogu = dostępna aktualizacja (widać na ikonie,
    bez otwierania menu)."""
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("pystray and Pillow are required for tray support")

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    draw.text((size // 2, size // 2), "V", fill="white", anchor="mm", font=font)

    if update:
        r = 26  # badge w prawym-górnym rogu
        draw.ellipse([size - r - 1, 1, size - 1, r + 1], fill=COLOR_UPDATE, outline="white", width=3)
        try:
            bfont = ImageFont.truetype("arialbd.ttf", 20)
        except OSError:
            try:
                bfont = ImageFont.truetype("arial.ttf", 20)
            except OSError:
                bfont = ImageFont.load_default()
        cx = size - 1 - r / 2
        draw.text((cx, r / 2 + 1), "↑", fill="white", anchor="mm", font=bfont)
    return image


class TrayApp:
    """System tray icon with plugin status and settings shortcuts."""

    def __init__(
        self,
        *,
        on_quit: Callable[[], None],
        on_logout: Callable[[], None],
        on_open_settings: Callable[[], None] | None = None,
        on_update: Callable[[], None] | None = None,
        log_file: str | None = None,
    ) -> None:
        self._on_quit = on_quit
        self._on_logout = on_logout
        self._on_open_settings = on_open_settings
        self._on_update = on_update
        self._log_file = log_file
        self._status_text = "Uruchamianie..."
        self._clinic_name = ""
        self._plugin_statuses: list[dict] = []
        self._settings_url: str | None = None
        self._icon = None
        self._update_version: str | None = None
        self._update_url: str | None = None
        self._current_color = COLOR_YELLOW  # bazowy kolor ikony (badge dokładany osobno)

    def _menu_items(self):
        if pystray is None:
            return []

        items = [
            pystray.MenuItem(lambda _item: f"Status: {self._status_text}", None, enabled=False),
        ]
        if self._clinic_name:
            items.append(
                pystray.MenuItem(
                    lambda _item: f"Klinika: {self._clinic_name}",
                    None,
                    enabled=False,
                )
            )

        if self._update_url:
            items.append(pystray.Menu.SEPARATOR)
            items.append(
                pystray.MenuItem(f"⬆ Zaktualizuj do v{self._update_version}", self._do_update)
            )

        if self._plugin_statuses:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Pluginy", None, enabled=False))
            for plugin in self._plugin_statuses:
                label = plugin.get("status_text") or f"{plugin['display_name']}: {'OK' if plugin['healthy'] else 'BLAD'}"
                items.append(pystray.MenuItem(label, None, enabled=False))

        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Ustawienia", self._open_settings),
                pystray.MenuItem("Pokaż logi", self._show_logs),
                pystray.MenuItem("Wyloguj", self._logout),
                pystray.MenuItem("Zamknij", self._quit),
            ]
        )
        return items

    def _build_menu(self):
        if pystray is None:
            raise RuntimeError("pystray is required for tray support")
        return pystray.Menu(*self._menu_items())

    def _show_logs(self, *_args) -> None:
        if not self._log_file or not os.path.exists(self._log_file):
            return
        if sys.platform == "win32":
            os.startfile(self._log_file)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", self._log_file])  # noqa: S603, S607

    def _quit(self, *_args) -> None:
        logger.info("Tray: quit requested")
        self._on_quit()
        if self._icon:
            self._icon.stop()

    def _logout(self, *_args) -> None:
        logger.info("Tray: logout requested")
        self._on_logout()
        if self._icon:
            self._icon.stop()

    def _open_settings(self, *_args) -> None:
        if self._on_open_settings is not None:
            self._on_open_settings()
        elif self._settings_url:
            webbrowser.open(self._settings_url)

    def _refresh_menu(self) -> None:
        """Przebuduj menu — statyczne pystray.Menu nie DODA nowej pozycji przez samo
        update_menu() (np. przycisku „Zaktualizuj" ani statusów pluginów dodanych po
        starcie). Trzeba podmienić obiekt menu i dopiero odświeżyć."""
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    def _apply_icon(self) -> None:
        """Przerysuj ikonę: bazowy kolor (status połączenia) + badge gdy jest aktualizacja."""
        if self._icon:
            self._icon.icon = create_status_icon(self._current_color, update=bool(self._update_url))

    def set_status(self, ok: bool, text: str | None = None) -> None:
        self._status_text = text or ("Połączono" if ok else "Błąd połączenia")
        self._current_color = COLOR_GREEN if ok else COLOR_RED
        if self._icon:
            self._apply_icon()
            self._refresh_menu()

    def set_connection(self, clinic_name: str, ok: bool = True, text: str | None = None) -> None:
        self._clinic_name = clinic_name
        self.set_status(ok=ok, text=text or f"Połączono z {clinic_name}")

    def set_plugins(self, plugin_statuses: list[dict]) -> None:
        self._plugin_statuses = plugin_statuses
        self._refresh_menu()

    def set_settings_url(self, url: str) -> None:
        self._settings_url = url

    def notify(self, title: str, message: str) -> None:
        if self._icon:
            self._icon.notify(message, title)

    def _do_update(self, *_args) -> None:
        logger.info("Tray: aktualizacja zażądana → v%s", self._update_version)
        if self._on_update is not None:
            self._on_update()

    def set_update_available(self, version: str, url: str) -> None:
        # url = bezpośredni link do zip nowej wersji; przycisk w menu odpala on_update.
        if version == self._update_version:
            return  # już pokazane — nie spamuj powiadomieniem co sprawdzenie
        self._update_version = version
        self._update_url = url
        self._apply_icon()  # pomarańczowy badge „↑" na ikonie = widać update bez menu
        self._refresh_menu()
        self.notify("VetFlowConnect", f"Dostępna nowa wersja v{version} — menu → Zaktualizuj")

    def run(self, setup: Callable | None = None) -> None:
        if pystray is None:
            logger.error("[TRAY] pystray is None — backend nie zaimportowany!")
            raise RuntimeError("pystray is required for tray support")

        logger.info("[TRAY] backend=%s, tworzę ikonę...", getattr(pystray.Icon, "__module__", "?"))
        icon_img = create_status_icon(COLOR_YELLOW)
        logger.info("[TRAY] ikona-image: %s size=%s", type(icon_img).__name__, getattr(icon_img, "size", "?"))
        self._icon = pystray.Icon(
            name="VetFlowConnect",
            icon=icon_img,
            title="VetFlowConnect",
            menu=self._build_menu(),
        )

        def _wrapped_setup(icon):
            # Nie połykamy błędu — jeśli icon.visible zawiedzie, app działałby
            # "uruchomiony ale niewidoczny w trayu" (dokładnie ten bug). Niech
            # propaguje: outer icon.run() loguje i re-raise, a user-setup (start
            # RuntimeControllera) NIE wystartuje, dopóki ikona nie jest widoczna.
            icon.visible = True
            logger.info("[TRAY] setup wywołany — visible=True ustawione, icon=%r", getattr(icon, "_hwnd", icon))
            if setup is not None:
                setup(icon)

        logger.info("[TRAY] wchodzę w icon.run() (główny wątek, pętla komunikatów)...")
        try:
            self._icon.run(setup=_wrapped_setup)
        except Exception:
            logger.exception("[TRAY] icon.run() rzucił wyjątek")
            raise
        logger.info("[TRAY] icon.run() zakończony")
