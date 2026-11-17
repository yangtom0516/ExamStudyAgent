"""Which LLM an agent runs on, from a model name in config (EXAM_MODEL etc.).

- A Gemini model id ("gemini-3.8-flash") runs on Gemini with our API key from .env. ADK would
  otherwise look for GOOGLE_API_KEY in the OS environment, which .env never reaches.
- "<provider>/<model>" (e.g. "openrouter/openai/gpt-oss-120b:free") runs through ADK's LiteLLM
  adapter. LiteLLM isn't installed until a provider like that is actually wanted.
"""

import importlib.util

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from app.config import Settings

# Short retries for brief Gemini hiccups (429/5xx) during a model call; a student is waiting.
AGENT_RETRIES = types.HttpRetryOptions(attempts=3, initial_delay=1, max_delay=10)


def make_model(name: str, settings: Settings) -> BaseLlm:
    if not name:
        raise RuntimeError("No agent model configured (e.g. EXAM_MODEL in .env)")

    if "/" in name and not name.startswith(("models/", "projects/")):
        if importlib.util.find_spec("litellm") is None:
            raise RuntimeError(
                f"Model {name!r} needs LiteLLM: run `uv add litellm` and set that provider's API key"
            )
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=name)

    api_key = settings.google_api_key.get_secret_value()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set in .env")
    # client_kwargs is excluded from ADK's logs and dumps, so the key isn't printed anywhere.
    return Gemini(model=name, client_kwargs={"api_key": api_key}, retry_options=AGENT_RETRIES)
