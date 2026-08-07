import io
import os
import re
import tempfile
import uuid

import boto3
from botocore.client import Config as BotoConfig
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from tts_gen import synth_segments

app = FastAPI(title="Puntersure TTS Service")

B2_ENDPOINT = os.environ.get("B2_ENDPOINT", "https://s3.us-east-005.backblazeb2.com")
B2_KEY_ID = os.environ.get("B2_KEY_ID", "")
B2_APP_KEY = os.environ.get("B2_APP_KEY", "")
B2_BUCKET = os.environ.get("B2_BUCKET", "")

MAX_TEXT = 4000
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "")


class TTSSegment(BaseModel):
    text: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    break_after_ms: int = 0


class TTSRequest(BaseModel):
    text: str | None = None
    filename: str = "audio"
    voice: str | None = None
    segments: list[TTSSegment] | None = None


def make_client():
    if not (B2_KEY_ID and B2_APP_KEY and B2_BUCKET):
        raise HTTPException(status_code=500, detail="B2 storage not configured")
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APP_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name=B2_ENDPOINT.rstrip("/").split(".")[-1],
    )


def slugify(name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return name[:60] or "audio"


def upload_to_b2(data: bytes, key: str, content_type: str) -> str:
    client = make_client()
    client.put_object(
        Bucket=B2_BUCKET,
        Key=key,
        Body=io.BytesIO(data),
        ContentType=content_type,
    )
    return key


def object_exists(key: str) -> bool:
    client = make_client()
    try:
        client.head_object(Bucket=B2_BUCKET, Key=key)
        return True
    except Exception:
        return False


def public_url(key: str) -> str:
    if PUBLIC_BASE:
        return f"{PUBLIC_BASE.rstrip('/')}/{key}"
    return f"{B2_ENDPOINT}/{B2_BUCKET}/{key}"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tts")
async def tts(req: TTSRequest):
    segments = [s for s in (req.segments or []) if (s.text or "").strip()]
    if not segments:
        if not (req.text or "").strip():
            raise HTTPException(status_code=400, detail="text or segments is required")
        segments = [TTSSegment(text=req.text.strip())]

    total_chars = sum(len(s.text) for s in segments)
    if total_chars > MAX_TEXT:
        raise HTTPException(
            status_code=400, detail=f"text too long (max {MAX_TEXT} chars)"
        )

    key = f"tts/{slugify(req.filename)}.mp3"
    url = public_url(key)

    # Cache hit: audio for this slug already exists in B2. Reuse it instead of
    # burning Render CPU time and B2 write transactions on a re-synthesis.
    try:
        if object_exists(key):
            return {"url": url, "bytes": 0, "duration_s": 0, "cached": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"B2 check failed: {exc}")

    voice = req.voice or os.environ.get("TTS_VOICE", "en-KE-AsiliaNeural")
    try:
        mp3 = await synth_segments(
            [
                {
                    "text": s.text,
                    "rate": s.rate,
                    "pitch": s.pitch,
                    "volume": s.volume,
                    "break_after_ms": s.break_after_ms,
                }
                for s in segments
            ],
            voice=voice,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {exc}")

    try:
        upload_to_b2(mp3, key, "audio/mpeg")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"B2 upload failed: {exc}")

    return {
        "url": url,
        "bytes": len(mp3),
        "duration_s": round(len(mp3) / 16000, 1),
        "cached": False,
    }
