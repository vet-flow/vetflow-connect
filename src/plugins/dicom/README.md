# DICOM Imaging plugin

Exposes a **DICOM Storage SCP** node. Imaging modalities (RTG/USG/CT acquisition
software — Vieworks VXvue, Examion, Sound, generic CR…) are configured to send
images to VetFlowConnect as a *DICOM destination* (AE title + IP + port). Each
received object is parsed, rendered to a JPEG derivative, and pushed to VetFlow
(`POST /api/device/imaging-studies`); the original `.dcm` goes to R2.

Validated live against **Vieworks VXvue** (VETFL-542): C-ECHO + C-STORE → DONE.

## Device config (server-side, `get_device_config`)

```json
{ "plugin": "dicom", "name": "RTG Vieworks", "ae_title": "VETFLOW",
  "port": 11112, "render_jpeg": true, "jpeg_quality": 85, "enabled": true }
```

## Files

- `plugin.py` — `DicomPlugin(DevicePlugin)`: starts the SCP, bridges C-STORE → upload.
- `dicom_listener.py` — pynetdicom Storage SCP (thread server, fast 0x0000 ack).
- `dicom_parser.py` — pydicom header → `DicomStudy` + JPEG render.

Deps: `pynetdicom`, `pydicom`, `numpy`, `pillow` (see root requirements + `.spec` hidden imports).
