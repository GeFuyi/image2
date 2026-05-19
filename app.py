from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from openai import AsyncOpenAI

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:8080/v1").rstrip("/")
DEFAULT_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("MODEL", "gpt-image-2").strip() or "gpt-image-2"
UPSTREAM_API = os.getenv("UPSTREAM_API", "responses").strip().lower() or "responses"
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT", "10")))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("sub2api-image2-proxy")


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _to_plain(item: Any) -> Any:
    if isinstance(item, (dict, list, tuple, str, int, float, bool)) or item is None:
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "dict"):
        return item.dict()
    return item


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _make_data_url(b64_value: str, mime_type: str = "image/png") -> str:
    if b64_value.startswith("data:"):
        return b64_value
    return f"data:{mime_type};base64,{b64_value}"


def _resolve_url(url: str, base_url: str = OPENAI_BASE_URL) -> str:
    if url.startswith(("http://", "https://", "data:")):
        return url
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


def _normalize_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if not base_url:
        return OPENAI_BASE_URL
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url 必须以 http:// 或 https:// 开头")
    return base_url


def _mime_from_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_data_url_from_base64(value: str) -> str | None:
    value = value.strip()
    if value.startswith("data:image/"):
        return value
    compact = "".join(value.split())
    if len(compact) < 80:
        return None
    try:
        decoded = base64.b64decode(compact, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not decoded.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")):
        return None
    return _make_data_url(compact, _mime_from_bytes(decoded[:16]))


async def _data_url_from_url(request: Request, url: str, base_url: str) -> str:
    resolved_url = _resolve_url(url, base_url)
    if resolved_url.startswith("data:"):
        return resolved_url
    response = await request.app.state.http_client.get(resolved_url)
    response.raise_for_status()
    mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
    return _make_data_url(base64.b64encode(response.content).decode("utf-8"), mime_type)


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(180.0, connect=10.0)
    limits = httpx.Limits(
        max_connections=MAX_CONCURRENT + 2,
        max_keepalive_connections=MAX_CONCURRENT + 2,
    )
    client = httpx.AsyncClient(timeout=timeout, limits=limits)
    app.state.http_client = client
    app.state.generation_gate = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "api_mode": UPSTREAM_API,
        "max_concurrent": MAX_CONCURRENT,
        "upstream": OPENAI_BASE_URL,
    }


async def _find_image_data_url(request: Request, item: Any, base_url: str) -> str | None:
    item = _to_plain(item)

    if isinstance(item, str):
        if item.startswith("data:image/"):
            return item
        return _image_data_url_from_base64(item)

    if isinstance(item, dict):
        for key in ("b64_json", "result", "image_base64", "partial_image_b64"):
            value = item.get(key)
            if isinstance(value, str):
                data_url = _image_data_url_from_base64(value)
                if data_url:
                    return data_url

        for key in ("url", "image_url"):
            value = item.get(key)
            if isinstance(value, str):
                return await _data_url_from_url(request, value, base_url)

        for value in item.values():
            data_url = await _find_image_data_url(request, value, base_url)
            if data_url:
                return data_url
        return None

    if isinstance(item, (list, tuple)):
        for value in item:
            data_url = await _find_image_data_url(request, value, base_url)
            if data_url:
                return data_url

    return None


async def _extract_image_payload(request: Request, result: Any, base_url: str) -> dict[str, Any]:
    image_data_url = await _find_image_data_url(request, result, base_url)
    if not image_data_url:
        raise RuntimeError("上游返回中没有可用的图片字段")

    mime_type = "image/png"
    if image_data_url.startswith("data:"):
        mime_type = image_data_url.split(";", 1)[0].replace("data:", "") or "image/png"

    return {
        "image_data_url": image_data_url,
        "mime_type": mime_type,
        "revised_prompt": _value(result, "revised_prompt"),
    }


async def _call_responses_api(client: AsyncOpenAI, prompt: str, image_bytes: bytes | None, content_type: str | None) -> Any:
    content = [{"type": "input_text", "text": prompt}]
    if image_bytes and content_type:
        image_data_url = _make_data_url(base64.b64encode(image_bytes).decode("utf-8"), content_type)
        content.append({"type": "input_image", "image_url": image_data_url})

    return await client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )


async def _call_images_generate_api(client: AsyncOpenAI, prompt: str) -> Any:
    return await client.images.generate(
        model=OPENAI_MODEL,
        prompt=prompt,
    )


async def _call_images_edit_api(client: AsyncOpenAI, prompt: str, image_bytes: bytes, content_type: str, filename: str) -> Any:
    image_file = (filename, io.BytesIO(image_bytes), content_type)
    return await client.images.edit(
        model=OPENAI_MODEL,
        image=image_file,
        prompt=prompt,
    )


@app.post("/api/generate")
async def generate(
    request: Request,
    prompt: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    image: Optional[UploadFile] = File(None),
) -> dict[str, Any]:
    prompt = prompt.strip()
    api_key = api_key.strip() or DEFAULT_OPENAI_API_KEY
    upstream_base_url = _normalize_base_url(base_url)
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API key")

    content_type: str | None = None
    image_bytes: bytes | None = None
    filename = "upload.png"

    if image and image.filename:
        content_type = _normalize_content_type(image.content_type)
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail="只支持 PNG、JPG、WEBP 图片")

        image_bytes = await image.read()
        if not image_bytes:
            image_bytes = None
            content_type = None
        elif len(image_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="图片过大，请压缩后重试")
        else:
            filename = image.filename or "upload.png"

    async with request.app.state.generation_gate:
        try:
            client = AsyncOpenAI(
                base_url=upstream_base_url,
                api_key=api_key,
                http_client=request.app.state.http_client,
            )
            if UPSTREAM_API in {"responses", "response"}:
                result = await _call_responses_api(client, prompt, image_bytes, content_type)
            elif UPSTREAM_API in {"images_edit", "image_edit", "edits", "images"}:
                if image_bytes and content_type:
                    result = await _call_images_edit_api(client, prompt, image_bytes, content_type, filename)
                else:
                    result = await _call_images_generate_api(client, prompt)
            else:
                raise HTTPException(status_code=500, detail="UPSTREAM_API 只能是 responses 或 images_edit")

            payload = await _extract_image_payload(request, result, upstream_base_url)
            payload["model"] = OPENAI_MODEL
            payload["api_mode"] = UPSTREAM_API
            return payload
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("upstream image generation failed")
            raise HTTPException(status_code=502, detail="上游图片生成失败") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
