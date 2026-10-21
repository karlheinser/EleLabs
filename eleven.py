"""کلاینت API سایت ElevenLabs."""
import re

import httpx

import config

API = "https://api.elevenlabs.io/v1"
_client: httpx.AsyncClient | None = None


class ElevenError(Exception):
    pass


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API,
            headers={"xi-api-key": config.ELEVENLABS_API_KEY},
            timeout=httpx.Timeout(300, connect=20),
        )
    return _client


async def close():
    if _client is not None:
        await _client.aclose()


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status") or str(detail)
            elif isinstance(detail, list):
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
        except Exception:
            detail = r.text
        raise ElevenError(f"{r.status_code}: {str(detail)[:300]}")
    return r


# ---------------- متن به صدا ----------------
def split_text(text: str, limit: int = 2500) -> list[str]:
    """متن طولانی را در مرز جمله‌ها تکه می‌کند."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[.!?؟。\n])\s+", text)
    chunks, cur = [], ""
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut])
            p = p[cut:].strip()
        if len(cur) + len(p) + 1 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def voice_settings(u) -> dict:
    s = {"stability": u["stability"], "similarity_boost": u["similarity"], "style": u["style"]}
    if (u["model_id"] or "") != "eleven_v3":
        s["speed"] = u["speed"]
        s["use_speaker_boost"] = True
    return s


async def tts(text: str, voice_id: str, model_id: str, settings: dict, lang: str = "auto") -> bytes:
    body = {"text": text, "model_id": model_id, "voice_settings": settings}
    if lang and lang != "auto" and "multilingual_v2" not in model_id:
        body["language_code"] = lang
    r = await client().post(f"/text-to-speech/{voice_id}",
                            params={"output_format": "mp3_44100_128"}, json=body)
    return _check(r).content


async def tts_long(text: str, voice_id: str, model_id: str, settings: dict, lang="auto") -> bytes:
    out = b""
    for chunk in split_text(text):
        out += await tts(chunk, voice_id, model_id, settings, lang)
    return out


async def dialogue(inputs: list[dict], model_id: str) -> bytes:
    r = await client().post("/text-to-dialogue", params={"output_format": "mp3_44100_128"},
                            json={"inputs": inputs, "model_id": model_id})
    return _check(r).content


# ---------------- صدا ----------------
async def stt(data: bytes, filename: str, lang: str = "auto") -> dict:
    form = {"model_id": config.STT_MODEL_ID}
    if lang and lang != "auto":
        form["language_code"] = lang
    r = await client().post("/speech-to-text", data=form, files={"file": (filename, data)})
    return _check(r).json()


async def voice_changer(data: bytes, filename: str, voice_id: str) -> bytes:
    r = await client().post(
        f"/speech-to-speech/{voice_id}",
        params={"output_format": "mp3_44100_128"},
        data={"model_id": config.STS_MODEL_ID, "remove_background_noise": "true"},
        files={"audio": (filename, data)},
    )
    return _check(r).content


async def isolate(data: bytes, filename: str) -> bytes:
    r = await client().post("/audio-isolation", files={"audio": (filename, data)})
    return _check(r).content


async def dub_create(data: bytes, filename: str, target_lang: str) -> str:
    r = await client().post(
        "/dubbing",
        data={"target_lang": target_lang, "source_lang": "auto", "num_speakers": "0",
              "watermark": "false", "name": filename[:50]},
        files={"file": (filename, data)},
    )
    return _check(r).json()["dubbing_id"]


async def dub_status(dubbing_id: str) -> dict:
    return _check(await client().get(f"/dubbing/{dubbing_id}")).json()


async def dub_download(dubbing_id: str, lang: str) -> tuple[bytes, str]:
    r = _check(await client().get(f"/dubbing/{dubbing_id}/audio/{lang}"))
    return r.content, r.headers.get("content-type", "")


# ---------------- ساخت صدا ----------------
async def clone_voice(name: str, samples: list[tuple[str, bytes]]) -> str:
    files = [("files", (fn, b)) for fn, b in samples]
    r = await client().post("/voices/add",
                            data={"name": name, "remove_background_noise": "true"}, files=files)
    return _check(r).json()["voice_id"]


async def design_voice(description: str) -> list[dict]:
    r = await client().post("/text-to-voice/design", json={
        "voice_description": description,
        "model_id": config.VOICE_DESIGN_MODEL_ID,
        "auto_generate_text": True,
    })
    return _check(r).json().get("previews", [])


async def save_designed_voice(name: str, description: str, generated_voice_id: str) -> str:
    r = await client().post("/text-to-voice", json={
        "voice_name": name, "voice_description": description,
        "generated_voice_id": generated_voice_id,
    })
    return _check(r).json()["voice_id"]


async def my_voices() -> list[dict]:
    return _check(await client().get("/voices")).json().get("voices", [])


async def delete_voice(voice_id: str):
    _check(await client().delete(f"/voices/{voice_id}"))


async def shared_voices(search: str = "", language: str = "", page_size: int = 8) -> list[dict]:
    params = {"page_size": page_size}
    if search:
        params["search"] = search
    if language:
        params["language"] = language
    return _check(await client().get("/shared-voices", params=params)).json().get("voices", [])


async def add_shared_voice(public_user_id: str, voice_id: str, name: str) -> str:
    r = await client().post(f"/voices/add/{public_user_id}/{voice_id}", json={"new_name": name})
    return _check(r).json().get("voice_id", voice_id)


# ---------------- افکت و موسیقی ----------------
async def sound_effect(prompt: str, duration: float | None) -> bytes:
    body = {"text": prompt, "prompt_influence": 0.3, "model_id": config.SFX_MODEL_ID}
    if duration:
        body["duration_seconds"] = duration
    r = await client().post("/sound-generation", params={"output_format": "mp3_44100_128"}, json=body)
    return _check(r).content


async def music(prompt: str, length_ms: int | None) -> bytes:
    body = {"prompt": prompt, "model_id": config.MUSIC_MODEL_ID}
    if length_ms:
        body["music_length_ms"] = length_ms
    r = await client().post("/music", json=body)
    return _check(r).content


# ---------------- حساب ----------------
async def subscription() -> dict:
    return _check(await client().get("/user/subscription")).json()
