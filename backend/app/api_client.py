"""AIGate API client."""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


def stringify_error(value: Any) -> str:
    if value is None:
        return "AIGate request failed"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [stringify_error(item) for item in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("message", "detail", "msg", "type", "error"):
            if value.get(key):
                return stringify_error(value[key])
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


@dataclass
class AIGateError(Exception):
    message: str
    status_code: Optional[int] = None
    code: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        prefix = f"[{self.status_code}] " if self.status_code else ""
        suffix = f" ({self.code})" if self.code else ""
        return f"{prefix}{self.message}{suffix}"


class AIGateClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.base_url = settings.PROVIDER_API_BASE.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        timeout: int = 30,
        **kwargs,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.request(method, url, headers=self.headers, **kwargs) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    data = {"error": {"message": text or resp.reason}}

                if resp.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        message = stringify_error(err.get("message") or err.get("type") or err)
                        code = err.get("code")
                    else:
                        message = stringify_error(err or data or "AIGate request failed")
                        code = None
                    raise AIGateError(message=message, status_code=resp.status, code=code, payload=data)

                if not isinstance(data, dict):
                    raise AIGateError("Unexpected AIGate response format", payload={"response": data})
                return data

    async def get_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/balance", timeout=30)

    async def get_video_models(self) -> List[Dict[str, str]]:
        data = await self._request("GET", "/models", timeout=30)
        raw_models = data.get("data") or data.get("models") or []
        video_markers = ("video", "kling", "luma", "runway", "pika", "seedance", "wan", "vidu", "veo")
        models: List[Dict[str, str]] = []

        for item in raw_models:
            if isinstance(item, str):
                model_id = item
                name = item
            elif isinstance(item, dict):
                model_id = str(item.get("id") or "")
                name = str(item.get("name") or item.get("title") or model_id)
            else:
                continue

            haystack = f"{model_id} {name}".lower()
            if model_id and any(marker in haystack for marker in video_markers):
                models.append({"id": model_id, "name": name})

        return models

    async def generate_video(
        self,
        *,
        model: str,
        prompt: str,
        image_urls: Optional[List[str]] = None,
        images_b64: Optional[List[str]] = None,
        video_url: Optional[str] = None,
        video_b64: Optional[str] = None,
        audio_url: Optional[str] = None,
        audio_b64: Optional[str] = None,
        duration: int = 5,
        resolution: str = "720p",
        aspect_ratio: str = "16:9",
        audio: bool = False,
        quality: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        common = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "audio": audio,
            "quality": quality,
            "negative_prompt": negative_prompt,
            "seed": seed,
        }

        use_urls = bool(image_urls or video_url or audio_url)
        payload = (
            self._video_url_payload(
                **common,
                image_urls=image_urls or [],
                video_url=video_url,
                audio_url=audio_url,
                include_extra_refs=True,
            )
            if use_urls
            else self._video_b64_payload(
                **common,
                images_b64=images_b64 or [],
                video_b64=video_b64,
                audio_b64=audio_b64,
                include_extra_refs=True,
            )
        )

        try:
            return await self._request("POST", "/video/generations", timeout=900, json=payload)
        except AIGateError as exc:
            has_experimental_refs = bool(
                (images_b64 and len(images_b64) > 1)
                or audio_b64
                or (image_urls and len(image_urls) > 1)
                or audio_url
            )
            if exc.status_code == 400 and has_experimental_refs:
                logger.info("AIGate rejected experimental reference payload; retrying with the official inputs only.")
                fallback_payload = (
                    self._video_url_payload(
                        **common,
                        image_urls=(image_urls or [])[:1],
                        video_url=video_url,
                        audio_url=None,
                        include_extra_refs=False,
                    )
                    if use_urls
                    else self._video_b64_payload(
                        **common,
                        images_b64=(images_b64 or [])[:1],
                        video_b64=video_b64,
                        audio_b64=None,
                        include_extra_refs=False,
                    )
                )
                result = await self._request("POST", "/video/generations", timeout=900, json=fallback_payload)
                result["_multiref_fallback"] = True
                return result
            raise

    def _base_payload(
        self,
        *,
        model: str,
        prompt: str,
        duration: int,
        resolution: str,
        aspect_ratio: str,
        audio: bool,
        quality: Optional[str],
        negative_prompt: Optional[str],
        seed: Optional[int],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "audio": audio,
            "n": 1,
        }

        if quality:
            payload["quality"] = quality

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None:
            payload["seed"] = seed

        return payload

    def _video_url_payload(
        self,
        *,
        model: str,
        prompt: str,
        image_urls: List[str],
        video_url: Optional[str],
        audio_url: Optional[str],
        duration: int,
        resolution: str,
        aspect_ratio: str,
        audio: bool,
        quality: Optional[str],
        negative_prompt: Optional[str],
        seed: Optional[int],
        include_extra_refs: bool,
    ) -> Dict[str, Any]:
        payload = self._base_payload(
            model=model,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            audio=audio,
            quality=quality,
            negative_prompt=negative_prompt,
            seed=seed,
        )

        if image_urls:
            payload["image_files"] = image_urls[:6]
            if include_extra_refs and len(image_urls) > 1:
                payload.setdefault("provider_options", {})["input_images"] = image_urls[1:6]

        if video_url:
            payload["video_url"] = video_url

        if audio_url and include_extra_refs:
            payload["audio_url"] = audio_url
            payload.setdefault("provider_options", {})["audio_reference"] = audio_url

        return payload

    def _video_b64_payload(
        self,
        *,
        model: str,
        prompt: str,
        images_b64: List[str],
        video_b64: Optional[str],
        audio_b64: Optional[str],
        duration: int,
        resolution: str,
        aspect_ratio: str,
        audio: bool,
        quality: Optional[str],
        negative_prompt: Optional[str],
        seed: Optional[int],
        include_extra_refs: bool,
    ) -> Dict[str, Any]:
        payload = self._base_payload(
            model=model,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            audio=audio,
            quality=quality,
            negative_prompt=negative_prompt,
            seed=seed,
        )

        if images_b64:
            payload["input_image_b64"] = images_b64[0]
            if include_extra_refs and len(images_b64) > 1:
                payload.setdefault("provider_options", {})["input_images"] = images_b64[1:6]

        if video_b64:
            payload["input_video_b64"] = video_b64

        if audio_b64 and include_extra_refs:
            payload["input_audio_b64"] = audio_b64
            payload.setdefault("provider_options", {})["audio_reference_b64"] = audio_b64

        return payload

    async def download_video_bytes(self, url: str) -> bytes:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise AIGateError(f"Video download failed: {resp.status}", status_code=resp.status)
                return await resp.read()


def extract_video_url(result: Dict[str, Any]) -> Optional[str]:
    videos = result.get("videos") or []
    if videos and isinstance(videos[0], dict) and videos[0].get("url"):
        return videos[0]["url"]

    data = result.get("data") or []
    if data and isinstance(data[0], dict) and data[0].get("url"):
        return data[0]["url"]

    if result.get("url"):
        return result["url"]

    return None


def format_balance(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    if "balance" in data:
        parts.append(f"Баланс: {data['balance']}")
    if "used" in data:
        parts.append(f"Потрачено: {data['used']}")

    token = data.get("token") or {}
    if token.get("remaining") is not None:
        parts.append(f"Остаток токена: {token['remaining']}")
    if token.get("unlimited_quota"):
        parts.append("Безлимитная квота")

    return "\n".join(parts) if parts else str(data)
