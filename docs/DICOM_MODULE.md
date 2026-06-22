# VetFlowConnect — moduł DICOM (Storage SCP)

Status: **PROPOZYCJA** (2026-06-20). Dodaje obsługę obrazówki (RTG/USG/CT) do istniejącego agenta HL7 jako drugi protokół-moduł. Walidator wymagań: lead Dominik (klinika z DICOM/Examion, słaby internet, multi-lab).

## 1. Cel

Agent staje się **węzłem DICOM Storage SCP** w sieci kliniki: modality (soft detektora RTG — Examion/Sound/Cuattro/generic CR) konfiguruje VetFlowConnect jako *DICOM destination*. Obrazy lecą automatycznie `C-STORE` → agent parsuje nagłówek → dopasowuje pacjenta → wysyła do VetFlow (derywat JPEG/WebP na kartę + oryginał `.dcm` do R2). Zero ręcznego eksportu/doklejania.

Jeden agent rozwiązuje **DICOM + multi-lab + słaby internet** (offline buffer) — patrn już sprawdzony na HL7 (Skyla na prodzie u Górskiej).

## 2. Mapowanie na istniejącą architekturę

| HL7 (jest) | DICOM (dodać) |
|---|---|
| `hl7_listener.py` — MLLP TCP server (asyncio) | `dicom_listener.py` — pynetdicom Storage SCP (własny thread-server) |
| `hl7_parser.py` → `HL7Message` | `dicom_parser.py` → `DicomStudy` (pydicom) |
| `xml_builder.py` / payload w `agent.py` | mapowanie nagłówka → payload imaging |
| `VetFlowClient.send_result_json` + `send_images` | `VetFlowClient.send_imaging_study` (nowy) |
| `DeviceConfig{type: cbc/chemistry, port}` | `DicomConfig{ae_title, port, ...}` (jeden węzeł, nie per-port device) |
| `auto_discover.py` (skan sieci) | n/d — przy DICOM to MODALITY celuje w nas; konfiguracja statyczna (AE+IP+port) |

Konwencje zachowane: logger `vetflow_connect`, `try/except ImportError` dual-import (frozen vs dev), zapis surowca do `captured_*`, tray notyfikacje, `_exe_dir()` dla ścieżek.

## 3. Nowe pliki

```
src/dicom_listener.py     # AE + EVT_C_STORE handler → spool job
src/dicom_parser.py       # pydicom: nagłówek → DicomStudy + render JPEG
src/spool.py              # dyskowa kolejka offline (współdzielona z HL7 — patrz §6)
tests/test_dicom_parser.py
tests/sample_data/*.dcm   # próbki (pydicom test files / anonimizowane RTG)
```

Zmiany w istniejących: `config.py` (+`DicomConfig`), `vetflow_client.py` (+`send_imaging_study`), `agent.py` (wiring + bridge thread→asyncio), `requirements`/`.spec` (deps + hidden imports), `config.json.example`, `README.md`.

## 4. dicom_listener.py — Storage SCP

pynetdicom server jest **synchroniczny i thread-based** (nie asyncio). Wzorzec:

```python
from pynetdicom import AE, evt, AllStoragePresentationContexts
from pynetdicom.sop_class import Verification  # C-ECHO

class DicomListener:
    def __init__(self, ae_title: str, on_study, loop):
        self.ae = AE(ae_title=ae_title.encode())
        self.ae.supported_contexts = AllStoragePresentationContexts  # CR/DX/US/CT/SC...
        self.ae.add_supported_context(Verification)                  # C-ECHO ping
        self._on_study = on_study   # async callable(DicomStudy)
        self._loop = loop           # agent background event loop

    def _handle_store(self, event) -> int:
        ds = event.dataset
        ds.file_meta = event.file_meta
        study = parse_dicom(ds)                    # dicom_parser
        # bridge sync SCP thread → agent asyncio loop
        asyncio.run_coroutine_threadsafe(self._on_study(study), self._loop)
        return 0x0000                              # Success status

    def start(self, host: str, port: int):
        handlers = [(evt.EVT_C_STORE, self._handle_store),
                    (evt.EVT_C_ECHO,  lambda e: 0x0000)]
        return self.ae.start_server((host, port), block=False, evt_handlers=handlers)
```

- `AllStoragePresentationContexts` przyjmuje wszystkie storage SOP class (RTG = CR/DX, USG = US, CT, Secondary Capture = SC). pynetdicom domyślnie negocjuje też Implicit/Explicit VR LE; dla skompresowanych dorzucić JPEG transfer syntaxes.
- `block=False` → server w wątku tła; `_handle_store` woła się z tego wątku → most do agenta przez `run_coroutine_threadsafe` (analogicznie jak tray↔loop dzisiaj).
- Zwracany status `0x0000` = Success; przy błędzie zapisu zwróć `0xA700` (Out of Resources) żeby modality wiedziało.

## 5. dicom_parser.py — nagłówek + render

```python
import pydicom
from pydicom.pixel_data_handlers.util import apply_voi_lut

@dataclass
class DicomStudy:
    patient_id: str | None          # (0010,0020) — klucz round-trip (MWL)
    patient_name: str | None        # (0010,0010) PN "Last^First"
    species: str | None             # (0010,2201) PatientSpeciesDescription (vet!)
    breed: str | None               # (0010,2292)
    study_uid: str                  # (0020,000D) StudyInstanceUID
    series_uid: str                 # (0020,000E)
    sop_uid: str                    # (0008,0018) — idempotencja (dedup)
    modality: str                   # (0008,0060) CR/DX/US/CT
    study_date: str | None          # (0008,0020)
    study_desc: str | None          # (0008,1030)
    accession: str | None           # (0008,0050)
    dcm_path: Path                  # oryginał na dysku
    jpeg_path: Path | None          # derywat do karty
```

Render do JPEG: `apply_voi_lut(ds.pixel_array, ds)` → normalizacja 8-bit → Pillow `Image.save(quality=85)` (~600-800 KB jak dziś robi ręcznie Dominik). Oryginał `.dcm` zapisany obok (`captured_dicom/`), wysyłany do R2.

**Dopasowanie pacjenta:**
- Faza 1: best-effort po `patient_id` → fallback `patient_name`+`species` → fallback **manualne przypisanie** w VetFlow (jak nieзматchowane wyniki lab dzisiaj). Nie blokujemy — obraz ląduje w „do przypisania".
- Faza 2 (MWL): VetFlow wypycha worklist → technik wybiera pacjenta przy aparacie → wracający obraz ma NASZ `patient_id` → dopasowanie 100% bez przepisywania.

## 6. spool.py — bufor offline (rozwiązuje słaby internet)

Generyczna dyskowa kolejka, **współdzielona z HL7** (HL7 też na tym skorzysta):

- Każdy przychodzący obiekt → `spool/<uuid>/{payload.json, file.dcm}` + status `pending`.
- Worker (async task w agencie) drenuje kolejkę: upload → `done` (usuń) / błąd → backoff + retry. Przeżywa restart agenta.
- Idempotencja po `sop_uid` (dedup gdy modality wyśle ponownie).
- Limit retencji / cap dyskowy w configu.

To bezpośrednio adresuje „słaby internet" Dominika: obrazy buforują się lokalnie i syncują gdy łącze wróci.

## 7. vetflow_client.send_imaging_study (nowy)

```python
async def send_imaging_study(self, meta: dict, dcm: Path, jpeg: Path | None) -> int | None:
    # multipart: pola meta (patient_id, modality, study_uid, sop_uid...) + plik(i)
    endpoint = f"{self.url}/api/clinic/imaging/import-external"
    # X-Clinic-API-Key (jak send_result_json) — NIE Bearer (to legacy send_result)
```

**Zależność backendowa (osobny task w repo vetflow):** endpoint `POST /api/clinic/imaging/import-external` + model `ImagingStudy` (FK pacjent + ew. `clinic_visit_id`), storage `.dcm` w R2 (szyfrowane at-rest jak reszta), derywat przez istniejący WebP-pipe (VETFL-440). Auth przez clinic API-key (jak lab-results-external). Dedup po `sop_uid`. *MVP-skrót:* można na start reużyć `lab-results` jako typ „imaging" z załącznikiem, ale czysto = osobny model.

## 8. Config

`config.json` zyskuje blok `dicom` (osobny, bo to jeden SCP-węzeł, nie lista portów-device):

```json
{
  "vetflow_url": "https://vet-flow.pl",
  "api_key": "clinic_api_key_here",
  "devices": [ {"name": "VM100", "host": "auto", "port": 8888, "type": "cbc"} ],
  "dicom": {
    "enabled": true,
    "ae_title": "VETFLOW",
    "port": 11112,
    "store_originals": true,
    "render_jpeg": true,
    "jpeg_quality": 85
  },
  "spool": { "dir": "spool", "max_retry": 20, "max_age_days": 7 },
  "auto_discover": true,
  "log_file": "vetflow_connect.log"
}
```

`DicomConfig` dataclass w `config.py`; w `load_config` parsuj opcjonalny `raw.get("dicom")` (brak = moduł off → pełna wsteczna kompatybilność istniejących instalacji).

## 9. agent.py — wiring

W `run_agent`, po starcie HL7 listenerów:

```python
if config.dicom and config.dicom.enabled:
    dicom_cb = _make_dicom_callback(client, spool, tray)
    dicom = DicomListener(config.dicom.ae_title, dicom_cb, asyncio.get_running_loop())
    dicom_server = dicom.start("0.0.0.0", config.dicom.port)
    logger.info("[DICOM] Storage SCP '%s' nasłuchuje na :%d", config.dicom.ae_title, config.dicom.port)
```

`_make_dicom_callback` analogiczny do `_make_callback`: zapis surowca → parse → spool.enqueue → tray.notify. Uploader-worker drenuje spool niezależnie.

## 10. Zależności i build

- requirements: `pynetdicom`, `pydicom`, `numpy`, `pillow`.
- **PyInstaller gotcha** (mieli już ten problem z pystray — patrz `6d56800`): pynetdicom ładuje SOP classes dynamicznie → w `.spec` dodać `collect_submodules('pynetdicom')` + `collect_data_files('pydicom')` (słowniki DICOM). Inaczej `.exe` padnie na brakujących SOP class.
- Rozmiar `.exe` urośnie (numpy+pillow) — zaakceptować.

## 11. Testy

- `test_dicom_parser.py`: wczytaj próbkę `.dcm` → assert pól (patient_id, modality, species), assert render JPEG niepusty.
- Próbki: pydicom ma wbudowane test-files (`pydicom.data.get_testdata_file("CT_small.dcm")`); dorzucić anonimizowane RTG vet jeśli Przemek/Dominik dadzą.
- Integration smoke: `storescu` (z dcmtk) → agent na localhost:11112 → sprawdź job w spool.

## 12. Fazy / taski

1. **Spike (0.5–1 dzień):** `pynetdicom` Storage SCP na localhost, `storescu` wysyła testowy `.dcm`, `dicom_parser` wypluwa pola + JPEG. Cel: potwierdzić czysty parse + dopasowanie po nagłówku. *(bez backendu, bez spool)*
2. **VETFL-XXX (connect):** `dicom_listener` + `dicom_parser` + `DicomConfig` + `_make_dicom_callback` + `send_imaging_study` (stub endpoint). Bump `0.4.0`.
3. **VETFL-YYY (vetflow backend):** endpoint `imaging/import-external` + model `ImagingStudy` + R2 + derywat WebP + manualne przypisanie w UI.
4. **VETFL-ZZZ (spool):** dyskowa kolejka offline (benefit też dla HL7).
5. **Faza 2 — MWL SCP:** worklist out → round-trip matching. Osobny epic.

MVP demonstrowalny Dominikowi = fazy 1–3 (auto-odbiór RTG → karta pacjenta).
