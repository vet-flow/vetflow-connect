"""DICOM imaging plugin — Storage SCP node for RTG/USG/CT modalities.

Mirror of the Skyla plugin, but the protocol is DICOM (pynetdicom Storage SCP)
and the payload is an imaging study, not a lab result. A modality (VXvue/Vieworks,
Examion, Sound, generic CR...) targets us as its DICOM destination; each received
object is parsed, rendered to a JPEG derivative, and pushed to VetFlow.

Validated live against Vieworks VXvue (VETFL-542): C-ECHO + C-STORE → DONE.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

try:
    from ...core.config import app_dir
    from ...core.plugin_base import DevicePlugin
except ImportError:  # frozen / flat import
    from core.config import app_dir
    from core.plugin_base import DevicePlugin

from .dicom_listener import DicomListener
from .dicom_parser import parse_dicom, render_jpeg

logger = logging.getLogger("vetflow_connect")


class DicomPlugin(DevicePlugin):
    """Plugin exposing a DICOM Storage SCP for imaging modalities."""

    name = "dicom"
    display_name = "DICOM Imaging (RTG/USG/CT)"
    protocol = "dicom"
    device_type = "imaging"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._listener: DicomListener | None = None
        self._server = None
        self._device_config: dict = {}

    async def start(self, config: dict) -> None:
        self._device_config = config
        ae_title = config.get("ae_title", "VETFLOW")
        port = int(config.get("port", 11112))
        loop = asyncio.get_running_loop()
        captured_dir = app_dir() / "captured_dicom"

        self._listener = DicomListener(
            ae_title=ae_title,
            on_store=self._handle_store,
            loop=loop,
            captured_dir=captured_dir,
        )
        # pynetdicom server runs in its own thread (block=False); C-STORE handler
        # bridges back to this loop via run_coroutine_threadsafe → _handle_store.
        self._server = self._listener.start(host="0.0.0.0", port=port)
        logger.info("[%s] DICOM Storage SCP '%s' on port %d", self._name(), ae_title, port)

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            self._server = None
        logger.info("[%s] DICOM plugin stopped", self._name())

    async def health_check(self) -> bool:
        return self._server is not None

    async def _handle_store(self, dcm_path: Path) -> None:
        """Parse + render + upload one received DICOM object. Runs on the agent loop."""
        try:
            import pydicom

            ds = pydicom.dcmread(dcm_path)
        except Exception:
            logger.exception("[%s] Failed to read %s", self._name(), dcm_path.name)
            return

        study = parse_dicom(ds, dcm_path)
        if not study.sop_uid:
            logger.warning("[%s] Object without SOPInstanceUID, skipping", self._name())
            return

        # Render JPEG derivative for the patient card (image objects only; a Dose SR
        # / structured report has no pixels → render returns None, we still send meta).
        jpeg_path: Path | None = None
        if self._device_config.get("render_jpeg", True) and "PixelData" in ds:
            quality = int(self._device_config.get("jpeg_quality", 85))
            jpeg_dir = app_dir() / "captured_dicom_jpeg"
            jpeg_path = render_jpeg(ds, jpeg_dir / f"{study.sop_uid}.jpg", quality=quality)
            study.jpeg_path = jpeg_path

        if self.api_client is None:
            logger.warning("[%s] No API client, dropping study %s", self._name(), study.sop_uid)
            return

        status_id = await self.api_client.send_imaging_study(study.to_meta(), dcm_path, jpeg_path)
        logger.info(
            "[%s] %s %s/%s %s -> VetFlow %s",
            self._name(),
            study.modality or "?",
            study.body_part or "?",
            study.view_position or "?",
            study.patient_name or study.patient_id or "?",
            "OK" if status_id else "FAIL",
        )

    def _name(self) -> str:
        return self._device_config.get("name", self.display_name)
