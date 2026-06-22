"""DICOM parser for VetFlowConnect — pydicom header → DicomStudy + JPEG render.

Mirror of hl7_parser.py: raw input (here a saved .dcm) → typed dataclass + a
lightweight derivative (JPEG) for the patient card. Heavy pixel decode lives
on-prem in the agent (cloud backend only stores + serves), so this module owns
both the metadata extraction and the render.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("vetflow_connect")


@dataclass
class DicomStudy:
    """Parsed DICOM instance metadata + on-disk artifacts.

    One instance per received C-STORE object (RTG = usually a few per study).
    `sop_uid` is the idempotency key (dedup when a modality re-sends).
    """

    patient_id: str | None      # (0010,0020) — round-trip key (MWL phase 2)
    patient_name: str | None    # (0010,0010) PN "Last^First"
    species: str | None         # (0010,2201) PatientSpeciesDescription (vet!)
    breed: str | None           # (0010,2292) PatientBreedDescription
    study_uid: str              # (0020,000D) StudyInstanceUID — grouping
    series_uid: str | None      # (0020,000E)
    sop_uid: str                # (0008,0018) SOPInstanceUID — dedup
    modality: str | None        # (0008,0060) CR/DX/US/CT
    study_date: str | None      # (0008,0020) YYYYMMDD
    study_desc: str | None      # (0008,1030)
    accession: str | None       # (0008,0050)
    body_part: str | None       # (0018,0015) BodyPartExamined
    view_position: str | None   # (0018,5101) ViewPosition
    rows: int | None            # (0028,0010)
    columns: int | None         # (0028,0011)
    dcm_path: Path              # original on disk → R2
    jpeg_path: Path | None = None  # derivative → patient card

    def to_meta(self) -> dict:
        """Serializable metadata payload (no local paths) for the API multipart."""
        d = asdict(self)
        d.pop("dcm_path", None)
        d.pop("jpeg_path", None)
        return {k: v for k, v in d.items() if v is not None}


def _str(value) -> str | None:
    """Coerce a pydicom value (PersonName, MultiValue, etc.) to a trimmed str."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_dicom(ds, dcm_path: Path) -> DicomStudy:
    """Extract DicomStudy metadata from a pydicom Dataset (already read)."""

    def g(tag):
        return _str(getattr(ds, tag, None))

    return DicomStudy(
        patient_id=g("PatientID"),
        patient_name=g("PatientName"),
        species=g("PatientSpeciesDescription"),
        breed=g("PatientBreedDescription"),
        study_uid=g("StudyInstanceUID") or "",
        series_uid=g("SeriesInstanceUID"),
        sop_uid=g("SOPInstanceUID") or "",
        modality=g("Modality"),
        study_date=g("StudyDate"),
        study_desc=g("StudyDescription"),
        accession=g("AccessionNumber"),
        body_part=g("BodyPartExamined"),
        view_position=g("ViewPosition"),
        rows=_int(getattr(ds, "Rows", None)),
        columns=_int(getattr(ds, "Columns", None)),
        dcm_path=dcm_path,
    )


def render_jpeg(ds, out_path: Path, quality: int = 85) -> Path | None:
    """Render the pixel data to a normalized 8-bit JPEG (~600-800 KB).

    Returns the path on success, None if the image can't be decoded (e.g. a
    Dose SR object has no pixels — those are metadata-only, skipped upstream).
    """
    try:
        import numpy as np
        from PIL import Image

        try:
            from pydicom.pixel_data_handlers.util import apply_voi_lut
        except Exception:  # pragma: no cover — older pydicom
            apply_voi_lut = None

        arr = ds.pixel_array
        if apply_voi_lut is not None:
            try:
                arr = apply_voi_lut(arr, ds)
            except Exception:
                logger.debug("[DICOM] apply_voi_lut failed, using raw pixels")

        arr = arr.astype("float64")
        rng = float(np.ptp(arr)) or 1.0
        arr = (arr - arr.min()) / rng * 255.0
        if str(getattr(ds, "PhotometricInterpretation", "")) == "MONOCHROME1":
            arr = 255.0 - arr  # invert so bone is bright

        img = Image.fromarray(arr.astype("uint8"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=quality)
        return out_path
    except Exception:
        logger.exception("[DICOM] JPEG render failed for %s", out_path.name)
        return None
