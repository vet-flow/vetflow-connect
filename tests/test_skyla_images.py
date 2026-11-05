"""VETFL-975: wykresy i histogramy (OBX typu ED) idą do Vet-Flow tylko z wynikiem tej samej wiadomości.

Wcześniej parser zapisywał obrazy do katalogu wtyczki (w .exe: _internal), a wtyczka wysyłała wszystkie
*.jpg z app_dir()/captured_images — inne katalogi, więc nic nie szło; a po samym wyrównaniu katalogów
obrazy poprzedniego pacjenta trafiałyby do wyników kolejnych.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest

from src.core import config as core_config
from src.plugins.skyla import plugin as plugin_module
from src.plugins.skyla.astm import parse_astm_orders
from src.plugins.skyla.hl7_parser import parse_hl7
from src.plugins.skyla.plugin import SkylaPlugin


def _jpeg_b64(color: tuple[int, int, int]) -> str:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(out, format="JPEG")
    return base64.b64encode(out.getvalue()).decode()


def _vm100(message_id: str, patient: str, images: dict[str, str]) -> str:
    obx_images = [f"OBX|{20 + i}|ED|{name}||^{data}" for i, (name, data) in enumerate(images.items())]
    return "\r".join([
        rf"MSH|^~\&|VM100|Skyla|||20260101120000||ORU^R01|{message_id}|P|2.3.1",
        f"PID|1||||||20200101|M|{patient}|Canine",
        "PV1|1||||P001",
        "OBR|1|||^CBC",
        "OBX|1|NM|WBC^WBC||10.2|10^9/L|6.0-17.0||||F",
        *obx_images,
    ]) + "\r"


class _FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.images: list[tuple[int, list[tuple[str, bytes]]]] = []

    async def send_lab_result(self, panel: dict):
        self.sent.append(panel)
        return len(self.sent)

    async def send_images(self, lab_result_id: int, image_paths: list[Path]) -> bool:
        self.images.append((lab_result_id, [(p.name, p.read_bytes()) for p in image_paths]))
        return True


def _plugin(api: _FakeApi) -> SkylaPlugin:
    plugin = SkylaPlugin()
    plugin.configure(api_client=api)
    plugin._device_config = {"name": "Skyla VM100"}
    return plugin


def test_parser_keeps_images_on_message_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    red = _jpeg_b64((200, 0, 0))
    m = parse_hl7(_vm100("M1", "Burek", {"WBCHistogram": red}))
    assert m.images == [("WBCHistogram_seq20.jpg", base64.b64decode(red))]
    assert [r.name for r in m.results] == ["WBC"]
    assert not any(tmp_path.iterdir()), "parser nie zapisuje plików"


def test_astm_orders_have_own_empty_image_lists():
    text = "\r".join([
        r"H|\^&|||skyla Solution^DS06|||||||P|1394-97|20151014183758",
        "P|1||123||Name",
        "O|1|||^^^A|||||||N",
        "R|1|^^^ALB^^^^M|3.0|g/dL|2.6^4.1|N||F",
        "O|2|||^^^B|||||||N",
        "R|2|^^^ALP^^^^M|20|U/L|0^30|N||F",
        "L|1|N",
    ]) + "\r"
    first, second = parse_astm_orders(text)
    assert first.images == [] and second.images == []
    first.images.append(("x.jpg", b"x"))
    assert second.images == [], "panele ASTM nie dzielą listy obrazów"


@pytest.mark.asyncio
async def test_second_message_uploads_only_its_own_images(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "app_dir", lambda: tmp_path)
    api = _FakeApi()
    plugin = _plugin(api)
    red, blue, green = _jpeg_b64((200, 0, 0)), _jpeg_b64((0, 0, 200)), _jpeg_b64((0, 200, 0))

    await plugin._handle_message(_vm100("M1", "Burek", {"WBCHistogram": red, "RBCHistogram": blue}))
    await plugin._handle_message(_vm100("M2", "Mruczek", {"PLTHistogram": green}))

    assert [p["patient_name"] for p in api.sent] == ["Burek", "Mruczek"]
    assert api.images == [
        (1, [("WBCHistogram_seq20.jpg", base64.b64decode(red)), ("RBCHistogram_seq21.jpg", base64.b64decode(blue))]),
        (2, [("PLTHistogram_seq20.jpg", base64.b64decode(green))]),
    ], "drugi pacjent dostaje tylko swój histogram, nie obrazy Burka"


@pytest.mark.asyncio
async def test_message_without_images_uploads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, "app_dir", lambda: tmp_path)
    api = _FakeApi()
    plugin = _plugin(api)

    await plugin._handle_message(_vm100("M1", "Burek", {"WBCHistogram": _jpeg_b64((200, 0, 0))}))
    await plugin._handle_message(_vm100("M2", "Mruczek", {}))

    assert len(api.sent) == 2
    assert [lab_id for lab_id, _ in api.images] == [1], "wynik bez obrazów nie dostaje plików z poprzedniego badania"


@pytest.mark.asyncio
async def test_frozen_build_stores_images_next_to_exe(tmp_path, monkeypatch):
    """W .exe (PyInstaller onedir) obrazy lądują obok exe (app_dir), nie w _internal."""
    exe_dir = tmp_path / "VetFlowConnect"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "VetFlowConnect.exe"))
    monkeypatch.setattr(plugin_module, "app_dir", core_config.app_dir)
    assert core_config.app_dir() == exe_dir.resolve()

    api = _FakeApi()
    plugin = _plugin(api)
    await plugin._handle_message(_vm100("M1", "Burek", {"WBCHistogram": _jpeg_b64((200, 0, 0))}))

    [stored] = (exe_dir / "captured_images").glob("*/WBCHistogram_seq20.jpg")
    assert stored.resolve().is_relative_to(exe_dir.resolve() / "captured_images")
    assert not list(internal.rglob("*.jpg")), "nic nie trafia do _internal"
    assert [(lab_id, [name for name, _ in files]) for lab_id, files in api.images] == [(1, ["WBCHistogram_seq20.jpg"])]
