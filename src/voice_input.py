"""Push-to-talk capture and ElevenLabs Scribe transcription for the TUI."""

import io
import json
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


def elevenlabs_api_key() -> str:
    return local_secret("ELEVENLABS_API_KEY", API_KEY_FILE)


class VoiceRecorder:
    def __init__(self) -> None:
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            raise VoiceInputError(
                "Microphone input needs sounddevice and the system PortAudio library."
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
                "Could not start the microphone. Check the selected input device and its permissions."
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
            "Add ELEVENLABS_API_KEY to the environment or ~/.mini-hermes/elevenlabs.env."
        )
    if not audio:
        raise VoiceInputError("No audio was captured. Check the microphone and try again.")

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
            raise VoiceInputError("ElevenLabs rejected the API key or Scribe access.") from exc
        if exc.code == 429:
            raise VoiceInputError("ElevenLabs rate limit or usage limit reached.") from exc
        raise VoiceInputError(f"ElevenLabs transcription failed (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VoiceInputError("Could not reach ElevenLabs for transcription.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VoiceInputError("ElevenLabs returned an unreadable transcription response.") from exc

    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise VoiceInputError("ElevenLabs did not return any recognized speech.")
    return text.strip()
