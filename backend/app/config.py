"""Application configuration."""

import os
from typing import Any, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BOT_TOKEN: str = ""
    ADMIN_IDS: Any = Field(default_factory=list)

    DATABASE_PATH: str = "data/bot.db"
    MEDIA_DIR: str = "media"
    MAX_PROMPT_LENGTH: int = 3500
    MAX_UPLOAD_MB: int = 16
    MAX_IMAGE_REFERENCES: int = 6

    WEBAPP_HOST: str = "0.0.0.0"
    WEBAPP_PORT: int = Field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    WEBAPP_URL: str = "https://your-domain.vercel.app"
    REFERRAL_URL: str = "https://aigate.shop"
    CORS_ORIGINS: Any = Field(default_factory=list)

    PROVIDER_API_BASE: str = "https://api.aigate.shop/v1"
    TELEGRAM_AUTH_MAX_AGE_SECONDS: int = 86400

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Local UI preview only. Keep false on a real server.
    ALLOW_DEV_AUTH: bool = False
    DEV_TELEGRAM_ID: int = 100000001

    SUPPORTED_RATIOS: Any = Field(default_factory=lambda: ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"])
    SUPPORTED_RESOLUTIONS: Any = Field(default_factory=lambda: ["480p", "720p", "1080p"])
    FAST_MODEL_ID: str = "bytedance/seedance-2.0-fast"
    STANDARD_MODEL_ID: str = "bytedance/seedance-2.0"
    FAST_PRICE_480P: float = 0.01614
    FAST_PRICE_720P: float = 0.0363
    FAST_PRICE_1080P: float = 0.08166
    STANDARD_PRICE_480P: float = 0.020178
    STANDARD_PRICE_720P: float = 0.04536
    STANDARD_PRICE_1080P: float = 0.10206
    STANDARD_REFERENCE_PRICE_480P: float = 0.020178
    STANDARD_REFERENCE_PRICE_720P: float = 0.04536
    STANDARD_REFERENCE_PRICE_1080P: float = 0.10206

    SUPPORTED_DURATIONS: Any = Field(default_factory=lambda: list(range(4, 16)))
    SUPPORTED_QUALITIES: Any = Field(default_factory=lambda: ["fast", "standard"])

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_int_list(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "CORS_ORIGINS",
        "SUPPORTED_RATIOS",
        "SUPPORTED_RESOLUTIONS",
        "SUPPORTED_DURATIONS",
        "SUPPORTED_QUALITIES",
        mode="before",
    )
    @classmethod
    def parse_list(cls, value, info):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            if info.field_name == "SUPPORTED_DURATIONS":
                return [int(item) for item in items]
            return items
        return value

    @property
    def cors_origins(self) -> List[str]:
        origins = {
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        }
        if self.WEBAPP_URL:
            origins.add(self.WEBAPP_URL.rstrip("/"))
        origins.update(origin.rstrip("/") for origin in self.CORS_ORIGINS if origin)
        return sorted(origins)

    @property
    def model_modes(self):
        return {
            "fast": {
                "id": self.FAST_MODEL_ID,
                "reference_id": self.FAST_MODEL_ID,
                "supports_references": True,
                "label": "Fast",
                "description": "Быстрее и дешевле, подходит для потока.",
                "pricing": {
                    "480p": self.FAST_PRICE_480P,
                    "720p": self.FAST_PRICE_720P,
                    "1080p": self.FAST_PRICE_1080P,
                },
            },
            "standard": {
                "id": self.STANDARD_MODEL_ID,
                "reference_id": self.STANDARD_MODEL_ID,
                "supports_references": True,
                "label": "Standard",
                "description": "Качественнее, обычно дороже и дольше.",
                "pricing": {
                    "480p": self.STANDARD_PRICE_480P,
                    "720p": self.STANDARD_PRICE_720P,
                    "1080p": self.STANDARD_PRICE_1080P,
                },
                "reference_pricing": {
                    "480p": self.STANDARD_REFERENCE_PRICE_480P,
                    "720p": self.STANDARD_REFERENCE_PRICE_720P,
                    "1080p": self.STANDARD_REFERENCE_PRICE_1080P,
                },
            },
        }

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()
