"""VETFL-967: ASTM ze Skyli Solution (i Tutti) — przykłady wprost z dokumentacji producenta.

„skyla Solution — Data Communication Using ASTM” rev. 1.4 (tabele 8–11, 13) i „Tutti ASTM v1.1”.
Sprawdza: wymianę ENQ/ACK/ramki/EOT po prawdziwym TCP, NAK przy złej sumie kontrolnej, że HL7 na tym
samym porcie działa jak dotąd, i co z wiadomości trafia do Vet-Flow (markery, normy, flagi H/L).
"""

from __future__ import annotations

import asyncio

import pytest

from src.plugins.skyla.astm import ACK, ENQ, EOT, NAK, checksum, parse_astm
from src.plugins.skyla.hl7_listener import CR, EB, SB, HL7Listener
from src.plugins.skyla.plugin import SkylaPlugin

from .mock_messages import TUTTI_CHEMISTRY

NORMAL = "\r".join([
    r"H|\^&|||skyla Solution^DS06|||||||P|1394-97|20151014183758",
    "P|1||123||Name||^5^Year|Male|||||||Feline||5.1^Kg||||||||||||||||HOSPITAL-NAME",
    "O|1|||^^^Thyroid/T4 Panel|||||||N||||Patient||||||||^01210927000DS06AES25DISCA+||F",
    "R|1|^^^QC1|103||min.90|N||||||20151008144113",
    "R|7|^^^QC7|101||90^110|N||||||20151008144113",
    "R|8|^^^L340nm|103||90^110|N||||||20151008144113",
    "R|16|^^^System_QC|100||min.90|N||||||20151008144113",
    "R|17|^^^Chemistry_QC|101||min.90|N||||||20151008144113",
    "R|18|^^^QC|OK|||N||||||20151008144113",
    "R|19|^^^Sample QC|OK|||N||||||20151008144113",
    "R|20|^^^LIP|0|||N||||||20151008144113",
    "R|21|^^^LIP#|0|||N||||||20151008144113",
    "R|26|^^^ALB^^^^M|3.0|g/dL|2.6^4.1|N||F||||20151008144113",
    "R|27|^^^ALP^^^^M|20|U/L|0^30|N||F||||20151008144113",
    "R|28|^^^ALT^^^^M|30|U/L|0^60|N||F||||20151008144113",
    "R|29|^^^AST^^^^M|10|U/L|0^20|N||F||||20151008144113",
    "R|30|^^^GGT^^^^M|6|U/L|0^12|N||F||||20151008144113",
    "R|31|^^^#GLOB^^^^C|3.1| g/dL|2.5^4.5|N||F||||20151008144113",
    "L|1|N",
]) + "\r"

ABNORMAL = "\r".join([
    r"H|\^&|||skyla Solution^DS06|||||||P|1394-97|20151014183758",
    "P|1||123||Name||^5^Year|Male|||||||Feline||5.1^Kg||||||||||||||||HOSPITAL-NAME",
    "O|1|||^^^Thyroid/T4 Panel|||||||N||||Patient||||||||^01210927000DK06AES25DISCA+||F",
    "R|19|^^^Sample QC|Interference|||N||||||20151014172503",
    "R|26|^^^ALP^^^^M|40^D|U/L|100^212|<||F||||20151014172503",
    "R|27|^^^AST^^^^M|1000^U|U/L|0^50|>||F||||20151014172503",
    "R|28|^^^ALB^^^^M|4.5^H|g/dL|2.6^4.1|H||F||||20151014172503",
    "R|29|^^^BUN^^^^M|5.5^L|mg/dL|6.0^26.0|L||F||||20151014172503",
    "R|30|^^^Mg^^^^M|3.0^%|mmol/L|0.4^3.3|A||F||||20151014172503",
    "R|31|^^^TBIL^^^^M|0.4|mg/dL|0^0.9|<||F||||20151014172503",
    "R|32|^^^BA^^^^M|20^H|umol/L|0^10|>||F||||20130308144113",
    "R|33|^^^T4^^^^M|N.A.|g/dL|0.8^4.7|A||F||||20151014172503",
    "R|34|^^^#GLOB^^^^C||g/dL|2.5^4.5|N||F||||20151014172503",
    "L|1|N",
]) + "\r"

SINGLE_ASSAY = "\r".join([
    r"H|\^&|||skyla Solution^DS06|||||||P|1394-97|20140121125733",
    "P|1||12345||||^5^Year||||||||Canine||||||||||||||||||HOSPITAL-NAME",
    r"O|1|||^^^Single Assay|||||||N||||Patient||||||||^Loc-1:20721\Loc-2:20722\Loc-3: 20723||F",
    "R|1|^^^QC^Loc-1|OK|||N||||||20140121125400",
    "R|4|^^^LIP^Loc-1|0|||N||||||20140121125400",
    "R|22|^^^TC^Loc-1^^^M|200|mg/dL|110^320|N||F||||20140121125400",
    "R|24|^^^BUN^Loc-2^^^M|13|mg/dL|6.0^26|N||F||||20140121125400",
    "R|25|^^^TC^Loc-3^^^M|240|mg/dL| 110^320|N||F||||20140121125400",
    "L|1|N",
]) + "\r"

CORTISOL = "\r".join([
    r"H|\^&|||skyla Solution^DS08|||||||P|1394-97|20210915145443",
    "P|1||150920211435||||^5^Year||||||||Canine",
    "O|1|||^^^cCOR|||||||N||||Patient||||||||^060325131123456DS08||F",
    "R|26|^^^cCOR^^^^M|828^H|nmol/L|55.2^166|>||F||||20210915145443",
    "C|1|I|Pre-ACTH (Baseline)|G",
    "L|1|N",
]) + "\r"

CONNECT = "\r".join([
    r"H|\^&|||skyla Solution^DS09|||||||P|1394-97|20160503160254",
    "C|1|I|L15112400149^Connect|G",
    "L|1|N",
]) + "\r"

TUTTI = "\r".join([
    r"H|\^&|||skyla Tutti^ES01.01.01|||||||P|1394-97|20260603110500",
    "P|1||A8899||Luna||^2^Year^6^Month|F|||||||Feline^Siamese||5.1^Kg||||||||||||||||HOSPITAL-NAME",
    "O|1||WL26032601|20260327091530|^^^General Chem 26",
    "R|1|^^^GLU^^^^M|5.2|mmol/L|3.9^6.1|N||F||||20260327091530",
    r"R|2|^^^TRIG^^^^M|>1000|mg/dL|10^100|>\H||F||||20260603110433",
    "R|20|^^^LIP|0|||N||F||||20260327091530",
    "O|2||WL26032602|20260327091530|^^^Electrolyte 7",
    "R|3|^^^Na^^^^M|142|mmol/L|136^145|N||F||||20260327091530",
    "L|1|N",
]) + "\r"


def _frames(message: str, size: int = 240) -> list[bytes]:
    """Podział na ramki E1381 jak w Solution (≤240 znaków tekstu, FN 1..7,0,1…)."""
    data = message.encode("cp850")
    chunks = [data[i : i + size] for i in range(0, len(data), size)]
    frames = []
    for n, chunk in enumerate(chunks, start=1):
        end = b"\x03" if n == len(chunks) else b"\x17"
        body = str(n % 8).encode() + chunk + end
        frames.append(b"\x02" + body + checksum(body) + b"\r\n")
    return frames


def _by_name(message):
    return {r.name: r for r in message.results}


# --- parser -----------------------------------------------------------------------------------


def test_normal_example_only_markers_with_ranges():
    m = parse_astm(NORMAL)
    assert m.device == "Solution" and m.device_type == "chemistry"
    assert m.panel_name == "Thyroid/T4 Panel"
    assert (m.patient.patient_id, m.patient.name, m.patient.species, m.patient.sex, m.patient.age) == (
        "123", "Name", "Feline", "Male", "5 Year")
    assert m.timestamp == "20151014183758"
    assert [r.name for r in m.results] == ["ALB", "ALP", "ALT", "AST", "GGT", "GLOB"], "QC analizatora pominięte"
    alb = _by_name(m)["ALB"]
    assert (alb.value, alb.unit, alb.reference_range, alb.flag) == ("3.0", "g/dL", "2.6-4.1", "")
    assert _by_name(m)["GLOB"].unit == "g/dL", "spacja przed jednostką z przykładu producenta"


def test_abnormal_example_flags_and_limits():
    r = _by_name(parse_astm(ABNORMAL))
    assert (r["ALP"].value, r["ALP"].flag) == ("<40", "L"), "poniżej zakresu pomiarowego + zakłócenie (D)"
    assert (r["AST"].value, r["AST"].flag) == (">1000", "H"), "powyżej zakresu pomiarowego + zakłócenie (U)"
    assert (r["ALB"].value, r["ALB"].flag) == ("4.5", "H")
    assert (r["BUN"].value, r["BUN"].flag) == ("5.5", "L")
    assert (r["Mg"].value, r["Mg"].flag) == ("3.0", ""), "tylko zakłócenie (%), w normie"
    assert (r["TBIL"].value, r["TBIL"].flag) == ("<0.4", ""), "poniżej zakresu pomiarowego, ale nie poniżej normy 0–0,9"
    assert (r["BA"].value, r["BA"].flag) == (">20", "H")
    assert (r["T4"].value, r["T4"].flag) == ("N.A.", "")
    assert "GLOB" not in r, "wyliczany marker bez wartości pominięty"
    assert "Sample QC" not in r


def test_single_assay_keeps_both_locations():
    r = _by_name(parse_astm(SINGLE_ASSAY))
    assert set(r) == {"TC Loc-1", "BUN Loc-2", "TC Loc-3"}, "ten sam marker z dwóch miejsc nie nadpisuje się"
    assert r["TC Loc-3"].reference_range == "110-320"


def test_cortisol_and_one_sided_ranges():
    m = parse_astm(CORTISOL)
    assert (m.panel_name, m.results[0].name, m.results[0].value, m.results[0].flag) == ("cCOR", "cCOR", ">828", "H")
    assert parse_astm("R|1|^^^X^^^^M|5|u|min.70|L||F").results[0].reference_range == ">70"
    assert parse_astm("R|1|^^^X^^^^M|5|u|^90|N||F").results[0].reference_range == "<90"


def test_tutti_astm_two_panels_and_double_flag():
    m = parse_astm(TUTTI)
    assert m.device == "Tutti"
    assert m.panel_name == "General Chem 26, Electrolyte 7"
    assert m.patient.age == "2 Year 6 Month" and m.patient.species == "Feline"
    r = _by_name(m)
    assert set(r) == {"GLU", "TRIG", "Na"}
    assert (r["TRIG"].value, r["TRIG"].flag) == (">1000", "H"), "flagi „>\\H” — bez podwójnego „>>”"


def test_quality_control_message_marked():
    qc = NORMAL.replace("|P|1394-97|", "|Q|1394-97|")
    assert parse_astm(qc).device_type == "qc"


# --- transmisja po TCP ------------------------------------------------------------------------


async def _serve(received: list[str], hl7: list[str] | None = None):
    async def on_astm(text: str) -> None:
        received.append(text)

    async def on_hl7(text: str) -> None:
        (hl7 if hl7 is not None else []).append(text)

    listener = HL7Listener(device_name="Test")
    server = await listener.start("127.0.0.1", 0, on_hl7, astm_callback=on_astm)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _expect(reader, byte: bytes) -> None:
    got = await asyncio.wait_for(reader.readexactly(1), 2)
    assert got == byte, (got, byte)


@pytest.mark.asyncio
async def test_astm_session_over_tcp_multiframe():
    received: list[str] = []
    server, port = await _serve(received)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        frames = _frames(NORMAL)
        assert len(frames) > 1, "przykład dzieli się na kilka ramek"
        writer.write(ENQ)
        await _expect(reader, ACK)
        for frame in frames:
            writer.write(frame)
            await _expect(reader, ACK)
        writer.write(bytes([EOT]))
        await writer.drain()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received == [NORMAL], "tekst ramek sklejony bez numerów ramek i sum kontrolnych"

        # Druga wiadomość na tym samym połączeniu (Solution trzyma połączenie między badaniami).
        writer.write(ENQ)
        await _expect(reader, ACK)
        for frame in _frames(CORTISOL):
            writer.write(frame)
            await _expect(reader, ACK)
        writer.write(bytes([EOT]))
        await writer.drain()
        for _ in range(50):
            if len(received) == 2:
                break
            await asyncio.sleep(0.02)
        assert received[1] == CORTISOL
    finally:
        if "writer" in locals():
            writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


@pytest.mark.asyncio
async def test_bad_checksum_gets_nak_and_retransmission_is_accepted():
    received: list[str] = []
    server, port = await _serve(received)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        [frame] = _frames(CONNECT)
        broken = frame[:-4] + (b"00" if frame[-4:-2] != b"00" else b"11") + b"\r\n"
        writer.write(ENQ)
        await _expect(reader, ACK)
        writer.write(broken)
        await _expect(reader, NAK)
        writer.write(frame)
        await _expect(reader, ACK)
        writer.write(bytes([EOT]))
        await writer.drain()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received == [CONNECT], "uszkodzona ramka nie weszła do wiadomości"
    finally:
        if "writer" in locals():
            writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


@pytest.mark.asyncio
async def test_hl7_on_the_same_port_still_works():
    received: list[str] = []
    hl7: list[str] = []
    server, port = await _serve(received, hl7)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(SB + TUTTI_CHEMISTRY.encode() + EB + CR)
        await writer.drain()
        ack = await asyncio.wait_for(reader.readuntil(EB + CR), 2)
        assert b"MSA|AA" in ack
        for _ in range(50):
            if hl7:
                break
            await asyncio.sleep(0.02)
        assert hl7 == [TUTTI_CHEMISTRY] and received == []
    finally:
        if "writer" in locals():
            writer.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


# --- wtyczka: co idzie do Vet-Flow ------------------------------------------------------------


class _FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_lab_result(self, panel: dict):
        self.sent.append(panel)
        return 1

    async def send_images(self, *_args):
        return True


@pytest.mark.asyncio
async def test_plugin_sends_results_and_skips_status_and_qc(tmp_path, monkeypatch):
    import src.plugins.skyla.plugin as plugin_module

    monkeypatch.setattr(plugin_module, "app_dir", lambda: tmp_path)
    plugin = SkylaPlugin()
    api = _FakeApi()
    plugin.configure(api_client=api)
    plugin._device_config = {"name": "Skyla Solution"}

    await plugin._handle_astm_message(CONNECT)
    await plugin._handle_astm_message(NORMAL.replace("|P|1394-97|", "|Q|1394-97|"))
    assert api.sent == [], "status połączenia i kontrola jakości nie idą jako wynik"

    await plugin._handle_astm_message(ABNORMAL)
    [panel] = api.sent
    assert panel["lab_name"] == "Solution" and panel["sample_type"] == "Thyroid/T4 Panel"
    assert panel["patient_name"] == "Name" and panel["patient_id"] == "123"
    assert panel["results_json"]["ALB"] == {"value": "4.5", "unit": "g/dL", "reference_range": "2.6-4.1", "flag": "H"}
    assert list((tmp_path / "captured_raw").glob("astm_*.txt")), "surowa wiadomość zapisana do diagnostyki"


@pytest.mark.asyncio
async def test_plugin_stop_does_not_wait_for_connected_analyzer(tmp_path, monkeypatch):
    """Solution trzyma połączenie otwarte — zatrzymanie wtyczki (samoaktualizacja) nie może na nie czekać."""
    plugin = SkylaPlugin()
    await plugin.start({"name": "Skyla", "port": 0})
    port = plugin._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(ENQ)
    await _expect(reader, ACK)
    try:
        await asyncio.wait_for(plugin.stop(), 3)
    finally:
        writer.close()
