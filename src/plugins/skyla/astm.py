"""ASTM E1381/E1394 dla Skyli Solution (biochemia) — VETFL-967.

Solution wysyła wyniki wyłącznie przez ASTM (dokumentacja „skyla Solution — Data Communication Using
ASTM” rev. 1.4), a nie HL7 jak VM100 i Tutti. Analizator łączy się z Connectem po TCP (adres hosta
i port ustawia się w Solution) — na ten sam port co HL7; listener rozpoznaje protokół po pierwszym
bajcie (<ENQ> = ASTM, <VT> = MLLP/HL7).

Niski poziom (E1381): <ENQ> → <ACK>, ramki <STX>FN tekst <ETB|ETX> C1C2 <CR><LF> → <ACK> (zła suma
kontrolna → <NAK>, analizator powtarza ramkę), <EOT> kończy wiadomość. Tekst ramek sklejamy i dzielimy
na rekordy po <CR>: H (nagłówek), P (pacjent), O (zlecenie/panel), R (wynik), C (komentarz), L (koniec).

Do Vet-Flow idą markery; rekordy kontroli jakości analizatora (QC1…QC7, L340nm…, System_QC, „Sample QC”,
indeksy LIP/HEM/ICT) zostają tylko w surowym zapisie w captured_raw. Ten sam parser czyta ASTM z Tutti
(dokumentacja „Tutti ASTM v1.1”: kilka rekordów O w jednej wiadomości, flagi typu „>\\H”).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from .hl7_parser import HL7Message, HL7Patient, HL7Result

logger = logging.getLogger("vetflow_connect")

ENQ = b"\x05"
STX = 0x02
ETX = 0x03
ETB = 0x17
EOT = 0x04
ACK = b"\x06"
NAK = b"\x15"
LF = 0x0A

# Brak <ENQ>/ramki przez tyle sekund = analizator zamilkł w pół wiadomości — porzucamy niedokończoną
# wiadomość, ale połączenie zostaje (Solution trzyma je otwarte między badaniami).
FRAME_TIMEOUT_S = 30

AstmCallback = Callable[[str], Awaitable[None]]


def checksum(body: bytes) -> bytes:
    """Suma kontrolna ramki: suma bajtów od FN do <ETB>/<ETX> włącznie, mod 256, jako 2 znaki hex."""
    return f"{sum(body) % 256:02X}".encode("ascii")


def _decode(data: bytes) -> str:
    # Rozszerzone znaki wg IBM 850 (dodatek A dokumentacji).
    try:
        return data.decode("cp850")
    except UnicodeDecodeError:
        return data.decode("latin-1")


async def handle_astm_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    initial: bytes,
    callback: AstmCallback,
    device_name: str = "ASTM",
) -> None:
    """Obsługa połączenia ASTM od pierwszego <ENQ> (``initial`` = bajty już odczytane przez listener)."""
    buffer = bytearray(initial)
    frames: list[bytes] = []
    in_message = False

    while True:
        progressed = True
        while progressed:
            progressed = False
            if not buffer:
                break
            first = buffer[0]
            if first == ENQ[0]:
                del buffer[0]
                frames, in_message = [], True
                writer.write(ACK)
                await writer.drain()
                progressed = True
            elif first == EOT:
                del buffer[0]
                if in_message and frames:
                    text = _decode(b"".join(frames))
                    try:
                        await callback(text)
                    except Exception:
                        logger.exception("[%s] Error processing ASTM message", device_name)
                frames, in_message = [], False
                progressed = True
            elif first == STX:
                end = next((i for i in range(1, len(buffer)) if buffer[i] in (ETX, ETB)), None)
                # Ramka kompletna: <STX> ... <ETX|ETB> C1 C2 <CR> <LF>
                if end is None or len(buffer) < end + 5:
                    break
                body = bytes(buffer[1 : end + 1])  # FN … <ETB>/<ETX>
                received = bytes(buffer[end + 1 : end + 3]).upper()
                tail_end = end + 3
                if buffer[tail_end : tail_end + 1] == b"\r":
                    tail_end += 1
                if buffer[tail_end : tail_end + 1] == b"\n":
                    tail_end += 1
                del buffer[:tail_end]
                if received != checksum(body):
                    logger.warning("[%s] ASTM checksum mismatch (got %r, want %r) — NAK", device_name, received, checksum(body))
                    writer.write(NAK)
                else:
                    frames.append(body[1:-1])  # bez numeru ramki i znaku końca
                    writer.write(ACK)
                await writer.drain()
                progressed = True
            else:
                # Śmieci między ramkami (np. samotne <CR><LF>) — pomijamy.
                del buffer[0]
                progressed = True

        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=FRAME_TIMEOUT_S if in_message else None)
        except asyncio.TimeoutError:
            logger.warning("[%s] ASTM: no frame for %ds, dropping incomplete message", device_name, FRAME_TIMEOUT_S)
            frames, in_message = [], False
            continue
        if not data:
            return
        buffer.extend(data)


# --- E1394: rekordy ------------------------------------------------------------------------------

_ESCAPES = {"&F&": "|", "&S&": "^", "&R&": "\\", "&E&": "&"}


def _unescape(value: str) -> str:
    for seq, char in _ESCAPES.items():
        value = value.replace(seq, char)
    return value


def _field(fields: list[str], index: int) -> str:
    """Pole wg numeracji ASTM (x.y.1 = typ rekordu = index 1)."""
    return fields[index - 1] if 0 < index <= len(fields) else ""


def _reference_range(raw: str) -> str:
    """„70^90” → „70-90”, „min.70” → „>70”, „^90” → „<90” (format zakresu jak w Vet-Flow)."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.lower().startswith("min."):
        return f">{raw[4:].strip()}"
    low, _, high = raw.partition("^")
    low, high = low.strip(), high.strip()
    if low and high:
        return f"{low}-{high}"
    if high:
        return f"<{high}"
    return f">{low}" if low else ""


def _to_float(value: str) -> float | None:
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


# Rekordy kontroli jakości analizatora — nie są wynikiem pacjenta.
_QC = re.compile(r"^(QC\d*|L\d+nm|System_QC|Chemistry_QC|Sample QC|LIP#?|HEM#?|ICT#?)$", re.IGNORECASE)


def _flag_and_value(raw_value: str, abnormal: str, ref_raw: str) -> tuple[str, str]:
    """Flaga H/L dla Vet-Flow i wartość do pokazania.

    Pole 10.1.7 (może mieć kilka flag rozdzielonych „\\”): L/H — poza normą, „<”/„>” — poza zakresem
    pomiarowym (wartość to granica zakresu, więc pokazujemy „<0.4” / „>1000”), N — w normie, A — „N.A.”.
    Dopisek przy wartości: „^H”/„^L” — poza normą, „^U”/„^D” — poza normą i zakłócony (LIP/HEM/ICT).
    """
    value, _, suffix = raw_value.partition("^")
    value, suffix = value.strip(), suffix.strip().upper()
    flags = {f.strip() for f in abnormal.split("\\") if f.strip()}
    flag = "H" if "H" in flags or suffix in ("H", "U") else ("L" if "L" in flags or suffix in ("L", "D") else "")
    for limit in ("<", ">"):
        if limit in flags and not value.startswith(limit):
            value = f"{limit}{value}"
    if not flag and value[:1] in ("<", ">"):
        number = _to_float(value[1:])
        low, _, high = ref_raw.partition("^")
        low_f, high_f = _to_float(low.replace("min.", "").strip() or "x"), _to_float(high.strip() or "x")
        if number is not None:
            if value[0] == ">" and high_f is not None and number >= high_f:
                flag = "H"
            elif value[0] == "<" and low_f is not None and number <= low_f:
                flag = "L"
    return flag, value


def parse_astm(text: str) -> HL7Message:
    """Wiadomość ASTM z Solution → ten sam obiekt co z HL7 (wtyczka wysyła go do Vet-Flow tak samo)."""
    message = HL7Message()
    message.hl7_version = "ASTM E1394"
    panels: list[str] = []
    for record in re.split(r"\r\n|\r|\n", text):
        record = record.strip("\x00")
        if not record:
            continue
        fields = record.split("|")
        kind = fields[0][-1:] if fields[0] else ""
        if kind == "H":
            sender = _field(fields, 5).split("^")
            name = sender[0].strip()
            low = name.lower()
            message.device = "Solution" if "solution" in low else ("Tutti" if "tutti" in low else name or "Skyla")
            message.device_type = "chemistry"
            message.timestamp = _field(fields, 14)
            message.message_id = _field(fields, 14)
            if _field(fields, 12).upper() == "Q":
                message.device_type = "qc"
        elif kind == "P":
            species = _field(fields, 16).split("^")
            age = _field(fields, 8).split("^")  # „^5^Year”, w Tutti też „^2^Year^6^Month”
            message.patient = HL7Patient(
                patient_id=_unescape(_field(fields, 4)),
                name=_unescape(_field(fields, 6)),
                species=_unescape(species[0]) if species else "",
                sex=_field(fields, 9),
                age=" ".join(part for part in age[1:] if part).strip(),
            )
        elif kind == "O":
            # Panel to pierwsze pole „^^^Nazwa” (Solution: 9.4.5, Tutti przesuwa je o jedno pole);
            # Tutti potrafi wysłać kilka paneli (kilka rekordów O) w jednej wiadomości.
            panel_field = next((f for f in fields[1:] if f.startswith("^^^")), "")
            parts = panel_field.split("^")
            panel = _unescape(parts[3].strip()) if len(parts) > 3 else ""
            if panel and panel not in panels:
                panels.append(panel)
            message.panel_name = ", ".join(panels)
            if "Q" in (_field(fields, 12).upper(), _field(fields, 13).upper()):
                message.device_type = "qc"
        elif kind == "R":
            test_id = _field(fields, 3).split("^")
            marker = _unescape(test_id[3].strip()) if len(test_id) > 3 else ""
            if not marker or _QC.match(marker):
                continue  # QC analizatora, indeksy LIP/HEM/ICT — tylko w surowym zapisie
            location = test_id[4].strip() if len(test_id) > 4 else ""
            raw_value = _field(fields, 4)
            if not raw_value.split("^")[0].strip():
                continue  # wyliczany marker bez wartości (N.A.)
            ref_raw = _field(fields, 6)
            flag, shown = _flag_and_value(raw_value, _field(fields, 7), ref_raw)
            name = marker.lstrip("#")
            if location:
                name = f"{name} {location}"
            try:
                sequence = int(_field(fields, 2))
            except ValueError:
                sequence = 0
            message.results.append(
                HL7Result(
                    sequence=sequence,
                    value_type="NM",
                    abbreviation=name,
                    name=name,
                    value=shown,
                    unit=_field(fields, 5).strip(),
                    reference_range=_reference_range(ref_raw),
                    flag=flag,
                )
            )
            if not message.timestamp:
                message.timestamp = _field(fields, 13)

    logger.info(
        "Parsed ASTM %s: patient=%s, panel=%s, %d results",
        message.device,
        message.patient.name,
        message.panel_name,
        len(message.results),
    )
    return message
