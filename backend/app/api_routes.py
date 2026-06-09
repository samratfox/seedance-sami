"""FastAPI routes used by the Telegram Mini App."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

import aiohttp
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.api_client import AIGateClient, AIGateError, extract_video_url, format_balance
from app.config import settings
from app.database import db
from app.websocket import manager

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_NEGATIVE_CONSTRAINTS = (
    "no borders, no frames, no black bars, no letterbox, no pillarbox, no photo frame, "
    "no sticker outline, no white outline, no screenshot UI, fill the entire video frame edge to edge"
)


def cloudinary_configured() -> bool:
    return bool(settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET)


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


def form_text(form, key: str, default: str = "") -> str:
    value = form.get(key)
    if value is None:
        return default
    return str(value)


def form_int(form, key: str, default: int) -> int:
    value = form_text(form, key, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}: {value}") from None


def form_optional_int(form, key: str) -> Optional[int]:
    value = form_text(form, key).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}: {value}") from None


def form_bool(form, key: str, default: bool = False) -> bool:
    value = form_text(form, key, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on", "да"}


def form_files(form, key: str) -> List[UploadFile]:
    items = form.getlist(key)
    return [item for item in items if getattr(item, "filename", None)]


def normalize_reference_mentions(
    prompt: str,
    *,
    image_count: Optional[int] = None,
    has_video: Optional[bool] = None,
    has_audio: Optional[bool] = None,
) -> str:
    image_aliases: Dict[str, int] = {}

    def image_alias_replacer(match: re.Match) -> str:
        raw = match.group(1)
        number = int(raw)
        if image_count and 1 <= number <= image_count:
            return f"@Image{number}"
        if image_count:
            if raw not in image_aliases:
                next_index = len(image_aliases) + 1
                image_aliases[raw] = min(next_index, image_count)
            return f"@Image{image_aliases[raw]}"
        return f"@Image{number}"

    def video_alias_replacer(match: re.Match) -> str:
        if has_video:
            return "@Video1"
        return f"@Video{int(match.group(1))}"

    def audio_alias_replacer(match: re.Match) -> str:
        if has_audio:
            return "@Audio1"
        return f"@Audio{int(match.group(1))}"

    prompt = re.sub(
        r"@reference\s*(\d+)(?:\s*\[[^\]]+\])?",
        image_alias_replacer,
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = re.sub(r"@img[_\s-]*(\d+)", image_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@image[_\s-]*(\d+)", image_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@vid[_\s-]*(\d+)", video_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@video[_\s-]*(\d+)", video_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@aud[_\s-]*(\d+)", audio_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@audio[_\s-]*(\d+)", audio_alias_replacer, prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@video\s+reference(?:\s*\[[^\]]+\])?", "@Video1", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"@audio\s+reference(?:\s*\[[^\]]+\])?", "@Audio1", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"(@(?:Image|Video|Audio)\d+)\s*\[[^\]]+\]", r"\1", prompt, flags=re.IGNORECASE)
    return prompt


def normalize_reference_tags(
    prompt: str,
    image_count: int,
    has_video: bool,
    has_audio: bool,
    *,
    image_sheet: bool = False,
    audio_from_video: bool = False,
    lipsync_only: bool = False,
) -> str:
    prompt = normalize_reference_mentions(prompt, image_count=image_count, has_video=has_video, has_audio=has_audio)

    prefix_parts: List[str] = []

    # In pure lipsync mode the video is used only for audio — no visual image prefix needed.
    if not lipsync_only:
        if image_sheet:
            image_tags = ", ".join(f"Image{index}" for index in range(1, image_count + 1))
            prefix_parts.append(
                f"The input image is a labeled reference sheet containing {image_tags}; "
                "treat each panel as a separate visual reference and ignore labels, borders, and file names."
            )
        elif image_count:
            image_map = "; ".join(f"@Image{index}=uploaded image reference {index}" for index in range(1, image_count + 1))
            prefix_parts.append(
                f"Reference map: {image_map}. "
                "Treat each @ImageN as a separate source. Do not merge identities, outfits, objects, or instructions "
                "between different image references. When the prompt mentions @ImageN, apply only that referenced image "
                "to that specific character, object, scene beat, or instruction. Keep the referenced subject's full head, "
                "face, and important outfit details in frame unless the user explicitly asks for a close-up crop."
            )
        if image_count and not re.search(r"@Image\d+", prompt, flags=re.IGNORECASE):
            image_tags = ", ".join(f"@Image{index}" for index in range(1, image_count + 1))
            prefix_parts.append(f"Use {image_tags} as visual reference.")
        if has_video and not re.search(r"@Video\d+", prompt, flags=re.IGNORECASE):
            prefix_parts.append("Use @Video1 as motion/video reference.")

    # For lipsync with audio from video: instruct model to use @Video1 as audio source
    if audio_from_video and not re.search(r"@Video\d+", prompt, flags=re.IGNORECASE):
        prefix_parts.append(
            "Use @Video1 as the audio source for precise lip-sync. "
            "Match the character's lip movements exactly to the audio track from @Video1."
        )
    elif has_audio and not audio_from_video and not re.search(r"@Audio\d+", prompt, flags=re.IGNORECASE):
        prefix_parts.append("Use @Audio1 as audio/rhythm reference.")

    if prefix_parts:
        prompt = f"{' '.join(prefix_parts)} {prompt}"
    return prompt


def instruction_image_indexes(prompt: str, image_count: int) -> List[int]:
    if image_count <= 0:
        return []

    normalized = normalize_reference_mentions(prompt, image_count=image_count)
    patterns = [
        r"(?:instruction|instructions|prompt|script|text|rules|read|ocr|инструкц\w*|промпт\w*|текст\w*|прочит\w*)"
        r"(?:\s+from|\s+in|\s+on|\s+из|\s+с|\s+на|\s+по)?[^@]{0,60}@Image(\d+)",
        r"@Image(\d+)\s*(?:is|as|=|это|как)?\s*"
        r"(?:instruction|instructions|prompt|script|text|rules|read|ocr|инструкц\w*|промпт\w*|текст\w*|прочит\w*)",
    ]

    indexes = set()
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            index = int(match.group(1))
            if 1 <= index <= image_count:
                indexes.add(index)

    return sorted(indexes)


def clean_ocr_text(text: str) -> str:
    text = text.replace("\x0c", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


def ocr_image_upload(upload: Dict[str, str], index: int) -> str:
    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract is not installed; cannot OCR @Image%s", index)
        return ""

    try:
        image = Image.open(BytesIO(upload["content"]))
        image = ImageOps.exif_transpose(image).convert("L")
        resample = getattr(Image, "Resampling", Image).LANCZOS
        if image.width < 1600:
            scale = min(4, max(2, int(1600 / max(image.width, 1))))
            image = image.resize((image.width * scale, image.height * scale), resample)
        image = ImageOps.autocontrast(image)
        text = pytesseract.image_to_string(image, lang=settings.OCR_LANG, config="--psm 6")
        text = clean_ocr_text(text)
        if len(text) < settings.OCR_MIN_TEXT_CHARS:
            logger.info("OCR text from @Image%s is too short to use", index)
            return ""
        return text[: settings.OCR_MAX_CHARS_PER_IMAGE]
    except Exception as exc:
        logger.warning("Cannot OCR @Image%s: %s", index, exc)
        return ""


async def extract_instruction_texts_from_images(prompt: str, uploads: List[Dict[str, str]]) -> Dict[int, str]:
    if not settings.ENABLE_IMAGE_OCR:
        return {}

    indexes = instruction_image_indexes(prompt, len(uploads))
    if not indexes:
        return {}

    results = await asyncio.gather(
        *(asyncio.to_thread(ocr_image_upload, uploads[index - 1], index) for index in indexes),
        return_exceptions=True,
    )

    extracted: Dict[int, str] = {}
    for index, result in zip(indexes, results):
        if isinstance(result, Exception):
            logger.warning("OCR task failed for @Image%s: %s", index, result)
            continue
        if result:
            extracted[index] = result

    return extracted


def add_ocr_instruction_text(prompt: str, extracted: Dict[int, str]) -> str:
    if not extracted:
        return prompt

    header = (
        "\n\nExtracted OCR instruction text from referenced images. "
        "Use this text when the prompt says to use instructions/text from that image:\n"
    )
    available = settings.MAX_GENERATION_PROMPT_LENGTH - len(prompt) - len(header)
    if available < 180:
        raise HTTPException(
            status_code=400,
            detail=(
                "Промпт почти заполнил серверный лимит. Для чтения инструкции с картинки нужно оставить место "
                "под распознанный текст или уменьшить инструкцию."
            ),
        )

    blocks: List[str] = []
    remaining = available
    for index, text in sorted(extracted.items()):
        title = f"@Image{index} OCR text:\n"
        if remaining <= len(title) + 80:
            break
        chunk = text[: remaining - len(title)]
        blocks.append(f"{title}{chunk}")
        remaining -= len(title) + len(chunk) + 2

    if not blocks:
        return prompt
    joined_blocks = "\n\n".join(blocks)
    return f"{prompt}{header}{joined_blocks}"


def add_negative_constraints_to_prompt(prompt: str, negative_prompt: str) -> str:
    constraints = DEFAULT_NEGATIVE_CONSTRAINTS
    negative_prompt = (negative_prompt or "").strip()
    if negative_prompt:
        constraints = f"{negative_prompt}, {constraints}"
    return f"{prompt}\n\nNegative constraints / avoid: {constraints}"


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
        await file.seek(0)

    return encoded


async def upload_one_to_b64(
    file: Optional[UploadFile],
    *,
    expected_prefix: str,
    label: str,
) -> Optional[str]:
    values = await upload_to_b64([file] if file else None, limit=1, expected_prefix=expected_prefix, label=label)
    return values[0] if values else None


async def read_uploads(
    files: Optional[List[UploadFile]],
    *,
    limit: int,
    expected_prefix: str,
    label: str,
) -> List[Dict[str, str]]:
    uploads: List[Dict[str, str]] = []
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024

    for file in (files or [])[:limit]:
        if not file or not file.filename:
            continue
        content_type = file.content_type or expected_prefix
        if content_type and not content_type.startswith(expected_prefix):
            raise HTTPException(status_code=400, detail=f"РќРµРІРµСЂРЅС‹Р№ С‚РёРї С„Р°Р№Р»Р°: {label}")

        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"{label} Р±РѕР»СЊС€Рµ {settings.MAX_UPLOAD_MB} MB")

        uploads.append(
            {
                "filename": file.filename,
                "content_type": content_type,
                "content": content,
                "b64": base64.b64encode(content).decode("utf-8"),
            }
        )
        await file.seek(0)

    return uploads


def build_image_reference_uploads(uploads: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if len(uploads) <= 1:
        return uploads

    sheet = build_image_reference_sheet(uploads[: settings.MAX_IMAGE_REFERENCES])
    return [sheet]


def image_reference_sheet_enabled() -> bool:
    return settings.MULTI_IMAGE_REFERENCE_MODE.strip().lower() in {"sheet", "reference_sheet", "combined"}


def parse_aspect_ratio(ratio: str) -> float:
    try:
        left, right = (ratio or "16:9").split(":", 1)
        width = float(left.strip())
        height = float(right.strip())
        if width > 0 and height > 0:
            return width / height
    except Exception:
        pass
    return 16 / 9


def pil_image_upload(image: Image.Image, filename: str, *, quality: int = 94) -> Dict[str, str]:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    content = output.getvalue()
    return {
        "filename": filename,
        "content_type": "image/jpeg",
        "content": content,
        "b64": base64.b64encode(content).decode("utf-8"),
    }


def strip_phone_screenshot_ui(source: Image.Image) -> Tuple[Image.Image, bool]:
    if not settings.CROP_PHONE_SCREENSHOT_UI:
        return source, False

    width, height = source.size
    if width < 360 or height < 700 or height / max(width, 1) < 1.65:
        return source, False

    gray = source.convert("L")
    resample = getattr(Image, "Resampling", Image).BILINEAR
    row_luma = [value for value in gray.resize((1, height), resample).getdata()]
    lower_start = int(height * 0.55)
    lower_mean = sum(row_luma[lower_start:]) / max(1, height - lower_start)
    if lower_mean > 75:
        return source, False

    dark_threshold = 38
    min_band = 8
    start = int(height * 0.30)
    end = int(height * 0.72)
    crop_y = None
    for y in range(start, max(start, end - min_band)):
        if all(row_luma[y + offset] < dark_threshold for offset in range(min_band)):
            above_start = max(0, y - 80)
            above_mean = sum(row_luma[above_start:y]) / max(1, y - above_start)
            if above_mean > lower_mean + 10:
                crop_y = y
                break

    if not crop_y or crop_y < int(height * 0.35):
        return source, False

    cropped = source.crop((0, 0, width, crop_y))
    logger.info("Cropped phone screenshot UI from image reference: %sx%s -> %sx%s", width, height, width, crop_y)
    return cropped, True


def make_no_crop_reference(upload: Dict[str, str], ratio: str, index: int) -> Dict[str, str]:
    try:
        source = Image.open(BytesIO(upload["content"]))
        source = ImageOps.exif_transpose(source).convert("RGB")
        source, cropped_ui = strip_phone_screenshot_ui(source)
    except Exception as exc:
        logger.warning("Cannot prepare image reference %s: %s", index, exc)
        return upload

    mode = settings.REFERENCE_PAD_MODE.strip().lower()
    if not settings.PREPARE_IMAGE_REFERENCES or mode in {"raw", "original", "none"}:
        if cropped_ui:
            return pil_image_upload(source, f"reference_{index}_clean.jpg")
        return upload

    target_ratio = parse_aspect_ratio(ratio)
    long_edge = max(512, int(settings.REFERENCE_CANVAS_LONG_EDGE))
    if target_ratio >= 1:
        canvas_w = long_edge
        canvas_h = max(1, round(long_edge / target_ratio))
    else:
        canvas_h = long_edge
        canvas_w = max(1, round(long_edge * target_ratio))

    resample = getattr(Image, "Resampling", Image).LANCZOS
    if mode in {"cover", "crop", "fill"}:
        covered = ImageOps.fit(source.copy(), (canvas_w, canvas_h), method=resample, centering=(0.5, 0.45))
        return pil_image_upload(covered, f"reference_{index}_cover.jpg")

    fit = source.copy()
    fit.thumbnail((canvas_w, canvas_h), resample)

    if mode == "blur":
        background = ImageOps.fit(source.copy(), (canvas_w, canvas_h), method=resample, centering=(0.5, 0.5))
        try:
            from PIL import ImageFilter

            background = background.filter(ImageFilter.GaussianBlur(radius=28))
        except Exception:
            pass
    else:
        background = Image.new("RGB", (canvas_w, canvas_h), "#111111")

    x = (canvas_w - fit.width) // 2
    y = (canvas_h - fit.height) // 2
    background.paste(fit, (x, y))
    return pil_image_upload(background, f"reference_{index}_no_crop.jpg")


def prepare_image_reference_uploads(uploads: List[Dict[str, str]], ratio: str) -> List[Dict[str, str]]:
    return [make_no_crop_reference(upload, ratio, index) for index, upload in enumerate(uploads, start=1)]


def build_image_reference_sheet(uploads: List[Dict[str, str]]) -> Dict[str, str]:
    count = len(uploads)
    columns = 2 if count <= 4 else 3
    rows = (count + columns - 1) // columns
    tile_w = 640
    tile_h = 640
    label_h = 54
    gap = 18
    padding = 24
    sheet_w = padding * 2 + columns * tile_w + (columns - 1) * gap
    sheet_h = padding * 2 + rows * (tile_h + label_h) + (rows - 1) * gap

    sheet = Image.new("RGB", (sheet_w, sheet_h), "#111111")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()

    resample = getattr(Image, "Resampling", Image).LANCZOS
    for index, upload in enumerate(uploads):
        row = index // columns
        col = index % columns
        x = padding + col * (tile_w + gap)
        y = padding + row * (tile_h + label_h + gap)

        frame = Image.new("RGB", (tile_w, tile_h), "#1b1a17")
        try:
            image = Image.open(BytesIO(upload["content"]))
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((tile_w, tile_h), resample)
            px = (tile_w - image.width) // 2
            py = (tile_h - image.height) // 2
            frame.paste(image, (px, py))
        except Exception as exc:
            logger.warning("Cannot add image reference %s to sheet: %s", index + 1, exc)

        sheet.paste(frame, (x, y + label_h))
        draw.rectangle((x, y, x + tile_w, y + label_h), fill="#082f2e")
        draw.rectangle((x, y, x + tile_w - 1, y + tile_h + label_h - 1), outline="#2dd4bf", width=3)
        draw.text((x + 18, y + 12), f"Image{index + 1}", fill="#ffffff", font=font)

    output = BytesIO()
    sheet.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    content = output.getvalue()
    return {
        "filename": "reference_sheet.jpg",
        "content_type": "image/jpeg",
        "content": content,
        "b64": base64.b64encode(content).decode("utf-8"),
    }


def normalize_video_reference_mode(value: str) -> str:
    normalized = (value or settings.VIDEO_REFERENCE_MODE or "motion").strip().lower()
    if normalized in {"motion", "movement", "visual", "video", "direct"}:
        return "motion"
    if normalized in {"lipsync", "lip_sync", "lip-sync", "audio", "voice"}:
        return "lipsync"
    if normalized in {"motion_lipsync", "motion-lipsync", "motion+lip-sync", "motion+audio", "both", "clip"}:
        return "motion_lipsync"
    raise HTTPException(status_code=400, detail=f"Unsupported video_reference_mode: {value}")


def visual_video_references_enabled(video_reference_mode: str) -> bool:
    return normalize_video_reference_mode(video_reference_mode) in {"motion", "motion_lipsync"}


async def extract_audio_from_video_upload(upload: Dict[str, str], duration: int) -> Optional[Dict[str, str]]:
    if not settings.EXTRACT_AUDIO_FROM_VIDEO:
        return None

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg is not installed; cannot extract audio from video reference")
        return None

    with tempfile.TemporaryDirectory(prefix="aigate_video_audio_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "source_video"
        output_path = tmp_dir / "audio_reference.mp3"
        input_path.write_bytes(upload["content"])

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-t",
            str(max(1, min(int(duration or 15), 15))),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            logger.warning("ffmpeg audio extraction timed out")
            return None

        if process.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            logger.warning("ffmpeg audio extraction failed: %s", stderr.decode("utf-8", errors="ignore")[:600])
            return None

        content = output_path.read_bytes()
        return {
            "filename": "video_audio_reference.mp3",
            "content_type": "audio/mpeg",
            "content": content,
            "b64": base64.b64encode(content).decode("utf-8"),
        }


def cloudinary_signature(params: Dict[str, str]) -> str:
    raw = "&".join(f"{key}={value}" for key, value in sorted(params.items()) if value)
    return hashlib.sha1(f"{raw}{settings.CLOUDINARY_API_SECRET}".encode("utf-8")).hexdigest()


async def verify_public_url(url: str) -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"}
    last_status = ""
    for _ in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status < 400:
                        return
                    last_status = str(response.status)
        except Exception as exc:
            last_status = str(exc)
        await asyncio.sleep(0.5)
    raise HTTPException(status_code=500, detail=f"Cloudinary URL is not public: {last_status}")


async def upload_refs_to_cloudinary(
    uploads: List[Dict[str, str]],
    *,
    resource_type: str,
    prefix: str,
) -> List[str]:
    if not uploads:
        return []
    if not cloudinary_configured():
        raise HTTPException(status_code=500, detail="Cloudinary credentials are required for references")

    urls: List[str] = []
    timeout = aiohttp.ClientTimeout(total=90)
    upload_url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/{resource_type}/upload"

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for upload in uploads:
            public_id = f"aigate_refs/{prefix}_{int(time.time())}_{uuid.uuid4().hex[:12]}"
            params = {"public_id": public_id, "timestamp": str(int(time.time()))}

            data = aiohttp.FormData()
            suffix = upload_suffix(upload)
            data.add_field(
                "file",
                upload["content"],
                filename=f"{prefix}{suffix}",
                content_type=upload["content_type"] or "application/octet-stream",
            )
            data.add_field("api_key", settings.CLOUDINARY_API_KEY)
            data.add_field("public_id", params["public_id"])
            data.add_field("timestamp", params["timestamp"])
            data.add_field("signature", cloudinary_signature(params))

            async with session.post(upload_url, data=data) as response:
                text = await response.text()
                if response.status >= 400:
                    raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {text}")
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {text}") from None

            url = str(result.get("secure_url") or "").strip()
            if not url.startswith("https://"):
                raise HTTPException(status_code=500, detail="Cloudinary did not return a public https URL")
            await verify_public_url(url)
            urls.append(url)

    return urls


async def try_upload_refs_to_cloudinary(
    uploads: List[Dict[str, str]],
    *,
    resource_type: str,
    prefix: str,
) -> List[str]:
    try:
        return await upload_refs_to_cloudinary(uploads, resource_type=resource_type, prefix=prefix)
    except HTTPException as exc:
        logger.warning("Cloudinary upload skipped for %s: %s", prefix, exc.detail)
        return []


def local_video_path(generation_id: int) -> Path:
    directory = Path(settings.MEDIA_DIR) / "generations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{generation_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"


def asset_url(path: str) -> str:
    path = str(path or "").replace("\\", "/").lstrip("/")
    return f"/media/{path}"


def serialize_reference_asset(item: Dict) -> Dict:
    return {
        "id": item["id"],
        "kind": item["kind"],
        "filename": item["filename"],
        "content_type": item.get("content_type"),
        "size": item.get("size") or 0,
        "url": item.get("remote_url") or asset_url(item["path"]),
        "created_at": item["created_at"],
    }


def upload_suffix(upload: Dict[str, str]) -> str:
    filename = upload.get("filename") or ""
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix[:12]
    content_type = (upload.get("content_type") or "").lower()
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type == "video/mp4":
        return ".mp4"
    if content_type in {"audio/mpeg", "audio/mp3"}:
        return ".mp3"
    if content_type == "audio/wav":
        return ".wav"
    return ".bin"


async def save_reference_assets(user_db_id: int, uploads: List[Dict[str, str]], kind: str) -> List[int]:
    if not uploads:
        return []

    directory = Path(settings.MEDIA_DIR) / "reference_assets" / str(user_db_id) / kind
    directory.mkdir(parents=True, exist_ok=True)

    resource_type = "image" if kind == "image" else "video"
    remote_urls = await try_upload_refs_to_cloudinary(uploads, resource_type=resource_type, prefix=f"library_{kind}")

    asset_ids: List[int] = []
    for index, upload in enumerate(uploads):
        content = upload.get("content") or b""
        if not content:
            continue

        digest = hashlib.sha256(content).hexdigest()
        filename = upload.get("filename") or f"{kind}_reference{upload_suffix(upload)}"
        stored_name = f"{uuid.uuid4().hex}{upload_suffix(upload)}"
        stored_path = directory / stored_name
        await asyncio.to_thread(stored_path.write_bytes, content)

        relative_path = stored_path.relative_to(Path(settings.MEDIA_DIR)).as_posix()
        asset_id = await db.create_reference_asset(
            user_db_id=user_db_id,
            kind=kind,
            filename=filename,
            content_type=upload.get("content_type"),
            path=relative_path,
            remote_url=remote_urls[index] if index < len(remote_urls) else None,
            size=len(content),
            sha256=digest,
        )
        asset_ids.append(asset_id)

    return asset_ids


def public_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    base = settings.WEBAPP_URL.rstrip("/")
    return f"{base}{url if url.startswith('/') else f'/{url}'}" if base else url


def prompt_preview(prompt: str, limit: int = 520) -> str:
    prompt = " ".join(prompt.split())
    if len(prompt) <= limit:
        return prompt
    return f"{prompt[:limit].rstrip()}..."


async def send_telegram_message(
    telegram_id: int,
    text: str,
    *,
    result_url: str = "",
) -> None:
    if not settings.BOT_TOKEN:
        return

    reply_markup = None
    buttons = []
    if result_url:
        buttons.append([{"text": "Скачать видео", "url": result_url}])
    if buttons:
        reply_markup = {"inline_keyboard": buttons}

    payload = {
        "chat_id": telegram_id,
        "text": text[:3900],
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
                json=payload,
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    logger.warning("Telegram notification failed: %s", data)
    except Exception:
        logger.exception("Could not send Telegram notification")


async def notify_generation_completed(
    *,
    telegram_id: int,
    generation_id: int,
    result_url: str,
    model: str,
    quality: str,
    duration: int,
    resolution: str,
    ratio: str,
    audio: bool,
    refs_count: int,
    prompt: str,
) -> None:
    result_url = public_url(result_url)
    text = (
        "Видео готово\n\n"
        f"Задача: #{generation_id}\n"
        f"Режим: {quality} ({model})\n"
        f"Параметры: {duration} c / {resolution} / {ratio}\n"
        f"Звук: {'да' if audio else 'нет'}\n"
        f"Референсы: {refs_count}\n\n"
        f"Промпт: {prompt_preview(prompt)}\n\n"
        f"Ссылка: {result_url}"
    )
    await send_telegram_message(telegram_id, text, result_url=result_url)


async def notify_generation_failed(
    *,
    telegram_id: int,
    generation_id: int,
    model: str,
    quality: str,
    duration: int,
    resolution: str,
    ratio: str,
    audio: bool,
    refs_count: int,
    prompt: str,
    error: str,
) -> None:
    text = (
        "Генерация не удалась\n\n"
        f"Задача: #{generation_id}\n"
        f"Режим: {quality} ({model})\n"
        f"Параметры: {duration} c / {resolution} / {ratio}\n"
        f"Звук: {'да' if audio else 'нет'}\n"
        f"Референсы: {refs_count}\n\n"
        f"Ошибка: {error}\n\n"
        f"Промпт: {prompt_preview(prompt)}"
    )
    await send_telegram_message(telegram_id, text)


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
        "max_generation_prompt_length": settings.MAX_GENERATION_PROMPT_LENGTH,
        "max_reference_assets": settings.MAX_REFERENCE_ASSETS,
        "video_reference_modes": ["motion", "lipsync", "motion_lipsync"],
        "default_video_reference_mode": normalize_video_reference_mode(settings.VIDEO_REFERENCE_MODE),
    }


def estimate_cost(model_mode: str, resolution: str, duration: int, *, with_references: bool = False) -> float:
    mode = settings.model_modes[model_mode]
    pricing = mode.get("reference_pricing") if with_references else mode.get("pricing")
    pricing = pricing or {}
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


@router.post("/api/reference-assets")
async def api_reference_assets(init_data: str = Form(...), limit: int = Form(80)):
    user = await get_or_create_user(init_data)
    limit = max(1, min(int(limit), settings.MAX_REFERENCE_ASSETS))
    assets = await db.get_user_reference_assets(int(user["telegram_id"]), limit=limit)
    return {
        "assets": [serialize_reference_asset(item) for item in assets]
    }


@router.post("/api/generate")
async def api_generate(request: Request):
    form = await request.form()
    init_data = form_text(form, "init_data")
    model_mode = form_text(form, "model_mode", "fast")
    prompt = form_text(form, "prompt")
    duration = form_int(form, "duration", 5)
    resolution = form_text(form, "resolution", "720p")
    ratio = form_text(form, "ratio", "16:9")
    audio = form_bool(form, "audio", False)
    video_reference_mode = normalize_video_reference_mode(form_text(form, "video_reference_mode", settings.VIDEO_REFERENCE_MODE))
    negative_prompt = form_text(form, "negative_prompt")
    seed = form_optional_int(form, "seed")
    image_files = form_files(form, "image_files")
    video_file = next(iter(form_files(form, "video_file")), None)
    audio_file = next(iter(form_files(form, "audio_file")), None)

    user = await get_or_create_user(init_data)
    if not user.get("api_key"):
        raise HTTPException(status_code=400, detail="Сначала подключите API-ключ AIGate")

    source_prompt = prompt.strip()
    source_negative_prompt = negative_prompt.strip()
    prompt = source_prompt
    negative_prompt = source_negative_prompt
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

    image_uploads = await read_uploads(image_files, limit=settings.MAX_IMAGE_REFERENCES, expected_prefix="image/", label="image reference")
    video_uploads = await read_uploads([video_file] if video_file else None, limit=1, expected_prefix="video/", label="video reference")
    audio_uploads = await read_uploads([audio_file] if audio_file else None, limit=1, expected_prefix="audio/", label="audio reference")
    image_asset_ids = await save_reference_assets(user["id"], image_uploads, "image")
    video_asset_ids = await save_reference_assets(user["id"], video_uploads, "video")
    audio_asset_ids = await save_reference_assets(user["id"], audio_uploads, "audio")
    reference_asset_ids = image_asset_ids + video_asset_ids + audio_asset_ids

    source_image_count = len(image_uploads)
    instruction_indexes = instruction_image_indexes(prompt, source_image_count)
    ocr_instruction_texts = await extract_instruction_texts_from_images(prompt, image_uploads)
    missing_ocr_indexes = [index for index in instruction_indexes if index not in ocr_instruction_texts]
    if missing_ocr_indexes:
        missing = ", ".join(f"@Image{index}" for index in missing_ocr_indexes)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Не удалось прочитать текст инструкции из {missing}. "
                "Загрузите более чёткую картинку или вставьте текст инструкции в промпт."
            ),
        )

    explicit_audio_count = len(audio_uploads)
    use_image_sheet = image_reference_sheet_enabled()
    prepared_image_uploads = prepare_image_reference_uploads(image_uploads, ratio)
    image_payload_uploads = build_image_reference_uploads(prepared_image_uploads) if use_image_sheet else prepared_image_uploads
    use_visual_video = visual_video_references_enabled(video_reference_mode)

    # === FINAL LIPSYNC LOGIC (video as input_video) ===
    extracted_audio_upload = None
    audio_from_video = False
    video_payload_uploads = video_uploads

    if video_uploads and settings.EXTRACT_AUDIO_FROM_VIDEO:
        if video_reference_mode in ("lipsync", "motion_lipsync"):
            extracted_audio_upload = await extract_audio_from_video_upload(video_uploads[0], duration)
            if extracted_audio_upload:
                audio_uploads = [extracted_audio_upload] if not audio_uploads else audio_uploads + [extracted_audio_upload]
                audio_from_video = True

    final_audio_param = False if (audio_from_video or audio_uploads) else audio
    audio = final_audio_param
    is_lipsync_mode = video_reference_mode in ("lipsync", "motion_lipsync")

    # Do not inject any prompt text for lipsync modes — the user's prompt is used as-is.

    image_urls = await try_upload_refs_to_cloudinary(image_payload_uploads, resource_type="image", prefix="image")
    video_urls = await try_upload_refs_to_cloudinary(video_payload_uploads, resource_type="video", prefix="video")
    audio_urls = await try_upload_refs_to_cloudinary(audio_uploads, resource_type="raw", prefix="audio")

    images_b64 = [item["b64"] for item in image_payload_uploads] or images_b64
    video_b64 = video_payload_uploads[0]["b64"] if video_payload_uploads else None
    audio_b64 = audio_uploads[0]["b64"] if audio_uploads else audio_b64
    video_url = video_urls[0] if video_urls else None
    audio_url = audio_urls[0] if audio_urls else None
    # No hard requirement for Cloudinary URLs — b64 fallback is used if URLs unavailable.
    refs_count = source_image_count + (1 if video_uploads else 0) + (1 if explicit_audio_count else 0)
    prompt = normalize_reference_tags(
        prompt,
        source_image_count,
        bool(video_url or video_b64),
        bool(audio_url or audio_b64),
        image_sheet=use_image_sheet and source_image_count > 1,
        audio_from_video=audio_from_video,
        lipsync_only=(video_reference_mode == "lipsync"),
    )
    prompt = add_ocr_instruction_text(prompt, ocr_instruction_texts)
    prompt = add_negative_constraints_to_prompt(prompt, negative_prompt)
    if len(prompt) > settings.MAX_GENERATION_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Промпт после OCR/negative длиннее {settings.MAX_GENERATION_PROMPT_LENGTH} символов",
        )

    if refs_count:
        if not (image_urls or images_b64 or video_url or video_b64 or audio_url or audio_b64):
            raise HTTPException(
                status_code=400,
                detail="Для референсной генерации нужен хотя бы фото- или видео-референс.",
            )

        logger.info(
            "Using catalog model with references: selected_mode=%s model=%s source_images=%s image_payloads=%s "
            "image_urls=%s image_b64=%s video_url=%s video_b64=%s audio_url=%s audio_b64=%s",
            model_mode,
            model,
            source_image_count,
            len(image_payload_uploads),
            len(image_urls),
            len(images_b64),
            bool(video_url),
            bool(video_b64),
            bool(audio_url),
            bool(audio_b64),
        )

    generation_id = await db.create_generation(
        user_db_id=user["id"],
        model=model,
        source_prompt=source_prompt,
        source_negative_prompt=source_negative_prompt or None,
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        image_paths="uploaded" if refs_count else None,
        references_count=refs_count,
        duration=duration,
        resolution=resolution,
        ratio=ratio,
        quality=model_mode,
        video_reference_mode=video_reference_mode,
        seed=seed,
        reference_asset_ids=json.dumps(reference_asset_ids) if reference_asset_ids else None,
        with_audio=audio,
    )

    asyncio.create_task(
        run_generation(
            api_key=user["api_key"],
            telegram_id=int(user["telegram_id"]),
            generation_id=generation_id,
            model=model,
            prompt=prompt,
            source_prompt=source_prompt,
            negative_prompt=negative_prompt or None,
            image_urls=image_urls or None,
            images_b64=images_b64,
            video_url=video_url,
            video_b64=video_b64,
            audio_url=audio_url,
            audio_b64=audio_b64,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            audio=audio,
            quality=model_mode,
            seed=seed,
            refs_count=refs_count,
        )
    )

    return {
        "generation_id": generation_id,
        "status": "started",
        "model_mode": model_mode,
        "model": model,
        "estimated_cost": estimate_cost(model_mode, resolution, duration, with_references=bool(refs_count)),
        "video_reference_mode": video_reference_mode,
    }


async def run_generation(
    *,
    api_key: str,
    telegram_id: int,
    generation_id: int,
    model: str,
    prompt: str,
    source_prompt: str,
    negative_prompt: Optional[str],
    image_urls: Optional[List[str]],
    images_b64: List[str],
    video_url: Optional[str],
    video_b64: Optional[str],
    audio_url: Optional[str],
    audio_b64: Optional[str],
    duration: int,
    resolution: str,
    ratio: str,
    audio: bool,
    quality: str,
    seed: Optional[int],
    refs_count: int,
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
            image_urls=image_urls,
            images_b64=images_b64 or None,
            video_url=video_url,
            video_b64=video_b64,
            audio_url=audio_url,
            audio_b64=audio_b64,
            duration=duration,
            resolution=resolution,
            aspect_ratio=ratio,
            audio=audio,
            quality=None,
            negative_prompt=negative_prompt,
            seed=seed,
            is_lipsync=is_lipsync_mode,
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
            result_url = f"/media/generations/{video_path.name}"
        except Exception as exc:
            await manager.send_progress(
                telegram_id,
                generation_id,
                "processing",
                f"Видео готово, но локальное сохранение не удалось: {exc}",
                progress=92,
            )

        message = "Готово!"

        await db.update_generation_status(generation_id, "completed", result_url=result_url)
        await manager.send_progress(
            telegram_id,
            generation_id,
            "completed",
            message,
            progress=100,
            result_url=result_url,
        )
        await notify_generation_completed(
            telegram_id=telegram_id,
            generation_id=generation_id,
            result_url=result_url,
            model=model,
            quality=quality,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            audio=audio,
            refs_count=refs_count,
            prompt=source_prompt,
        )
    except AIGateError as exc:
        logger.warning("AIGate generation %s failed: %s", generation_id, exc, exc_info=True)
        await db.update_generation_status(generation_id, "failed", error_message=str(exc))
        await manager.send_progress(telegram_id, generation_id, "failed", str(exc), progress=100)
        await notify_generation_failed(
            telegram_id=telegram_id,
            generation_id=generation_id,
            model=model,
            quality=quality,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            audio=audio,
            refs_count=refs_count,
            prompt=source_prompt,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Generation %s failed unexpectedly", generation_id)
        await db.update_generation_status(generation_id, "failed", error_message=str(exc))
        await manager.send_progress(telegram_id, generation_id, "failed", str(exc), progress=100)
        await notify_generation_failed(
            telegram_id=telegram_id,
            generation_id=generation_id,
            model=model,
            quality=quality,
            duration=duration,
            resolution=resolution,
            ratio=ratio,
            audio=audio,
            refs_count=refs_count,
            prompt=source_prompt,
            error=str(exc),
        )


@router.post("/api/history")
async def api_history(init_data: str = Form(...), limit: int = Form(12)):
    user = await get_or_create_user(init_data)
    limit = max(1, min(int(limit), 50))
    generations = await db.get_user_generations(int(user["telegram_id"]), limit=limit)
    for generation in generations:
        raw_asset_ids = generation.get("reference_asset_ids")
        asset_ids: List[int] = []
        try:
            loaded_ids = json.loads(raw_asset_ids or "[]")
            if isinstance(loaded_ids, list):
                asset_ids = [int(asset_id) for asset_id in loaded_ids if str(asset_id).isdigit()]
        except (TypeError, ValueError, json.JSONDecodeError):
            asset_ids = []

        assets = await db.get_reference_assets_by_ids(int(user["id"]), asset_ids)
        generation["reference_assets"] = [serialize_reference_asset(item) for item in assets]
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
