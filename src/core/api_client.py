"""VetFlow HTTP client for registration, config, heartbeat and uploads."""

from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

logger = logging.getLogger("vetflow_connect")


class VetFlowClient:
    """Client for VetFlow device endpoints."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    @property
    def _bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def _legacy_headers(self) -> dict[str, str]:
        return {"X-Clinic-API-Key": self.api_key}

    async def register_device(self) -> dict:
        """Verify API key and fetch basic clinic info."""
        endpoint = f"{self.url}/api/device/register"
        payload = {"api_key": self.api_key}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers=self._legacy_headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                body = await self._read_json(response)
                if response.status not in (200, 201):
                    raise RuntimeError(f"Device registration failed ({response.status}): {body}")
                return body

    async def get_device_config(self) -> dict:
        """Fetch remote device/plugin configuration."""
        endpoint = f"{self.url}/api/device/config"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                endpoint,
                headers=self._legacy_headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                body = await self._read_json(response)
                if response.status != 200:
                    raise RuntimeError(f"Fetching device config failed ({response.status}): {body}")
                return body

    async def send_heartbeat(self, payload: dict) -> bool:
        """Send agent/plugin heartbeat to VetFlow."""
        endpoint = f"{self.url}/api/device/heartbeat"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=self._legacy_headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status in (200, 201, 204):
                        return True
                    logger.warning("Heartbeat rejected with status %s", response.status)
                    return False
        except aiohttp.ClientError as exc:
            logger.warning("Heartbeat failed: %s", exc)
            return False

    async def send_lab_result(self, payload: dict) -> int | None:
        """Upload a parsed lab result to the new device endpoint."""
        endpoint = f"{self.url}/api/device/lab-results"
        return await self._post_lab_result(endpoint, payload, headers=self._bearer_headers)

    async def send_result_json(self, payload: dict) -> int | None:
        """Backward-compatible upload against the legacy external import endpoint."""
        endpoint = f"{self.url}/api/clinic/lab-results/import-json-external"
        return await self._post_lab_result(endpoint, payload, headers=self._legacy_headers)

    async def send_images(self, lab_result_id: int, image_paths: list[Path]) -> bool:
        """Upload captured JPEG images for a lab result."""
        endpoint = f"{self.url}/api/clinic/lab-results/{lab_result_id}/images"
        file_handles = []
        try:
            data = aiohttp.FormData()
            for path in image_paths:
                handle = open(path, "rb")
                file_handles.append(handle)
                data.add_field(
                    "files",
                    handle,
                    filename=path.name,
                    content_type="image/jpeg",
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    data=data,
                    headers=self._legacy_headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    if response.status == 200:
                        return True
                    text = await response.text()
                    logger.error("VetFlow image upload error %d: %s", response.status, text[:500])
                    return False
        except aiohttp.ClientError as exc:
            logger.error("VetFlow image upload connection error: %s", exc)
            return False
        finally:
            for handle in file_handles:
                handle.close()

    async def send_imaging_study(
        self,
        meta: dict,
        dcm_path: Path,
        jpeg_path: Path | None = None,
    ) -> int | None:
        """Upload a DICOM imaging study (original .dcm + optional JPEG derivative).

        Multipart: `meta` fields (patient_id, modality, study_uid, sop_uid, ...) +
        the `.dcm` file + optional `.jpg` derivative. Server dedups on sop_uid.
        Returns the imaging_study id on success, None on failure.
        """
        endpoint = f"{self.url}/api/clinic/imaging/import-external"
        file_handles = []
        try:
            data = aiohttp.FormData()
            for key, value in meta.items():
                if value is not None:
                    data.add_field(key, str(value))

            dcm_handle = open(dcm_path, "rb")
            file_handles.append(dcm_handle)
            data.add_field(
                "dcm", dcm_handle, filename=dcm_path.name,
                content_type="application/dicom",
            )
            if jpeg_path is not None and Path(jpeg_path).exists():
                jpeg_handle = open(jpeg_path, "rb")
                file_handles.append(jpeg_handle)
                data.add_field(
                    "jpeg", jpeg_handle, filename=Path(jpeg_path).name,
                    content_type="image/jpeg",
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    data=data,
                    headers=self._legacy_headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as response:
                    if response.status in (200, 201):
                        body = await self._read_json(response)
                        return body.get("id")
                    text = await response.text()
                    logger.error("VetFlow imaging upload error %d: %s", response.status, text[:500])
                    return None
        except aiohttp.ClientError as exc:
            logger.error("VetFlow imaging upload connection error: %s", exc)
            return None
        finally:
            for handle in file_handles:
                handle.close()

    async def check_connection(self) -> bool:
        """Compatibility helper used by legacy callers."""
        try:
            await self.register_device()
            return True
        except Exception as exc:
            logger.error("Connection check failed: %s", exc)
            return False

    async def _post_lab_result(
        self,
        endpoint: str,
        payload: dict,
        *,
        headers: dict[str, str],
    ) -> int | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status in (200, 201):
                        body = await self._read_json(response)
                        return body.get("id")
                    text = await response.text()
                    logger.error("VetFlow API error %d: %s", response.status, text[:500])
                    return None
        except aiohttp.ClientError as exc:
            logger.error("VetFlow connection error: %s", exc)
            return None

    async def _read_json(self, response: aiohttp.ClientResponse) -> dict:
        try:
            return await response.json(content_type=None)
        except Exception:
            return {"detail": await response.text()}
