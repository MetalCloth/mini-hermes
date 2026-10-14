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
from dataclasses import dataclass
from pathlib import Path

from src.security.secrets import local_secret


API_KEY_FILE = "elevenlabs.env"
TRANSCRIPTION_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MAX_RECORDING_SECONDS = 90
VOICE_SETTINGS_PATH = Path.home() / ".mini-hermes" / "voice-settings.json"
SYSTEM_DEFAULT_INPUT = "system-default"


class VoiceInputError(RuntimeError):
    """A microphone or speech-to-text setup/request problem safe to show in the TUI."""

    def __init__(self, message: str, *, code: str = "voice_input_error") -> None:
        super().__init__(message)
        self.code = code


def elevenlabs_api_key() -> str:
    return local_secret("ELEVENLABS_API_KEY", API_KEY_FILE)


@dataclass(frozen=True)
class VoiceInputDevice:
    index: int
    name: str
    host_api: str
    occurrence: int

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.name.casefold(), self.host_api.casefold(), self.occurrence

    @property
    def choice_id(self) -> str:
        return "device:" + json.dumps(self.identity, separators=(",", ":"))

    @property
    def label(self) -> str:
        suffix = f" #{self.occurrence + 1}" if self.occurrence else ""
        return f"{self.name}{suffix}"


def voice_input_devices() -> list[VoiceInputDevice]:
    """List current recording devices with identities stable across device-index changes."""
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise VoiceInputError(
            "Microphone input needs sounddevice and the system PortAudio library.",
            code="missing_audio_dependency",
        ) from exc

    try:
        host_apis = sounddevice.query_hostapis()
        devices = sounddevice.query_devices()
    except Exception as exc:
        raise VoiceInputError(
            "Could not list microphone inputs. Check that the system audio service is running.",
            code="device_list_failed",
        ) from exc

    occurrences: dict[tuple[str, str], int] = {}
    result = []
    for device in devices:
        if device.get("max_input_channels", 0) < 1:
            continue
        name = str(device.get("name", "")).strip()
        host_index = device.get("hostapi")
        if not name or not isinstance(host_index, int) or not 0 <= host_index < len(host_apis):
            continue
        host_api = str(host_apis[host_index].get("name", "")).strip()
        if not host_api:
            continue
        key = (name.casefold(), host_api.casefold())
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        result.append(VoiceInputDevice(
            index=int(device["index"]), name=name, host_api=host_api, occurrence=occurrence,
        ))
    result.sort(key=lambda item: (item.host_api.casefold(), item.name.casefold(), item.occurrence))
    return result


def load_voice_input_preference() -> tuple[str, str, int] | None:
    try:
        data = json.loads(VOICE_SETTINGS_PATH.read_text(encoding="utf-8"))
        value = data.get("input_device")
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 3
            or not isinstance(value[0], str) or not value[0]
            or not isinstance(value[1], str) or not value[1]
            or not isinstance(value[2], int) or isinstance(value[2], bool) or value[2] < 0):
        return None
    return value[0].casefold(), value[1].casefold(), value[2]


def save_voice_input_preference(identity: tuple[str, str, int] | None) -> None:
    if identity is not None and (
        len(identity) != 3 or not isinstance(identity[0], str) or not identity[0]
        or not isinstance(identity[1], str) or not identity[1]
        or not isinstance(identity[2], int) or isinstance(identity[2], bool) or identity[2] < 0
    ):
        raise ValueError("Invalid microphone selection.")
    target = VOICE_SETTINGS_PATH
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as settings:
            settings.write(json.dumps({"input_device": list(identity) if identity else None}) + "\n")
            settings.flush()
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


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
    def __init__(self, input_device: tuple[str, str, int] | None = None) -> None:
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
        self.input_overflow = False
        self.limit_reached = False
        self._closed = False

        try:
            if input_device is None:
                device = sounddevice.query_devices(kind="input")
            else:
                selected = next((
                    item for item in voice_input_devices() if item.identity == input_device
                ), None)
                if selected is None:
                    raise VoiceInputError(
                        "Your selected microphone is unavailable. Reconnect it or choose another with /mic.",
                        code="device_unavailable",
                    )
                device = sounddevice.query_devices(selected.index, kind="input")
        except VoiceInputError:
            raise
        except Exception as exc:
            raise VoiceInputError(
                "Could not find an available microphone. Check the input device and its permissions.",
                code="microphone_unavailable",
            ) from exc

        try:
            self.sample_rate = int(round(device["default_samplerate"]))
            if self.sample_rate <= 0 or device.get("max_input_channels", 0) < 1:
                raise ValueError("No usable input device")
            self.input_device_name = str(device["name"])
            self.input_host_api = sounddevice.query_hostapis(int(device["hostapi"]))["name"]
            self.device_index = int(device["index"])
            self._limit_frames = self.sample_rate * MAX_RECORDING_SECONDS
            self._stream = sounddevice.RawInputStream(
                device=self.device_index,
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

    def _capture(self, data, frames, _time_info, status) -> None:
        with self._lock:
            if getattr(status, "input_overflow", False):
                self.input_overflow = True
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
