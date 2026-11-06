"""VETFL-967: Skyla SH50 (hematologia) przez HL7 — przykład z dokumentacji producenta.

„SH50 HL7 External communication protocol”, A.4.1: MSH-3 „SKYLA”, MSH-4 „skyla SH50”. Wcześniej
parser brał każde „skyla” za Tutti (biochemia) i czytał pacjenta z pól Tutti.
"""

from __future__ import annotations

import base64
import io

from src.plugins.skyla.hl7_parser import parse_hl7


def _bmp_b64() -> str:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 30, 30)).save(out, format="BMP")
    return base64.b64encode(out.getvalue()).decode()


def _message(bmp: str = "Qk0=") -> str:
    return "\r".join([
        r"MSH|^~\&|SKYLA|skyla SH50|LIS|PC|20110627144458||ORU^R01|0001|P|2.3.1||||||UNICODE",
        "PID|1|1010051|A1123145|15|Mary||19811011|M",
        "PV1|1|||",
        "OBR|1||BAR101010101|SKYLA^AUTO ANALIZADOR HEMATOLÓGICO 5 DIF skyla SH50||||01110621143134|||||^||||||||||||||||",
        "OBX|1|NM|WBC||110.0|10^9/L|40.0-100.0|H|||F|||||||",
        "OBX|2|NM|LYM||35.57|%|20.00-40.00||||F|||||||",
        "OBX|14|NM|HCT||26.4|%|37.0-50.0|L|||F|||||||",
        f"OBX|25|ED|RBCHistogram||skyla SH50^Image^BMP^Base64^{bmp}",
    ]) + "\r"


def test_sh50_recognised_as_cbc_not_tutti():
    m = parse_hl7(_message())
    assert (m.device, m.device_type, m.panel_name) == ("SH50", "cbc", "CBC")
    assert (m.patient.patient_id, m.patient.name, m.patient.sex) == ("1010051", "Mary", "M")
    r = {x.name: x for x in m.results}
    assert set(r) == {"WBC", "LYM", "HCT"}, "histogram (ED) nie jest parametrem"
    assert (r["WBC"].value, r["WBC"].unit, r["WBC"].reference_range, r["WBC"].flag) == ("110.0", "10^9/L", "40.0-100.0", "H")
    assert r["HCT"].flag == "L"


def test_sh50_bmp_histogram_kept_on_message_as_jpeg():
    m = parse_hl7(_message(_bmp_b64()))
    [(name, data)] = m.images
    assert name == "RBCHistogram_seq25.jpg"
    assert data[:3] == b"\xff\xd8\xff", "BMP przekonwertowany na JPEG"


def test_tutti_and_vm100_unchanged():
    from .mock_messages import TUTTI_CHEMISTRY, VM100_CBC

    assert parse_hl7(TUTTI_CHEMISTRY).device == "Tutti"
    assert parse_hl7(VM100_CBC).device == "VM100"


def test_sh50_real_device_name_in_pv1_before_pid():
    """Bandikot 6.11: prawdziwy SH50 wysyła „Pat.Name” w PV1-3 (PV1 przed PID), PID-5 pusty —
    wynik przychodził bez pacjenta i nie przypisywał się do wizyty. MSH-10 stały („4”), próbka w OBR-3."""
    real = "\r".join([
        r"MSH|^~\&|SKYLA|SH50|LIS|PC|20261106212400||ORU^R01|4|P|2.3.1||||||UNICODE",
        "PV1|1||cynamon",
        "PID|1||||||18|M",
        "OBR|1||0000002|SKYLA^SH50|||20261106190100||||||||1|||||2|",
        "OBX|1|NM|WBC||11.28|10^9/L|2.87-17.02||||F||||cynamon|administrator|",
    ]) + "\r"
    m = parse_hl7(real)
    assert (m.device, m.panel_name) == ("SH50", "CBC")
    assert m.patient.name == "cynamon"
    assert m.patient.sex == "M"
    assert m.message_id == "0000002", "numer próbki, nie stałe MSH-10"
    assert [(r.name, r.value) for r in m.results] == [("WBC", "11.28")]


def test_sh50_pid_name_still_wins_over_pv1():
    m = parse_hl7(_message().replace("PV1|1|||", "PV1|1||inne"))
    assert m.patient.name == "Mary"
