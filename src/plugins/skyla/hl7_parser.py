"""HL7 ORU^R01 parser for Skyla VM100 (CBC), SH50 (CBC) and Tutti (Chemistry).

SH50 (VETFL-967, dokumentacja „SH50 HL7 External communication protocol”): MSH-3 „SKYLA”, MSH-4
„skyla SH50”; PID-2 numer pacjenta, PID-5 imię; histogramy i wykresy jako OBX typu ED
„skyla SH50^Image^BMP^Base64^<dane>”.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("vetflow_connect")


@dataclass
class HL7Result:
    sequence: int
    value_type: str
    abbreviation: str
    name: str
    value: str
    unit: str
    reference_range: str
    flag: str


@dataclass
class HL7Patient:
    patient_id: str = ""
    name: str = ""
    species: str = ""
    sex: str = ""
    birthday: str = ""
    age: str = ""


@dataclass
class HL7Message:
    device: str = ""
    device_type: str = ""
    hl7_version: str = ""
    message_id: str = ""
    timestamp: str = ""
    patient: HL7Patient = field(default_factory=HL7Patient)
    panel_name: str = ""
    results: list[HL7Result] = field(default_factory=list)
    # VETFL-975: obrazy (histogramy, wykresy) z OBX typu ED — (nazwa pliku, bajty JPEG) TEJ wiadomości.
    # Parser nie zapisuje ich do wspólnego katalogu: wtyczka wysyła je tylko do wyniku tego pacjenta.
    images: list[tuple[str, bytes]] = field(default_factory=list)


def _split_field(field_value: str, separator: str = "^") -> list[str]:
    return field_value.split(separator)


def _get_field(fields: list[str], index: int) -> str:
    if index < len(fields):
        return fields[index]
    return ""


def _parse_msh(fields: list[str]) -> dict:
    sending_app = _get_field(fields, 2)
    sending_facility = _get_field(fields, 3)
    timestamp = _get_field(fields, 6)
    message_id = _get_field(fields, 9)
    version = _get_field(fields, 11)

    sending_app_lower = sending_app.lower()
    # SH50 przedstawia się jako „SKYLA” z urządzeniem w MSH-4 — bez tego wpadał pod „skyla” = Tutti.
    if "sh50" in f"{sending_app} {sending_facility}".lower():
        device = "SH50"
        device_type = "cbc"
    elif "vm100" in sending_app_lower:
        device = "VM100"
        device_type = "cbc"
    elif "tutti" in sending_app_lower or "skyla" in sending_app_lower:
        device = "Tutti"
        device_type = "chemistry"
    else:
        device = sending_app
        device_type = "unknown"

    return {
        "device": device,
        "device_type": device_type,
        "hl7_version": version,
        "message_id": message_id,
        "timestamp": timestamp,
    }


def _parse_pid_vm100(fields: list[str]) -> HL7Patient:
    return HL7Patient(
        name=_get_field(fields, 9),
        species=_get_field(fields, 10),
        sex=_get_field(fields, 8),
        birthday=_get_field(fields, 7),
    )


def _parse_pid_tutti(fields: list[str]) -> HL7Patient:
    age_parts = _split_field(_get_field(fields, 7))
    age = f"{age_parts[1]} {age_parts[2]}" if len(age_parts) >= 3 else ""
    return HL7Patient(
        patient_id=_get_field(fields, 3),
        name=_get_field(fields, 5),
        sex=_get_field(fields, 8),
        age=age.strip(),
    )


def _parse_pid_sh50(fields: list[str]) -> HL7Patient:
    return HL7Patient(
        patient_id=_get_field(fields, 2),
        name=_get_field(fields, 5),
        sex=_get_field(fields, 8),
        birthday=_get_field(fields, 7),
    )


def _parse_pv1(fields: list[str]) -> str:
    return _get_field(fields, 5)


def _parse_obr(fields: list[str]) -> str:
    panel_field = _get_field(fields, 4)
    parts = _split_field(panel_field)
    return parts[1] if len(parts) > 1 else parts[0]


def _parse_obx(fields: list[str]) -> HL7Result | None:
    sequence = _get_field(fields, 1)
    value_type = _get_field(fields, 2)

    if value_type == "ED":
        return None

    if value_type == "TX":
        logger.debug("Skipping OBX type TX (seq %s)", sequence)
        return None

    param_parts = _split_field(_get_field(fields, 3))
    abbreviation = param_parts[0] if param_parts else ""
    name = param_parts[1] if len(param_parts) > 1 else abbreviation

    try:
        seq_num = int(sequence)
    except ValueError:
        seq_num = 0

    return HL7Result(
        sequence=seq_num,
        value_type=value_type,
        abbreviation=abbreviation,
        name=name,
        value=_get_field(fields, 5),
        unit=_get_field(fields, 6),
        reference_range=_get_field(fields, 7),
        flag=_get_field(fields, 8),
    )


def _decode_ed_image(fields: list[str]) -> tuple[str, bytes] | None:
    """Obraz z OBX typu ED jako (nazwa pliku .jpg, bajty JPEG); None, gdy segment nie niesie obrazu."""
    sequence = _get_field(fields, 1)
    # SH50 w przykładzie producenta potrafi wstawić dane o pole wcześniej (OBX-4 zamiast OBX-5).
    raw_data = _get_field(fields, 5) or _get_field(fields, 4)
    if not raw_data:
        return None
    try:
        import base64
        import binascii

        # Dane obrazu to ostatni składnik („^dane”, „skyla SH50^Image^BMP^Base64^dane”) albo — bez nagłówka
        # HL7 ED — samo Base64 (uwaga CodeAnt #14). Bierzemy tylko to, co po zdekodowaniu jest obrazem.
        b64_data = raw_data.rsplit("^", 1)[-1].strip()
        image_bytes = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError):
        logger.debug("OBX ED (seq %s) bez danych obrazu — zostaje tylko w surowej wiadomości", sequence)
        return None
    try:
        if image_bytes[:2] == b"BM" or image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            image_bytes = _bmp_to_jpeg(image_bytes)
        elif image_bytes[:3] != b"\xff\xd8\xff":
            logger.debug("OBX ED (seq %s): dane nie są obrazem — pominięte", sequence)
            return None
    except Exception as exc:
        logger.warning("Could not decode ED segment %s: %s", sequence, exc)
        return None
    param_parts = _split_field(_get_field(fields, 3))
    image_type = _safe_name(param_parts[0] if param_parts else "") or "img"
    return f"{image_type}_seq{_safe_name(sequence) or '0'}.jpg", image_bytes


def _safe_name(value: str) -> str:
    """Nazwa z pola urządzenia jako bezpieczny fragment nazwy pliku (bez separatorów ścieżek)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip()).strip("._")[:60]


def _bmp_to_jpeg(bmp_bytes: bytes) -> bytes:
    """SH50 wysyła histogramy jako BMP (bywa też PNG); do Vet-Flow idą jako JPEG jak obrazy z VM100."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(bmp_bytes)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=90)
        return out.getvalue()


def parse_hl7(message: str) -> HL7Message:
    result = HL7Message()
    segments = re.split(r"\r\n|\r|\n", message.strip())
    device_detected = False

    for segment in segments:
        if not segment.strip():
            continue
        fields = segment.split("|")

        if segment.startswith("MSH"):
            msh = _parse_msh(fields)
            result.device = msh["device"]
            result.device_type = msh["device_type"]
            result.hl7_version = msh["hl7_version"]
            result.message_id = msh["message_id"]
            result.timestamp = msh["timestamp"]
            device_detected = True
        elif segment.startswith("PID"):
            if result.device == "Tutti":
                result.patient = _parse_pid_tutti(fields)
            elif result.device == "SH50":
                result.patient = _parse_pid_sh50(fields)
            else:
                result.patient = _parse_pid_vm100(fields)
        elif segment.startswith("PV1"):
            patient_id = _parse_pv1(fields)
            if patient_id and not result.patient.patient_id:
                result.patient.patient_id = patient_id
        elif segment.startswith("OBR"):
            # SH50: OBR-4 to opis urządzenia („AUTO ANALIZADOR HEMATOLÓGICO 5 DIF skyla SH50”), nie panel.
            result.panel_name = "CBC" if result.device == "SH50" else _parse_obr(fields)
        elif segment.startswith("OBX"):
            if _get_field(fields, 2) == "ED":
                image = _decode_ed_image(fields)
                if image is not None:
                    result.images.append(image)
                continue
            obx = _parse_obx(fields)
            if obx is not None:
                result.results.append(obx)

    if not device_detected:
        logger.warning("No MSH segment found in message")

    logger.info(
        "Parsed %s %s: patient=%s, panel=%s, %d results, %d images",
        result.device,
        result.device_type,
        result.patient.name,
        result.panel_name,
        len(result.results),
        len(result.images),
    )
    return result


def build_ack(message: str) -> str:
    segments = re.split(r"\r\n|\r|\n", message.strip())
    msh_line = next((segment for segment in segments if segment.startswith("MSH")), "")
    if not msh_line:
        return ""

    fields = msh_line.split("|")
    sending_app = _get_field(fields, 2)
    sending_facility = _get_field(fields, 3)
    receiving_app = _get_field(fields, 4)
    receiving_facility = _get_field(fields, 5)
    message_id = _get_field(fields, 9)
    version = _get_field(fields, 11)
    now = datetime.now().strftime("%Y%m%d%H%M%S")

    ack_msh = (
        f"MSH|^~\\&|{receiving_app or 'VetFlow'}|{receiving_facility}|"
        f"{sending_app}|{sending_facility}|{now}||ACK^R01|{now}|P|{version}"
    )
    ack_msa = f"MSA|AA|{message_id}"
    return f"{ack_msh}\r{ack_msa}\r"
