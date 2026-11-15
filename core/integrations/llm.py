"""Provider-neutral LLM gateway for chat-style generation.

The application asks for a task class (`simple`, `complex` or `json`) and this
module chooses the configured provider/model. Gemini is prepared for cheap
simple work, but when its API key is absent we fall back to OpenAI so local
development keeps working.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Literal

import httpx
from langsmith import get_current_run_tree

from core.config import GEMINI_KEY, GEMINI_URL, OPENAI_KEY, OPENAI_URL, settings
from core.integrations.llm_utils import extract_json_from_text
from core.metrics import observe_llm
from core.observability import traced
from core.services import budget

logger = logging.getLogger(__name__)

TaskKind = Literal["simple", "complex", "json"]
Provider = Literal["gemini", "openai"]
ProviderResponse = tuple[str | None, int, int]

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=90.0)
    return _client


def _provider_for(task: TaskKind) -> Provider:
    if task in ("simple", "json"):
        provider = settings.llm_simple_provider
    else:
        provider = settings.llm_complex_provider

    if provider == "gemini" and not GEMINI_KEY:
        logger.warning("Gemini API key is not configured; falling back to OpenAI.")
        return "openai"
    return provider


def _looks_like_provider_model(model: str, provider: Provider) -> bool:
    lower = model.lower()
    if provider == "gemini":
        return lower.startswith("gemini")
    return not lower.startswith("gemini")


def _model_for(task: TaskKind, provider: Provider, explicit: str | None = None) -> str:
    if explicit:
        if _looks_like_provider_model(explicit, provider):
            return explicit
        return (
            settings.llm_fallback_model
            if provider == "openai"
            else settings.llm_simple_model
        )

    configured = (
        settings.llm_simple_model
        if task in ("simple", "json")
        else settings.llm_complex_model
    )
    if _looks_like_provider_model(configured, provider):
        return configured
    return settings.llm_fallback_model if provider == "openai" else settings.llm_simple_model


def _worst_input_for(task: TaskKind) -> int:
    """The most input this task class is allowed to send, from the pilot policy.

    Not an estimate of this prompt: the project counts tokens only from what
    the provider reports back, and adds no tokenizer to guess beforehand.
    """
    return {
        "json": settings.budget_worst_input_json,
        "simple": settings.budget_worst_input_simple,
    }.get(task, settings.budget_worst_input_complex)


def _temperature_for(task: TaskKind) -> float:
    if task in ("simple", "json"):
        return settings.llm_temperature_simple
    return settings.llm_temperature_complex


def content_to_text(content) -> str:
    """Flatten provider/data blocks into prompt text.

    Providers do not share one exact document/citation block shape through this
    lightweight gateway, so we keep the boundary explicit in text instead of
    mixing it into system instructions.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
            continue
        if block.get("type") == "document":
            title = block.get("title") or "document"
            source = block.get("source") or {}
            source_content = source.get("content") or []
            text = "\n".join(
                str(item.get("text", "")) for item in source_content
                if isinstance(item, dict)
            ).strip()
            parts.append(
                f"[Documento: {title}]\n{text}\n[/Documento: {title}]"
            )
    return "\n\n".join(part for part in parts if part).strip()


async def _openai_chat(system: str, user: str, model: str, json_mode: bool,
                       max_tokens: int, temperature: float) -> ProviderResponse:
    if not OPENAI_KEY:
        logger.error("OPENAI_API_KEY is not configured.")
        return None, 0, 0

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = await _http().post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as e:
        logger.error("OpenAI chat request failed: %s", e)
        return None, 0, 0

    if resp.status_code != 200:
        logger.error("OpenAI chat API returned status %s", resp.status_code)
        return None, 0, 0

    try:
        body = resp.json()
        usage = body.get("usage") or {}
        return (
            body["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
    except (KeyError, IndexError, TypeError) as e:
        logger.error("OpenAI chat returned an unexpected payload: %s", e)
        return None, 0, 0


async def _gemini_chat(system: str, user: str, model: str, json_mode: bool,
                       max_tokens: int, temperature: float) -> ProviderResponse:
    if not GEMINI_KEY:
        logger.error("GEMINI_API_KEY is not configured.")
        return None, 0, 0

    generation_config = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    try:
        resp = await _http().post(
            f"{GEMINI_URL}/{model}:generateContent",
            params={"key": GEMINI_KEY},
            json={
                "systemInstruction": {
                    "parts": [{"text": system}],
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user}],
                    }
                ],
                "generationConfig": generation_config,
            },
        )
    except httpx.HTTPError as e:
        logger.error("Gemini chat request failed: %s", e)
        return None, 0, 0

    if resp.status_code != 200:
        logger.error("Gemini chat API returned status %s", resp.status_code)
        return None, 0, 0

    try:
        body = resp.json()
        parts = body["candidates"][0]["content"]["parts"]
        usage = body.get("usageMetadata") or {}
        return (
            "".join(part.get("text", "") for part in parts),
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
        )
    except (KeyError, IndexError, TypeError) as e:
        logger.error("Gemini chat returned an unexpected payload: %s", e)
        return None, 0, 0


def _trace_inputs(inputs: dict) -> dict:
    if settings.langsmith_capture_content:
        return inputs
    return {
        "task": inputs.get("task", "complex"),
        "model": inputs.get("model"),
        "max_tokens": inputs.get("max_tokens", 1000),
        "content_redacted": True,
    }


def _trace_output(output: str | None) -> dict:
    if settings.langsmith_capture_content:
        return {"output": output}
    return {
        "generated": output is not None,
        "output_chars": len(output or ""),
        "content_redacted": True,
    }


@traced(
    name="llm.complete",
    run_type="llm",
    process_inputs=_trace_inputs,
    process_outputs=_trace_output,
)
async def complete(system: str, user, *, task: TaskKind = "complex",
                   model: str | None = None, max_tokens: int = 1000,
                   json_mode: bool = False, extra_input_tokens: int = 0) -> str | None:
    """Generate text using the provider selected for the task kind."""
    user_text = content_to_text(user)
    provider = _provider_for(task)
    chosen_model = _model_for(task, provider, model)
    temperature = _temperature_for(task)

    # Every call this application makes passes through here, which is why the
    # meter sits here and nowhere else. The output reservation is exact, being
    # the ceiling actually sent to the provider; the input one is the task
    # class's worst case, because this project has no tokenizer. Both are
    # returned below the moment the provider says what it really used.
    reservation = await budget.reserve(
        _worst_input_for(task) + max(0, extra_input_tokens), max_tokens)
    started = time.perf_counter()
    input_tokens = output_tokens = 0
    try:
        if provider == "gemini":
            text, input_tokens, output_tokens = await _gemini_chat(
                system, user_text, chosen_model, json_mode, max_tokens, temperature
            )
        else:
            text, input_tokens, output_tokens = await _openai_chat(
                system, user_text, chosen_model, json_mode, max_tokens, temperature
            )
    finally:
        # In a finally so a provider error returns its reservation too: a call
        # that never happened must not be charged for. Only a process that dies
        # here leaves one spent, which is the safe direction to fail.
        await budget.settle(reservation, input_tokens, output_tokens)

    observe_llm(
        provider,
        chosen_model,
        "success" if text is not None else "error",
        time.perf_counter() - started,
        input_tokens,
        output_tokens,
    )
    current_run = get_current_run_tree()
    if current_run is not None:
        current_run.set(
            metadata={"ls_provider": provider, "ls_model_name": chosen_model},
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )
    logger.info("LLM task=%s provider=%s model=%s", task, provider, chosen_model)
    return text


class VisionUnavailable(RuntimeError):
    """The provider did not read the image: no key, an error, or nothing
    usable came back. Carries no content."""


def _trace_image_inputs(inputs: dict) -> dict:
    # Never the image, and the prompt only when content capture is on.
    image = inputs.get("image_jpeg") or b""
    traced_inputs = {"task": inputs.get("task"), "image_bytes": len(image),
                     "max_tokens": inputs.get("max_tokens"), "content_redacted": True}
    if settings.langsmith_capture_content:
        traced_inputs["prompt"] = inputs.get("prompt")
        traced_inputs["content_redacted"] = False
    return traced_inputs


@traced(
    name="llm.complete_with_image",
    run_type="llm",
    process_inputs=_trace_image_inputs,
    process_outputs=_trace_output,
)
async def complete_with_image(task: TaskKind, prompt: str, image_jpeg: bytes, *,
                              max_tokens: int) -> str:
    """Ask the model about one image: the only caller of the provider's
    vision input, since `complete` flattens everything to text.

    Always OpenAI, the family that answers today (the spec's decision), with
    the task's model. The image goes as a JPEG data URL and nothing else
    about it (no name, no metadata). Metered like `complete`: the task's
    input ceiling plus one image's, and the output ceiling, reserved before
    the call and settled with what the provider reports. Raises
    `budget.BudgetExceeded` before any call when the budget is spent, and
    `VisionUnavailable` when no reading came back.
    """
    chosen_model = _model_for(task, "openai")
    reservation = await budget.reserve(
        _worst_input_for(task) + settings.budget_worst_input_image, max_tokens)
    started = time.perf_counter()
    input_tokens = output_tokens = 0
    text: str | None = None
    try:
        text, input_tokens, output_tokens = await _openai_vision(
            prompt, image_jpeg, chosen_model, max_tokens, _temperature_for(task))
    finally:
        await budget.settle(reservation, input_tokens, output_tokens)
    observe_llm("openai", chosen_model, "success" if text else "error",
                time.perf_counter() - started, input_tokens, output_tokens)
    logger.info("LLM vision task=%s model=%s", task, chosen_model)
    if not text or not text.strip():
        raise VisionUnavailable("the model returned no reading")
    return text.strip()


async def _openai_vision(prompt: str, image_jpeg: bytes, model: str, max_tokens: int,
                         temperature: float) -> ProviderResponse:
    if not OPENAI_KEY:
        logger.error("OPENAI_API_KEY is not configured.")
        return None, 0, 0
    image_url = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
        ]}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = await _http().post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as e:
        logger.error("OpenAI vision request failed: %s", type(e).__name__)
        return None, 0, 0
    if resp.status_code != 200:
        logger.error("OpenAI vision API returned status %s", resp.status_code)
        return None, 0, 0
    try:
        body = resp.json()
        usage = body.get("usage") or {}
        return (
            body["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.error("OpenAI vision returned an unexpected payload: %s", type(e).__name__)
        return None, 0, 0


async def complete_json(system: str, user, *, task: TaskKind = "json",
                        model: str | None = None,
                        max_tokens: int = 1000) -> dict | None:
    """Generate and parse a JSON object from the selected provider."""
    text = await complete(
        system, user, task=task, model=model, max_tokens=max_tokens, json_mode=True
    )
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = extract_json_from_text(text)
        if repaired:
            return repaired
        logger.error("LLM JSON response was unparseable.")
        return None


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
