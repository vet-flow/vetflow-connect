"""VETFL-658: autostart VetFlowConnect przy logowaniu do Windows.

Incydent (Górska): po zaniku prądu PC się zrestartował, nikt ręcznie nie włączył Connecta,
wyniki z analizatorów nie szły. Fix: Connect rejestruje się w kluczu Run → po restarcie i
zalogowaniu wstaje SAM, bez ręki użytkownika.

UWAGA — to autostart PRZY LOGOWANIU (HKCU Run). Prawdziwy boot-start BEZ logowania wymaga
Windows Service (osobny, większy refaktor — patrz VETFL-658 follow-up). Rekomendacja dla
recepcji: włączyć auto-login na dedykowanym PC + ten autostart = pełne „wstaje sam po prądzie".
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("vetflow-connect")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VetFlowConnect"


def _is_frozen_windows() -> bool:
    """Tylko dla zbudowanego .exe na Windows — w dev (python src) NIE ruszamy rejestru."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def ensure_autostart() -> None:
    """Zarejestruj VetFlowConnect do autostartu przy logowaniu (idempotentnie).

    No-op poza zbudowanym .exe na Windows. Nie wywala aplikacji przy błędzie rejestru —
    autostart to wygoda, nie funkcja krytyczna, więc awaria zapisu tylko loguje warning.
    """
    if not _is_frozen_windows():
        return
    try:
        import winreg  # tylko Windows

        exe_path = f'"{sys.executable}"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            try:
                current, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            except FileNotFoundError:
                current = None
            if current != exe_path:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, exe_path)
                logger.info("Autostart zarejestrowany (Run key): %s", exe_path)
    except Exception as exc:  # noqa: BLE001 — świadomie: nie blokujemy startu aplikacji
        logger.warning("Nie udało się zarejestrować autostartu: %s", exc)
