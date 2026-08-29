"""The one place in this package that opens a network connection.

Everything else runs against your files in your process. Keeping the boundary in
a single module is what makes that checkable rather than merely claimed — there
is a test asserting no other module imports a network library.

Credentials come from the environment only. Never a config file, never a path
inside the package, never a flag that would put a key in your shell history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

MODEL = "claude-opus-5"
MAX_TOKENS = 16000


class ProviderError(RuntimeError):
    pass


class MissingCredentials(ProviderError):
    pass


NO_CREDENTIALS = (
    "No Anthropic credentials found. Set ANTHROPIC_API_KEY in your "
    "environment, or run `ant auth login`. This tool never reads a key from a "
    "file and never accepts one as an argument, so it cannot end up in your "
    "shell history or a commit."
)


@dataclass
class Response:
    data: dict
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class AnthropicProvider:
    """Claude, via the official SDK.

    Installed only with the `llm` extra, and imported lazily so the core package
    keeps its single dependency whether or not the extra is present.
    """

    name = "anthropic"

    def __init__(self, model: str = MODEL) -> None:
        self.model = model

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "The anthropic SDK is not installed. "
                "Install the extra: pip install 'dataassay[llm]'"
            ) from exc

        # The SDK resolves ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN, then
        # an `ant auth login` profile. An unset API key does NOT mean no
        # credentials, so the SDK is left to decide.
        return anthropic.Anthropic()

    def ask(self, system: str, prompt: str, schema: dict) -> Response:
        import anthropic

        client = self._client()
        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except TypeError as exc:
            # The SDK resolves credentials when the request is BUILT, not when
            # the client is constructed, so an absent key surfaces here as a
            # TypeError rather than anywhere sensible.
            if "authentication" in str(exc).lower():
                raise MissingCredentials(NO_CREDENTIALS) from exc
            raise
        except anthropic.AuthenticationError as exc:
            raise MissingCredentials(
                "Anthropic rejected the credentials in your environment. "
                + NO_CREDENTIALS
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Rate limited by the Anthropic API.") from exc
        except anthropic.APIStatusError as exc:
            # Carry the API's own explanation. Reporting only the status code
            # turned a one-line schema fix into a debugging session: a 400 here
            # is nearly always a malformed request that names its own cause.
            detail = getattr(exc, "message", "") or str(exc)
            raise ProviderError(
                f"Anthropic API error {exc.status_code}: {detail}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                "Could not reach the Anthropic API. This tool works fully "
                "offline without the interview."
            ) from exc

        if message.stop_reason == "refusal":
            raise ProviderError("The model declined to answer this request.")

        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("The model's reply was not valid JSON.") from exc

        usage = getattr(message, "usage", None)
        return Response(
            data=data,
            model=message.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )


def credentials_present() -> bool:
    """Whether anything looks like a usable credential, without opening a socket."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path

    return (Path.home() / ".config" / "anthropic").is_dir()


def get(name: str = "anthropic") -> AnthropicProvider:
    if name != "anthropic":
        raise ProviderError(
            f"Unknown provider {name!r}. Only 'anthropic' ships today; the "
            "interface is one method (ask) if you want to add another."
        )
    return AnthropicProvider()
