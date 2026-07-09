"""VETFL-671: auto-update connecta (GitHub releases) — self-update z podmianą folderu.

Przepływ (na klik „Zaktualizuj" w tray'u):
  1. pobierz zip nowej wersji do %TEMP% + zweryfikuj SHA256  — ZANIM cokolwiek tknie instalację
  2. rozpakuj do %TEMP%
  3. odpal updater.bat (osobny, odczepiony proces — przeżywa zamknięcie connecta):
       czeka → taskkill /f connecta (wymuszone zamknięcie — nie da się nadpisać działającego
       .exe) → robocopy nowy→instalacja (/E: kopiuje bez kasowania, config.json+log zostają)
       → odpala nowy .exe
  4. connect się zamyka
Bezpieczeństwo: instalacja NIE jest ruszana, dopóki pobranie + SHA + rozpakówka nie przejdą.
Jak którykolwiek krok padnie → wyjątek, caller łapie, instalacja nietknięta, connect działa dalej.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("vetflow_connect")

_REPO = "vet-flow/vetflow-connect"
_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_ZIP = "VetFlowConnect-windows.zip"


def _parse(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for x in v.lstrip("vV").split("."):
        if x.isdigit():
            parts.append(int(x))
        else:
            break
    return tuple(parts)


def check_latest(current: str) -> tuple[str, str, str] | None:
    """Zwraca (wersja, url_zip, url_sha) jeśli GitHub ma NOWSZY release niż `current`,
    inaczej None. Best-effort — caller łapie wyjątki (sieć/GitHub)."""
    req = urllib.request.Request(
        _API, headers={"User-Agent": "VetFlowConnect", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    tag = str(data.get("tag_name", "")).strip()
    if not tag or not _parse(tag) or _parse(tag) <= _parse(current):
        return None
    base = f"https://github.com/{_REPO}/releases/download/{tag}"
    return tag.lstrip("vV"), f"{base}/{_ZIP}", f"{base}/{_ZIP}.sha256"


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "VetFlowConnect"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download_and_stage(zip_url: str, sha_url: str) -> Path:
    """Pobierz + zweryfikuj SHA256 + rozpakuj do %TEMP%. Zwraca ścieżkę rozpakowanego
    folderu `VetFlowConnect`. Rzuca przy błędzie/niezgodności SHA — instalacja NIE tknięta."""
    work = Path(tempfile.mkdtemp(prefix="vfc_update_"))
    zpath = work / _ZIP
    _download(zip_url, zpath)

    # SHA256 — jeśli plik .sha256 istnieje, MUSI się zgadzać (integralność pobrania)
    try:
        spath = work / (_ZIP + ".sha256")
        _download(sha_url, spath)
        expected = spath.read_text().split()[0].strip().lower()
    except Exception as exc:  # noqa: BLE001 — brak/niedostępny sha: pobranie i tak z HTTPS
        logger.warning("Nie pobrano pliku SHA256 (%s) — pomijam weryfikację", exc)
        expected = ""
    if expected:
        actual = _sha256(zpath)
        if actual != expected:
            raise RuntimeError(
                f"SHA256 się nie zgadza (oczekiwano {expected[:12]}…, jest {actual[:12]}…) — przerywam"
            )

    extract = work / "extract"
    with zipfile.ZipFile(zpath) as z:
        z.extractall(extract)
    staged = extract / "VetFlowConnect"
    if not (staged / "VetFlowConnect.exe").exists():
        raise RuntimeError("Rozpakowany pakiet nie zawiera VetFlowConnect.exe")
    return staged


def launch_swap_and_exit(staged_dir: Path) -> None:
    """Odpal odczepiony updater.bat, który po WYMUSZONYM zamknięciu connecta podmienia
    folder instalacji zawartością staged_dir i odpala nowy .exe. Tylko dla frozen (.exe)."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update działa tylko dla zbudowanego .exe (nie z pythona)")
    install_dir = Path(sys.executable).resolve().parent
    exe_name = Path(sys.executable).name
    bat = Path(tempfile.gettempdir()) / "vfc_update.bat"
    # /E = kopiuj nowe BEZ kasowania (config.json + log w instalacji NIE są w źródle → zostają).
    # taskkill /f = wymuszone zamknięcie (nie nadpiszesz działającego .exe). /R:5 /W:2 = retry.
    bat.write_text(
        "@echo off\r\n"
        "timeout /t 3 /nobreak >nul\r\n"
        f'taskkill /f /im "{exe_name}" >nul 2>&1\r\n'
        "timeout /t 1 /nobreak >nul\r\n"
        f'robocopy "{staged_dir}" "{install_dir}" /E /R:5 /W:2 >nul\r\n'
        f'start "" "{install_dir / exe_name}"\r\n'
        'del "%~f0"\r\n',
        encoding="ascii",
    )
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — przeżywa zamknięcie connecta
    subprocess.Popen(  # noqa: S603
        ["cmd", "/c", str(bat)],
        creationflags=0x00000008 | 0x00000200,
        close_fds=True,
    )
    logger.info("Updater odpalony (%s) — podmieni instalację po zamknięciu connecta", bat)
