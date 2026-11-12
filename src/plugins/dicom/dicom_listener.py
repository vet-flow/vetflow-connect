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

from pynetdicom import ALL_TRANSFER_SYNTAXES, AE, AllStoragePresentationContexts, evt
from pynetdicom.sop_class import Verification  # C-ECHO

logger = logging.getLogger("vetflow_connect")


def _uid_name(uid) -> str:
    """'Ultrasound Image Storage' instead of a bare 1.2.840... in the log."""
    name = getattr(uid, "name", "") or ""
    return name if name and name != str(uid) else str(uid)

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
        # Accept every storage SOP class (CR/DX, US, CT, Secondary Capture, ...) in EVERY
        # transfer syntax, compressed included. VETFL-993: USG ProPet70 (Bandikot) proposes
        # JPEG; with pynetdicom's default (uncompressed only) the context was rejected during
        # negotiation — Echo OK, "storage failed" on the modality, nothing in our log.
        # We write the encoded bytes as-is, so storing needs no decoder.
        for context in AllStoragePresentationContexts:
            self.ae.add_supported_context(context.abstract_syntax, ALL_TRANSFER_SYNTAXES)
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

    # VETFL-993: a rejected negotiation never reaches C-STORE, so without these the log stays
    # silent while the modality shows "storage failed". Log who called and what we refused.
    @staticmethod
    def _handle_accepted(event) -> None:
        assoc = event.assoc
        for cx in assoc.rejected_contexts:
            logger.warning(
                "[DICOM] %s (AE %s): odrzucony typ %s, składnie: %s",
                assoc.requestor.address,
                assoc.requestor.ae_title,
                _uid_name(cx.abstract_syntax),
                ", ".join(_uid_name(ts) for ts in cx.transfer_syntax) or "-",
            )

    @staticmethod
    def _handle_rejected(event) -> None:
        assoc = event.assoc
        logger.warning(
            "[DICOM] Odrzucone połączenie od %s (AE %s -> %s)",
            assoc.requestor.address,
            assoc.requestor.ae_title,
            assoc.acceptor.ae_title,
        )

    @staticmethod
    def _handle_aborted(event) -> None:
        logger.warning("[DICOM] Połączenie od %s przerwane (abort)", event.assoc.requestor.address)

    def start(self, host: str, port: int):
        """Start the Storage SCP in a background thread. Returns the server."""
        handlers = [
            (evt.EVT_C_STORE, self._handle_store),
            (evt.EVT_C_ECHO, self._handle_echo),
            (evt.EVT_ACCEPTED, self._handle_accepted),
            (evt.EVT_REJECTED, self._handle_rejected),
            (evt.EVT_ABORTED, self._handle_aborted),
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
