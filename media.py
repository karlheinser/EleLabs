"""تبدیل فرمت صدا با ffmpeg و استخراج متن از فایل."""
import asyncio
import io


async def _ffmpeg(data: bytes, *args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[:200]}")
    return out


async def to_ogg_voice(data: bytes) -> bytes:
    """ویس تلگرام باید OGG/Opus باشد."""
    return await _ffmpeg(data, "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "64k", "-f", "ogg")


async def to_mp3(data: bytes) -> bytes:
    return await _ffmpeg(data, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3")


def extract_text(data: bytes, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if name.endswith(".docx"):
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs).strip()
    for enc in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore").strip()
