"""Choosing an agent's model from config (agents/models.py). No API calls."""

import pytest
from google.adk.models.google_llm import Gemini
from pydantic import SecretStr

from app.agents.models import make_model
from app.config import get_settings


def settings_with_key(key: str):
    return get_settings().model_copy(update={"google_api_key": SecretStr(key)})


def test_exam_writer_defaults_to_gemini_3_8_flash():
    assert type(get_settings()).model_fields["exam_model"].default == "gemini-3.8-flash"
    assert type(get_settings()).model_fields["exam_max_llm_calls"].default == 25


def test_gemini_names_use_our_api_key():
    model = make_model("gemini-3.8-flash", settings_with_key("test-key"))
    assert isinstance(model, Gemini)
    assert model.model == "gemini-3.8-flash"
    assert model.client_kwargs == {"api_key": "test-key"}
    assert "test-key" not in repr(model) and "test-key" not in str(model.model_dump())  # never printed


def test_missing_api_key_or_model_name_is_a_clear_error():
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY is not set"):
        make_model("gemini-3.8-flash", settings_with_key(""))
    with pytest.raises(RuntimeError, match="No agent model configured"):
        make_model("", settings_with_key("test-key"))


def test_other_providers_need_litellm(monkeypatch):
    monkeypatch.setattr("app.agents.models.importlib.util.find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="uv add litellm"):
        make_model("openrouter/openai/gpt-oss-120b:free", settings_with_key("test-key"))
