"""Select an installed Oryn provider from the chosen model ID."""

from pathlib import Path
from typing import Any

from src.providers.codex import AUTH_FILE, CodexProvider, auth_setup_warning
from src.providers.gemini import GeminiProvider


def provider_for_model(model: str, auth_file: Path = AUTH_FILE):
    if model.startswith("gemini-"):
        return GeminiProvider(model)
    return CodexProvider(model, auth_file)


def available_models(auth_file: Path = AUTH_FILE) -> list[tuple[str, str, str]]:
    codex_models = CodexProvider("gpt-5.6-luna", auth_file).cached_models()
    gemini_models = GeminiProvider.cached_models()
    return ([(model, label, "ChatGPT") for model, label in codex_models]
            + [(model, label, "Gemini") for model, label in gemini_models])


def provider_setup_warning(provider: Any) -> str | None:
    if isinstance(provider, GeminiProvider):
        return provider.setup_warning()
    return auth_setup_warning(getattr(provider, "auth_file", None))
