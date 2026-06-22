"""DICOM Storage SCP listener for VetFlowConnect — pynetdicom, thread-based.

Mirror of hl7_listener.py, but the transport is DICOM, not MLLP. pynetdicom's
server is synchronous/thread-based (`block=False` → its own thread), so the
C-STORE handler runs OFF the agent's asyncio loop. We keep that handler tiny —
write the raw .dcm fast and bridge to the agent loop via run_coroutine_threadsafe
(same pattern as tray↔loop). Heavy parse/render/upload happens in the callback.

Lesson from the live VXvue test (VETFL-542): respond 0x0000 fast and write the
already-encoded bytes (no decode/re-encode) so the modality's queue confirms DONE.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from pynetdicom import AE, AllStoragePresentationContexts, evt
from pynetdicom.sop_class import Verification  # C-ECHO

logger = logging.getLogger("vetflow_connect")

# async callable(dcm_path: Path) — agent-side parse + render + spool
StoreCallback = Callable[[Path], Awaitable[None]]


class DicomListener:
    """DICOM Storage SCP. A modality (RTG/USG soft) targets us as a C-STORE
    destination; each received object lands as a .dcm and is handed to the agent."""

    def __init__(
        self,
        ae_title: str,
        on_store: StoreCallback,
        loop: asyncio.AbstractEventLoop,
        captured_dir: Path,
    ):
        self.ae = AE(ae_title=ae_title)
        # Accept every storage SOP class (CR/DX, US, CT, Secondary Capture, ...)
        self.ae.supported_contexts = AllStoragePresentationContexts
        self.ae.add_supported_context(Verification)  # C-ECHO ping (modality "Echo" test)
        self.ae.maximum_pdu_size = 0  # no cap our side
        self._on_store = on_store
        self._loop = loop
        self._captured_dir = captured_dir
        self._server = None

    def _handle_store(self, event) -> int:
        """C-STORE handler (runs in pynetdicom's thread). Fast raw write +
        bridge to agent loop. Returns 0x0000 success / 0xA700 out-of-resources."""
        try:
            from pydicom.filewriter import write_file_meta_info

            sop_uid = event.request.AffectedSOPInstanceUID
            self._captured_dir.mkdir(parents=True, exist_ok=True)
            dcm_path = self._captured_dir / f"{sop_uid}.dcm"
            # Write already-encoded dataset bytes — no decode → instant ack.
            with open(dcm_path, "wb") as f:
                f.write(b"\x00" * 128)
                f.write(b"DICM")
                write_file_meta_info(f, event.file_meta, enforce_standard=False)
                f.write(event.request.DataSet.getvalue())

            logger.info("[DICOM] C-STORE %s (%d B)", sop_uid, dcm_path.stat().st_size)
            # hand off to the agent's asyncio loop (parse/render/upload there)
            asyncio.run_coroutine_threadsafe(self._on_store(dcm_path), self._loop)
            return 0x0000
        except Exception:
            logger.exception("[DICOM] C-STORE handler failed")
            return 0xA700  # Out of Resources

    @staticmethod
    def _handle_echo(event) -> int:
        logger.debug("[DICOM] C-ECHO from %s", event.assoc.requestor.address)
        return 0x0000

    def start(self, host: str, port: int):
        """Start the Storage SCP in a background thread. Returns the server."""
        handlers = [
            (evt.EVT_C_STORE, self._handle_store),
            (evt.EVT_C_ECHO, self._handle_echo),
        ]
        self._server = self.ae.start_server(
            (host, port), block=False, evt_handlers=handlers
        )
        logger.info(
            "[DICOM] Storage SCP '%s' listening on %s:%d",
            self.ae.ae_title, host, port,
        )
        return self._server

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
