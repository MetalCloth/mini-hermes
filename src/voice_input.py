"""Push-to-talk capture and ElevenLabs Scribe transcription for the TUI."""

import array
import io
import json
import math
import sys
import threading
import urllib.error
import urllib.request
import uuid
import wave

from src.security.secrets import local_secret


API_KEY_FILE = "elevenlabs.env"
TRANSCRIPTION_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MAX_RECORDING_SECONDS = 90


class VoiceInputError(RuntimeError):
    """A microphone or speech-to-text setup/request problem safe to show in the TUI."""

    def __init__(self, message: str, *, code: str = "voice_input_error") -> None:
        super().__init__(message)
        self.code = code


def elevenlabs_api_key() -> str:
    return local_secret("ELEVENLABS_API_KEY", API_KEY_FILE)


def wav_signal_levels(audio: bytes) -> tuple[bool, float | None, float | None]:
    """Return safe peak/RMS metadata, never audio content, for a captured WAV."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            if wav_file.getsampwidth() != 2:
                return False, None, None
            samples = array.array("h")
            samples.frombytes(wav_file.readframes(wav_file.getnframes()))
            if sys.byteorder != "little":
                samples.byteswap()
    except (OSError, EOFError, wave.Error):
        return False, None, None
    if not samples:
        return True, None, None

    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    peak_dbfs = round(20 * math.log10(peak / 32768), 1) if peak else None
    rms_dbfs = round(20 * math.log10(rms / 32768), 1) if rms else None
    return True, peak_dbfs, rms_dbfs


class VoiceRecorder:
    def __init__(self) -> None:
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            raise VoiceInputError(
                "Microphone input needs sounddevice and the system PortAudio library.",
                code="missing_audio_dependency",
            ) from exc

        self._sounddevice = sounddevice
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._frames = 0
        self._limit_frames = 0
        self.limit_reached = False
        self._closed = False
        try:
            device = sounddevice.query_devices(kind="input")
            self.sample_rate = int(round(device["default_samplerate"]))
            if self.sample_rate <= 0 or device.get("max_input_channels", 0) < 1:
                raise ValueError("No usable input device")
            self._limit_frames = self.sample_rate * MAX_RECORDING_SECONDS
            self._stream = sounddevice.RawInputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16",
                blocksize=0, callback=self._capture,
            )
            self._stream.start()
        except Exception as exc:
            stream = getattr(self, "_stream", None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise VoiceInputError(
                "Could not start the microphone. Check the selected input device and its permissions.",
                code="microphone_unavailable",
            ) from exc

    def _capture(self, data, frames, _time_info, _status) -> None:
        with self._lock:
            remaining = self._limit_frames - self._frames
            kept_frames = min(frames, max(0, remaining))
            if kept_frames:
                self._chunks.append(bytes(memoryview(data)[:kept_frames * 2]))
                self._frames += kept_frames
            if self._frames >= self._limit_frames:
                self.limit_reached = True
                raise self._sounddevice.CallbackStop

    def stop(self) -> bytes:
        self._close_stream()

        with self._lock:
            frames = self._frames
            audio = b"".join(self._chunks)
            self._chunks.clear()
        if not frames:
            return b""

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio)
        return output.getvalue()

    @property
    def duration_ms(self) -> int:
        with self._lock:
            if self.sample_rate <= 0:
                return 0
            return round(self._frames * 1000 / self.sample_rate)

    def _close_stream(self) -> None:
        if self._closed:
            return
        self._closed = True
        stream = self._stream
        try:
            if stream.active:
                stream.stop()
        finally:
            stream.close()

    def discard(self) -> None:
        self._close_stream()
        with self._lock:
            self._chunks.clear()
            self._frames = 0


def transcribe_wav(audio: bytes) -> str:
    key = elevenlabs_api_key()
    if not key:
        raise VoiceInputError(
            "Add ELEVENLABS_API_KEY to the environment or ~/.mini-hermes/elevenlabs.env.",
            code="missing_api_key",
        )
    if not audio:
        raise VoiceInputError("No audio was captured. Check the microphone and try again.", code="no_audio")

    boundary = uuid.uuid4().hex
    body = b"".join((
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v2\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
        audio,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    request = urllib.request.Request(
        TRANSCRIPTION_URL,
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read(1_000_001))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise VoiceInputError("ElevenLabs rejected the API key or Scribe access.", code="auth_rejected") from exc
        if exc.code == 429:
            raise VoiceInputError("ElevenLabs rate limit or usage limit reached.", code="rate_limited") from exc
        raise VoiceInputError(f"ElevenLabs transcription failed (HTTP {exc.code}).", code="http_error") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VoiceInputError("Could not reach ElevenLabs for transcription.", code="network_error") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VoiceInputError("ElevenLabs returned an unreadable transcription response.", code="invalid_response") from exc

    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise VoiceInputError("ElevenLabs did not return any recognized speech.", code="no_speech")
    return text.strip()
