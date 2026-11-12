"""Tests for the DICOM Storage SCP listener (VETFL-993)."""

import asyncio
import pathlib
import sys
import threading

import pytest

_DICOM_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins" / "dicom"
sys.path.insert(0, str(_DICOM_DIR))

pydicom = pytest.importorskip("pydicom")
pynetdicom = pytest.importorskip("pynetdicom")
from pydicom.data import get_testdata_file  # noqa: E402
from pydicom.uid import ImplicitVRLittleEndian, JPEGBaseline8Bit, RLELossless  # noqa: E402
from pynetdicom import AE  # noqa: E402
from pynetdicom.sop_class import UltrasoundImageStorage  # noqa: E402

from dicom_listener import DicomListener  # noqa: E402


@pytest.fixture
def listener(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    received: list[pathlib.Path] = []
    done = threading.Event()

    async def on_store(dcm_path):
        received.append(dcm_path)
        done.set()

    scp = DicomListener("VETFLOW", on_store=on_store, loop=loop, captured_dir=tmp_path)
    server = scp.start("127.0.0.1", 0)
    port = server.server_address[1]
    yield port, received, done
    scp.stop()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def _associate(port, abstract_syntax, transfer_syntax):
    scu = AE(ae_title="PROPET70")
    scu.add_requested_context(abstract_syntax, transfer_syntax)
    return scu.associate("127.0.0.1", port, ae_title="VETFLOW")


@pytest.mark.parametrize("transfer_syntax", [JPEGBaseline8Bit, RLELossless, ImplicitVRLittleEndian])
def test_usg_context_accepted_in_compressed_syntax(listener, transfer_syntax):
    port, _, _ = listener
    assoc = _associate(port, UltrasoundImageStorage, transfer_syntax)
    try:
        assert assoc.is_established
        assert [cx.transfer_syntax[0] for cx in assoc.accepted_contexts] == [transfer_syntax]
    finally:
        assoc.release()


@pytest.mark.parametrize("retired_us", ["1.2.840.10008.5.1.4.1.1.6", "1.2.840.10008.5.1.4.1.1.3"])
def test_retired_ultrasound_classes_accepted(listener, retired_us):
    port, _, _ = listener
    assoc = _associate(port, retired_us, ImplicitVRLittleEndian)
    try:
        assert assoc.is_established
        assert len(assoc.accepted_contexts) == 1
    finally:
        assoc.release()


def test_jpeg_compressed_image_is_stored_as_received(listener):
    port, received, done = listener
    ds = pydicom.dcmread(get_testdata_file("SC_rgb_jpeg_dcmtk.dcm"))
    assoc = _associate(port, ds.SOPClassUID, ds.file_meta.TransferSyntaxUID)
    try:
        assert assoc.is_established
        status = assoc.send_c_store(ds)
        assert status.Status == 0x0000
    finally:
        assoc.release()

    assert done.wait(5)
    stored = pydicom.dcmread(received[0])
    assert stored.file_meta.TransferSyntaxUID == JPEGBaseline8Bit
    assert stored.SOPInstanceUID == ds.SOPInstanceUID


def test_refused_image_type_is_logged_with_proposed_syntaxes(listener, caplog):
    # A refused context never reaches C-STORE — without this the modality says "storage
    # failed" and our log is silent (Bandikot USG, 12.11).
    from pynetdicom.sop_class import ModalityWorklistInformationFind, Verification

    port, _, _ = listener
    scu = AE(ae_title="PROPET70")
    scu.add_requested_context(Verification)
    scu.add_requested_context(ModalityWorklistInformationFind)
    with caplog.at_level("WARNING", logger="vetflow_connect"):
        assoc = scu.associate("127.0.0.1", port, ae_title="VETFLOW")
        assert assoc.is_established
        assoc.release()

    refused = [r.getMessage() for r in caplog.records if "odrzucony typ" in r.getMessage()]
    assert len(refused) == 1
    assert "PROPET70" in refused[0]
    assert "Modality Worklist" in refused[0]
    assert "Implicit VR Little Endian" in refused[0]
