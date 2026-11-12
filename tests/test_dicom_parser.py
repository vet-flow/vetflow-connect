"""Tests for the DICOM plugin parser/render (VETFL-543)."""

import pathlib
import sys

import pytest

_DICOM_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins" / "dicom"
sys.path.insert(0, str(_DICOM_DIR))

pydicom = pytest.importorskip("pydicom")
from pydicom.data import get_testdata_file  # noqa: E402

from dicom_parser import DicomStudy, parse_dicom, render_jpeg  # noqa: E402


def _sample_ds():
    path = pathlib.Path(get_testdata_file("MR_small.dcm"))
    return pydicom.dcmread(path), path


def test_parse_extracts_core_fields():
    ds, path = _sample_ds()
    study = parse_dicom(ds, path)
    assert isinstance(study, DicomStudy)
    assert study.sop_uid           # dedup key present
    assert study.study_uid         # grouping key present
    assert study.modality == "MR"
    assert study.rows and study.columns


def test_to_meta_drops_paths_and_nones():
    ds, path = _sample_ds()
    meta = parse_dicom(ds, path).to_meta()
    assert "dcm_path" not in meta and "jpeg_path" not in meta
    assert meta["sop_uid"] and meta["modality"] == "MR"
    assert all(v is not None for v in meta.values())


def test_render_jpeg_produces_file(tmp_path):
    ds, _ = _sample_ds()
    out = render_jpeg(ds, tmp_path / "render.jpg")
    assert out is not None and out.exists()
    assert out.stat().st_size > 0


def test_render_jpeg_multiframe_uses_first_frame(tmp_path):
    # VETFL-993: USG sends cine loops (many frames) — the card preview is frame one.
    ds = pydicom.dcmread(get_testdata_file("SC_rgb_rle_2frame.dcm"))
    out = render_jpeg(ds, tmp_path / "cine.jpg")
    assert out is not None and out.exists()
    from PIL import Image

    with Image.open(out) as img:
        assert img.size == (ds.Columns, ds.Rows)


def test_render_jpeg_from_jpeg_compressed_image(tmp_path):
    ds = pydicom.dcmread(get_testdata_file("SC_rgb_jpeg_dcmtk.dcm"))
    out = render_jpeg(ds, tmp_path / "jpeg.jpg")
    assert out is not None and out.exists()
