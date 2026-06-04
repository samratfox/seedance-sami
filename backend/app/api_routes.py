"""FastAPI routes used by the Telegram Mini App."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from app.api_client import AIGateClient, AIGateError, extract_video_url, format_balance
from app.config import settings
from app.database import db
from app.websocket import manager

router = APIRouter()
logger = logging.getLogger(__name__)


def validate_telegram_init_data(init_data: str) -> Dict:
    if settings.ALLOW_DEV_AUTH and (not init_data or init_data == "dev"):
        return {"id": settings.DEV_TELEGRAM_ID, "first_name": "Demo", "username": "demo_user"}

    if not settings.BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    parsed = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=403, detail="Telegram auth hash is missing")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=403, detail="Invalid Telegram Mini App signature")

    auth_date_raw = parsed.get("auth_date")
    if auth_date_raw:
        try:
            if time.time() - int(auth_date_raw) > settings.TELEGRAM_AUTH_MAX_AGE_SECONDS:
                raise HTTPException(status_code=403, detail="Telegram auth data is expired")
        except ValueError:
            raise HTTPException(status_code=403, detail="Invalid Telegram auth date")

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=403, detail="Invalid Telegram user payload")

    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Telegram user id is missing")
    return user


async def get_or_create_user(init_data: str) -> Dict:
    tg_user = validate_telegram_init_data(init_data)
    telegram_id = int(tg_user["id"])
    user = await db.get_user(telegram_id)

    if not user:
        full_name = " ".join(part for part in [tg_user.get("first_name"), tg_user.get("last_name")] if part).strip()
        await db.create_user(telegram_id, tg_user.get("username"), full_name)
        user = await db.get_user(telegram_id)

    if not user:
        raise HTTPException(status_code=500, detail="Cannot create local user")
    return user


def ensure_supported(value, allowed, label: str):
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported {label}: {value}")


async def upload_to_b64(
    files: Optional[List[UploadFile]],
    *,
    limit: int,
    expected_prefix: str,
    label: str,
) -> List[str]:
    encoded: List[str] = []
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024

    for file in (files or [])[:limit]:
        if not file or not file.filename:
            continue
        if file.content_type and not file.content_type.startswith(expected_prefix):
            raise HTTPException(status_code=400, detail=f"Неверный тип файла: {label}")

        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"{label} больше {settings.MAX_UPLOAD_MB} MB")

        encoded.append(base64.b64encode(content).decode("utf-8"))

    return encoded


async def upload_one_to_b64(
    file: Optional[UploadFile],
    *,
    expected_prefix: str,
    label: str,
) -> Optional[str]:
    values = await upload_to_b64([file] if file else None, limit=1, expected_prefix=expected_prefix, label=label)
    return values[0] if values else None


def local_video_path(generation_id: int) -> Path:
    directory = Path(settings.MEDIA_DIR) / "generations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{generation_id}.mp4"


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def api_config():
    return {
        "referral_url": settings.REFERRAL_URL,
        "durations": settings.SUPPORTED_DURATIONS,
        "resolutions": settings.SUPPORTED_RESOLUTIONS,
        "ratios": settings.SUPPORTED_RATIOS,
        "qualities": settings.SUPPORTED_QUALITIES,
        "model_modes": settings.model_modes,
        "max_image_references": settings.MAX_IMAGE_REFERENCES,
        "max_upload_mb": settings.MAX_UPLOAD_MB,
        "max_prompt_length": settings.MAX_PROMPT_LENGTH,
    }


def estimate_cost(model_mode: str, resolution: str, duration: int) -> float:
    mode = settings.model_modes[model_mode]
    pricing = mode.get("pricing") or {}
    return round(float(pricing.get(resolution, 0)) * duration, 6)


@router.post("/api/balance")
async def api_balance(init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    if not user.get("api_key"):
        return {"has_key": False, "balance": None, "raw": None}

    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        return {"has_key": True, "balance": format_balance(balance), "raw": balance}
    except AIGateError as exc:
        return {"has_key": True, "balance": None, "raw": None, "error": str(exc)}


@router.post("/api/models")
async def api_models(init_data: str = Form(...)):
    user = await get_or_create_user(init_data)
    if not user.get("api_key"):
        return {"models": []}

    try:
        models = await AIGateClient(user["api_key"]).get_video_models()
        return {"models": models}
    except AIGateError as exc:
        return {"models": [], "error": str(exc)}


@router.post("/api/generate")
async def api_generate(
    init_data: str = Form(...),
    model_mode: str = Form("fast"),
    prompt: str = Form(...),
    duration: int = Form(5),
    resolution: str = Form("720p"),
    ratio: str = Form("16:9"),
    audio: bool = Form(False),
    negative_prompt: str = Form(""),
    seed: Optional[int] = Form(None),
    image_files: Optional[List[UploadFile]] = File(default=None),
    video_file: Optional[UploadFile] = File(default=None),
    audio_file: Optional[UploadFile] = File(default=None),
):
    user = await get_or_create_user(init_data)
    if not user.get("api_key"):
        raise HTTPException(status_code=400, detail="Сначала подключите API-ключ AIGate")

    prompt = prompt.strip()
    negative_prompt = negative_prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Промпт не может быть пустым")
    if len(prompt) > settings.MAX_PROMPT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Промпт длиннее {settings.MAX_PROMPT_LENGTH} символов")

    ensure_supported(duration, settings.SUPPORTED_DURATIONS, "duration")
    ensure_supported(resolution, settings.SUPPORTED_RESOLUTIONS, "resolution")
    ensure_supported(ratio, settings.SUPPORTED_RATIOS, "aspect ratio")
    ensure_supported(model_mode, settings.SUPPORTED_QUALITIES, "quality")

    mode = settings.model_modes[model_mode]
    model = mode["id"]
    if not model:
        raise HTTPException(status_code=500, detail=f"Model id for {model_mode} is not configured")

    images_b64 = await upload_to_b64(
        image_files,
        limit=settings.MAX_IMAGE_REFERENCES,
        expected_prefix="image/",
        label="Фото-референс",
    )
    video_b64 = await upload_one_to_b64(video_file, expected_prefix="video/", label="Видео-референс")
    audio_b64 = await upload_one_to_b64(audio_file, expected_prefix="audio/", label="Аудио-референс")
    refs_count = len(images_b64) + (1 if video_b64 else 0) + (1 if audio_b64 else 0)

    generation_id = await db.create_generation(
        user_db_id=user["id"],
        model=model,
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        image_paths="uploaded" if refs_count else None,
        references_count=refs_count,
        duration=duration,
        resolution=resolution,
        ratio=ratio,
        quality=model_mode,
        seed=seed,
        with_audio=audio,
    )

    asyncio.create_task(
        run_generation(
            api_key=user["api_key"],
            telegram_id=int(user["telegram_id"]),
            generation_id=generation_id,
            model=model,
            prompt=prompt,
            negative_prompt=negative_prompt or None,
            images_b64=images_b64,
            video_b64=video_b64,
            audio_b64=audio_b64,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            audio=audio,
            quality=model_mode,
            seed=seed,
        )
    )

    return {
        "generation_id": generation_id,
        "status": "started",
        "model_mode": model_mode,
        "model": model,
        "estimated_cost": estimate_cost(model_mode, resolution, duration),
    }


async def run_generation(
    *,
    api_key: str,
    telegram_id: int,
    generation_id: int,
    model: str,
    prompt: str,
    negative_prompt: Optional[str],
    images_b64: List[str],
    video_b64: Optional[str],
    audio_b64: Optional[str],
    duration: int,
    resolution: str,
    ratio: str,
    audio: bool,
    quality: str,
    seed: Optional[int],
):
    client = AIGateClient(api_key)
    try:
        await db.update_generation_status(generation_id, "processing")
        await manager.send_progress(
            telegram_id,
            generation_id,
            "processing",
            "Отправили задачу в AIGate. Генерация может занять до 15 минут.",
            progress=18,
        )

        result = await client.generate_video(
            model=model,
            prompt=prompt,
            images_b64=images_b64 or None,
            video_b64=video_b64,
            audio_b64=audio_b64,
            duration=duration,
            resolution=resolution,
            aspect_ratio=ratio,
            audio=audio,
            quality=None,
            negative_prompt=negative_prompt,
            seed=seed,
        )

        source_url = extract_video_url(result)
        if not source_url:
            raise AIGateError("AIGate did not return a video URL", payload=result)

        await manager.send_progress(
            telegram_id,
            generation_id,
            "processing",
            "Видео готово. Сохраняем файл.",
            progress=86,
        )

        result_url = source_url
        try:
            video_bytes = await client.download_video_bytes(source_url)
            video_path = local_video_path(generation_id)
            await asyncio.to_thread(video_path.write_bytes, video_bytes)
            result_url = f"/media/generations/{generation_id}.mp4"
        except Exception as exc:
            await manager.send_progress(
                telegram_id,
                generation_id,
                "processing",
                f"Видео готово, но локальное сохранение не удалось: {exc}",
                progress=92,
            )

        message = "Готово!"
        if result.get("_multiref_fallback"):
            message = "Готово. AIGate не принял экспериментальные референсы, поэтому использовали официальный набор."

        await db.update_generation_status(generation_id, "completed", result_url=result_url)
        await manager.send_progress(
            telegram_id,
            generation_id,
            "completed",
            message,
            progress=100,
            result_url=result_url,
        )
    except AIGateError as exc:
        logger.warning("AIGate generation %s failed: %s", generation_id, exc, exc_info=True)
        await db.update_generation_status(generation_id, "failed", error_message=str(exc))
        await manager.send_progress(telegram_id, generation_id, "failed", str(exc), progress=100)
    except Exception as exc:
        logger.exception("Generation %s failed unexpectedly", generation_id)
        await db.update_generation_status(generation_id, "failed", error_message=str(exc))
        await manager.send_progress(telegram_id, generation_id, "failed", str(exc), progress=100)


@router.post("/api/history")
async def api_history(init_data: str = Form(...), limit: int = Form(12)):
    user = await get_or_create_user(init_data)
    limit = max(1, min(int(limit), 50))
    generations = await db.get_user_generations(int(user["telegram_id"]), limit=limit)
    return {"generations": generations}


@router.post("/api/setkey")
async def api_setkey(init_data: str = Form(...), api_key: str = Form(...)):
    user = await get_or_create_user(init_data)
    api_key = api_key.strip()
    if len(api_key) < 16:
        raise HTTPException(status_code=400, detail="Ключ выглядит слишком коротким")

    try:
        balance = await AIGateClient(api_key).get_balance()
        await db.set_user_api_key(int(user["telegram_id"]), api_key)
        return {"success": True, "balance": format_balance(balance), "raw": balance}
    except AIGateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, init_data: str = ""):
    try:
        user = validate_telegram_init_data(init_data)
        telegram_id = int(user["id"])
    except HTTPException:
        await websocket.close(code=1008)
        return

    await manager.connect(telegram_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(telegram_id, websocket)
