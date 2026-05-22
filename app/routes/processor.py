from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, HttpUrl

from app.core.config import settings
from app.services.processor_service import ProcessorService


router = APIRouter(prefix="/media-processor", tags=["Media Processor"])


class ProcessRequest(BaseModel):
    sourceUrl: HttpUrl
    assetType: Literal["IMAGE", "VIDEO"]
    jobType: Literal[
        "IMAGE_BACKGROUND_REMOVAL",
        "VIDEO_BACKGROUND_REMOVAL",
        "IMAGE_WATERMARK_REMOVAL",
        "VIDEO_WATERMARK_REMOVAL",
        "IMAGE_ENHANCEMENT",
        "VIDEO_ENHANCEMENT",
    ]
    mimeType: str
    options: dict[str, Any] = {}


processor_service = ProcessorService(
    timeout_seconds=settings.MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
    max_download_mb=settings.MEDIA_MAX_DOWNLOAD_MB,
)


def verify_processor_key(authorization: str | None):
    if not settings.MEDIA_PROCESSOR_API_KEY:
        return

    expected = f"Bearer {settings.MEDIA_PROCESSOR_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid media processor authorization.")


def normalize_output_url(request: Request, output_url: str) -> str:
    if output_url.startswith("http://") or output_url.startswith("https://"):
        return output_url

    base_url = settings.MEDIA_PUBLIC_BASE_URL.rstrip("/") if settings.MEDIA_PUBLIC_BASE_URL else str(request.base_url).rstrip("/")
    return f"{base_url}{output_url}"


@router.get("/health")
async def processor_health():
    return {"status": "ok", "service": "python-media-processor"}


@router.post("/process")
async def process_media(request: Request, payload: ProcessRequest, authorization: str | None = Header(default=None)):
    verify_processor_key(authorization)
    result = processor_service.process(str(payload.sourceUrl), payload.assetType, payload.jobType, payload.mimeType, payload.options)

    if "outputUrl" in result:
        result["outputUrl"] = normalize_output_url(request, str(result["outputUrl"]))

    return result
