"""Bounded cloud synthesis with persistent reservations and no blind retries."""

import array
import asyncio
import hashlib
import io
import json
import math
import re
import sys
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


def declined_voice(text):
    """Recognize a user's request for text instead of short voice messages.

    Args:
        text: Current user message.

    Returns:
        Whether the user declines voice output.
    """
    return bool(
        re.search(
            r"(?:不要|不用|不必|别|不发|不想|不方便听|不能听|没法听).{0,4}(?:语音|声音|录音)|打字就好|文字就好|只发文字",
            str(text),
        )
    )


def requested_voice(text, recent=()):
    """Recognize an explicit one-turn voice request, honoring a later correction.

    Args:
        text: Current merged user message, never conversation history.
        recent: Recent public user-message strings for resolving a short follow-up.

    Returns:
        Whether this turn explicitly requests a voice reply.
    """
    message = str(text).strip()
    if declined_voice(message):
        return False
    if re.search(
        r"(?:发|来|用|听|要|想听|给我).{0,6}(?:语音|声音|录音)|"
        r"(?:语音|声音|录音).{0,4}(?:说|回|回复|回答|聊|发)",
        message,
    ):
        return True
    if re.search(
        r"(?:想听|让我听|给我听|听听).{0,6}(?:你|梦雨).{0,4}(?:说|声音|念|叫)|"
        r"(?:你|梦雨).{0,2}(?:说句话|说一句|说两句|说点什么).{0,4}(?:听|给我|让我)",
        message,
    ):
        return True
    if re.search(
        r"(?:说句|说一句|说两句|说点什么|念一下|读一下).{0,6}(?:听|给我听|让我听)",
        message,
    ):
        return True
    if re.fullmatch(
        r"[，,。！!\s]*(?:叫我一?声|喊我一?声|叫我的名字|说说看|说句话|说一句)[啊呀吧呗哦嘛呢，,。！!\s]*",
        message,
    ):
        return True
    trial = r"试试|试一下|试一句|试一条|听听|来一句|来一条"
    if "语音" in message and re.search(trial, message):
        return True
    if re.fullmatch(
        r"(?:那|好|嗯|你|可以|就|再|给我|来|啊|呀|吧|呗|哈|哦|[，,。！!\s])*"
        r"(?:试试|试一下|试一句|试一条|听听|来一句|来一条)"
        r"(?:啊|呀|吧|呗|哈|哦|[，,。！!\s])*",
        message,
    ) and any("语音" in str(item) for item in recent):
        return True
    return bool(
        re.search(
            r"(?:用|发|来|给我听).{0,6}(?:语音|声音)|(?:语音|声音).{0,3}(?:说|回复|回答|聊)",
            message,
        )
    )


def pad_wav(raw, lead=0.15, tail=0.6):
    """Add silence around a WAV clip so the line does not start or stop abruptly.

    Args:
        raw: Complete WAV bytes from the voice API.
        lead: Seconds of silence before the first syllable.
        tail: Seconds of silence after the last syllable.

    Returns:
        WAV bytes with the requested silence added.
    """
    with wave.open(io.BytesIO(raw), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    width = params.sampwidth * params.nchannels
    rate = params.framerate

    def silence(seconds):
        return b"\x00" * max(0, int(rate * float(seconds))) * width

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(rate)
        target.writeframes(silence(lead) + frames + silence(tail))
    return output.getvalue()


def boost_wav(raw, gain=1.0):
    """Raise PCM16 speech level while leaving headroom for encoding peaks.

    Args:
        raw: Complete generated WAV bytes.
        gain: Requested amplitude multiplier, from 0.25 to 4.0.

    Returns:
        WAV bytes and applied gain/peak metadata. Other sample formats pass through.

    Raises:
        ValueError: Gain is not finite or outside the supported range.
    """
    gain = float(gain)
    if not math.isfinite(gain) or not 0.25 <= gain <= 4.0:
        raise ValueError("Invalid local voice volume gain")
    with wave.open(io.BytesIO(raw), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    if params.sampwidth != 2:
        return raw, {"applied_gain": 1.0, "supported": False}
    samples = array.array("h", frames)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    ceiling = int(32767 * 10 ** (-1.0 / 20))
    applied = min(gain, ceiling / peak) if peak else 1.0
    adjusted = array.array("h", (round(value * applied) for value in samples))
    after_peak = max((abs(value) for value in adjusted), default=0)
    if sys.byteorder != "little":
        adjusted.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setparams(params)
        target.writeframes(adjusted.tobytes())
    return output.getvalue(), {
        "applied_gain": round(applied, 6),
        "supported": True,
        "peak_before_dbfs": round(20 * math.log10(max(peak / 32768, 1e-9)), 2),
        "peak_after_dbfs": round(20 * math.log10(max(after_peak / 32768, 1e-9)), 2),
    }


def speech_text(text):
    """Clean a reply so text-to-speech phrases it like a sentence.

    Args:
        text: Public reply text, possibly with bubble breaks or markup.

    Returns:
        One spoken line with sentence punctuation only.
    """
    text = re.sub(r"&&[^&\n]+&&|\[\[QQ:[^\]]+\]\]", "", str(text or ""))
    text = text.replace("\n", "，")
    text = re.sub(r"…+|\.{3,}", "，", text)
    text = re.sub(r"[~～]+", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,、]{2,}", "，", text)
    text = re.sub(r"[，,](?=[。！？!?])", "", text)
    text = text.strip("，, ")
    if text and text[-1] not in "。！？!?":
        text += "。"
    return text


class BreezeSpeech:
    """Persist generation and delivery states separately in the plugin's own data."""

    def __init__(self, root, client):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.lock = asyncio.Lock()
        path = self.root / "usage.json"
        self.usage = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def save(self):
        """Atomically save quota reservations without credentials or reply text."""
        target = self.root / "usage.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.usage, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)

    async def generate(self, text, config, request_id, instructions=""):
        """Generate one short WAV using the official cloud API contract.

        Args:
            text: Final public-facing reply only.
            config: Plugin settings including the secret API key.
            request_id: Original QQ message ID or an explicit isolated preview ID.
            instructions: Brief performance direction, separate from spoken text.

        Returns:
            A local audio path and separate generation/delivery metadata.

        Raises:
            ValueError: Configuration, text or returned audio is invalid.
            RuntimeError: Quota exhausted, API rejected or submission outcome unknown.
        """
        text = str(text).strip()
        if not config.get("enabled", True) or not config.get("api_key", "").strip():
            raise ValueError(
                "Voice credentials are not configured or voice is disabled"
            )
        if not 0 < len(text) <= min(120, int(config.get("max_chars", 80))):
            raise ValueError("Reply exceeds the configured short-voice limit")
        voice = str(config.get("voice_id", ""))
        if not re.fullmatch(r"voc_[a-z0-9]+", voice):
            raise ValueError("Invalid saved voice identifier")
        model = str(config.get("model_id", "breeze-tts-2"))
        if model not in ("breeze-tts-2", "breeze-tts-2-multilingual"):
            raise ValueError("Unsupported configured speech model")
        volume_gain = float(config.get("volume_gain", 1.0))
        if not math.isfinite(volume_gain) or not 0.25 <= volume_gain <= 4.0:
            raise ValueError("Invalid local voice volume gain")
        identity = hashlib.sha256(str(request_id).encode()).hexdigest()
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        credits = math.ceil(
            sum(2 if "\u3400" <= c <= "\u9fff" else 1 for c in text) / 10
        )
        async with self.lock:
            previous = self.usage.get(identity)
            if previous:
                if (
                    previous.get("status") == "ready"
                    and Path(previous["path"]).is_file()
                ):
                    return dict(previous)
                raise RuntimeError(
                    "Prior submission is pending or unknown; not retried"
                )
            today = [item for item in self.usage.values() if item.get("day") == day]
            if len(today) >= int(config.get("daily_calls", 20)) or sum(
                x.get("credits_reserved", 0) for x in today
            ) + credits > int(config.get("daily_credits", 100)):
                raise RuntimeError("Daily voice budget reached")
            record = {
                "request_id": identity,
                "day": day,
                "at": time.time(),
                "status": "pending",
                "delivery": "not_sent",
                "credits_reserved": credits,
            }
            self.usage[identity] = record
            self.save()
            try:
                response = await self.client.post(
                    "https://api.breeze.blue/v1/text-to-speech/" + voice,
                    params={"output_format": "wav"},
                    headers={"xi-api-key": config["api_key"].strip()},
                    json={
                        "text": text,
                        "model_id": model,
                        "language_code": "zh",
                        "instructions": str(instructions)[:400],
                        "voice_settings": {
                            "guidance_scale": max(
                                1.0, min(10.0, float(config.get("guidance_scale", 2.0)))
                            ),
                            "speed": max(
                                0.5, min(2.0, float(config.get("speed", 0.85)))
                            ),
                        },
                    },
                )
            except (httpx.HTTPError, OSError) as exc:
                record["status"] = "unknown"
                record["error_type"] = type(exc).__name__
                self.save()
                raise RuntimeError(
                    "Voice submission outcome unknown; no automatic retry"
                ) from None
            if response.status_code != 200:
                record.update(
                    status="unknown" if response.status_code >= 500 else "rejected",
                    http_status=response.status_code,
                )
                self.save()
                raise RuntimeError(
                    "Voice API returned HTTP " + str(response.status_code)
                )
            try:
                raw = response.content
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError("Audio is unexpectedly large")
                raw, volume_info = boost_wav(raw, volume_gain)
                raw = pad_wav(
                    raw,
                    float(config.get("lead_silence", 0.15)),
                    float(config.get("tail_silence", 0.6)),
                )
                with wave.open(io.BytesIO(raw), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if not 0.05 <= duration <= 90:
                        raise ValueError("Invalid audio duration")
                target = self.root / (identity + ".wav")
                target.write_bytes(raw)
            except (ValueError, wave.Error, EOFError, OSError):
                record["status"] = "invalid_audio"
                self.save()
                raise ValueError(
                    "Voice API did not return a complete short WAV"
                ) from None
            record.update(
                status="ready",
                path=str(target),
                duration_seconds=round(duration, 2),
                volume=volume_info,
                history_id=response.headers.get("history-item-id"),
            )
            self.save()
            return dict(record)

    def mark_delivery(self, identity, state):
        """Record a known delivery transition without resubmitting synthesis.

        Args:
            identity: Hashed request ID returned by generate.
            state: pending, sent or unknown.
        """
        if identity in self.usage:
            self.usage[identity]["delivery"] = state
            self.save()
