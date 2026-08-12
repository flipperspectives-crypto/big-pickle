import json
import time

import httpx

from . import providers
from .config import settings
from .db import record_usage
from .local import local_model_ids
from . import runtime

_ERR_MARKER = "\x00GWERR\x00"


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str | None = None, *, provider: str | None = None, reason: str | None = None):
        # `detail` is a SAFE, client-facing message only. Raw upstream response
        # bodies, provider keys, internal URLs/hosts, and exception text must
        # NEVER be placed here or logged. `reason` is a short, non-sensitive
        # reason code; `provider` is the provider name (not a secret).
        self.status = status
        self.provider = provider
        self.reason = reason
        self.detail = detail or _safe_detail(status, reason)
        super().__init__(f"upstream {status} provider={provider} reason={reason}")


def _safe_detail(status: int, reason: str | None) -> str:
    if reason == "network_error":
        return "The provider could not be reached (network error). Please retry."
    if status == 401 or status == 403:
        return "The upstream provider rejected the request (auth)."
    if status == 404:
        return "That model is not available from any configured provider."
    if status == 429:
        return "The provider is rate-limiting requests right now. Please retry shortly."
    if 500 <= status < 600:
        return "The provider is temporarily unavailable. Please retry or try another model."
    return "The provider returned an error. Please retry or try another model."


def _auth_headers(provider: str) -> dict:
    key = settings.provider_key(provider)
    if provider in providers.ANTHROPIC:
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _openai_payload(body: dict, provider: str) -> dict:
    payload = json.loads(json.dumps(body))
    payload["model"] = providers.upstream_model(provider, payload["model"])
    return payload


def _anthropic_payload(body: dict, provider: str) -> dict:
    messages = []
    system = []
    for m in body.get("messages", []):
        role = m["role"]
        if role == "system":
            system.append(m.get("content", ""))
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for p in content:
                if p.get("type") == "text":
                    parts.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": p["image_url"]["url"].split(";base64,")[0].split("data:")[-1],
                            "data": p["image_url"]["url"].split(",", 1)[1],
                        },
                    })
            content = parts
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    payload = {
        "model": providers.upstream_model(provider, body["model"]),
        "messages": messages,
        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 4096)),
    }
    if system:
        payload["system"] = system if len(system) > 1 else system[0]
    for k in ("temperature", "top_p", "top_k", "stop_sequences"):
        if k in body:
            payload[k] = body[k]
    if body.get("stream"):
        payload["stream"] = True
    return payload


def _usage_of(payload_usage: dict) -> tuple[int, int]:
    if not payload_usage:
        return 0, 0
    return payload_usage.get("prompt_tokens", 0), payload_usage.get("completion_tokens", 0)


async def _chat_openai(
    client: httpx.AsyncClient,
    provider: str,
    payload: dict,
    stream: bool,
):
    url = f"{providers.base_url(provider)}/chat/completions"
    headers = {"Content-Type": "application/json", ** _auth_headers(provider)}
    if stream:
        headers["Accept"] = "text/event-stream"
    try:
        r = await client.post(url, headers=headers, json=payload, timeout=120)
    except httpx.HTTPError:
        raise UpstreamError(502, provider=provider, reason="network_error")
    if r.status_code >= 400:
        # never include the raw upstream response body
        raise UpstreamError(r.status_code, provider=provider, reason=f"provider_http_{r.status_code}")
    if stream:
        return _stream_ok(r, provider, payload["model"])
    data = r.json()
    pt, ct = _usage_of(data.get("usage"))
    return data, pt, ct


def _stream_ok(r: httpx.Response, provider: str, model: str):
    async def gen():
        async for line in r.aiter_lines():
            yield line + "\n"
    return gen(), provider, model


async def _chat_anthropic(
    client: httpx.AsyncClient,
    provider: str,
    payload: dict,
):
    url = f"{providers.base_url(provider)}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.provider_key(provider),
        "anthropic-version": "2023-06-01",
    }
    stream = payload.get("stream", False)
    if stream:
        headers["Accept"] = "text/event-stream"
    try:
        r = await client.post(url, headers=headers, json=payload, timeout=120)
    except httpx.HTTPError:
        raise UpstreamError(502, provider=provider, reason="network_error")
    if r.status_code >= 400:
        # never include the raw upstream response body
        raise UpstreamError(r.status_code, provider=provider, reason=f"provider_http_{r.status_code}")
    if not stream:
        data = r.json()
        pt = data.get("usage", {}).get("input_tokens", 0)
        ct = data.get("usage", {}).get("output_tokens", 0)
        content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {
            "id": data.get("id", "chatcmpl-anthropic"),
            "object": "chat.completion",
            "model": providers.upstream_model(provider, payload["model"]),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": data.get("stop_reason", "stop")}
            ],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }, pt, ct
    return _anthropic_stream(r, provider, payload["model"])


def _anthropic_stream(r: httpx.Response, provider: str, model: str):
    async def gen():
        yielded_role = False
        usage_p = 0
        usage_c = 0
        stop_reason = "stop"
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "message_start":
                usage_p = ev.get("message", {}).get("usage", {}).get("input_tokens", 0)
            elif etype == "content_block_delta":
                dt = ev.get("delta", {}).get("type")
                if dt == "text_delta":
                    if not yielded_role:
                        chunk = {
                            "id": "chatcmpl-stream",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                        }
                        yield "data: " + json.dumps(chunk) + "\n\n"
                        yielded_role = True
                    chunk = {
                        "id": "chatcmpl-stream",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": ev["delta"]["text"]}, "finish_reason": None}],
                    }
                    yield "data: " + json.dumps(chunk) + "\n\n"
            elif etype == "message_delta":
                usage_c = ev.get("usage", {}).get("output_tokens", 0)
                stop_reason = ev.get("delta", {}).get("stop_reason", "stop")
            elif etype == "message_stop":
                break
        chunk = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": stop_reason}],
            "usage": {"prompt_tokens": usage_p, "completion_tokens": usage_c, "total_tokens": usage_p + usage_c},
        }
        yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"
    return gen(), provider, model


async def run_completion(body: dict, key_id: str):
    model = body.get("model", "")
    stream = bool(body.get("stream"))
    providers_list = providers.providers_for(model)
    if not providers_list:
        raise UpstreamError(404, f"no provider for model {model}")

    last_err: UpstreamError | None = None
    local_rt = False
    async with httpx.AsyncClient() as client:
        for provider in providers_list:
            try:
                if provider in providers.ANTHROPIC:
                    payload = _anthropic_payload(body, provider)
                    result = await _chat_anthropic(client, provider, payload)
                else:
                    payload = _openai_payload(body, provider)
                    if provider == "local" and not stream:
                        # Measure the Clarity-side round-trip around the ACTUAL
                        # local upstream request for non-streaming local success.
                        # Telemetry is recorded only on success; a failed/streaming
                        # or cloud request never populates local telemetry.
                        rt_start = time.monotonic()
                        result = await _chat_openai(client, provider, payload, stream)
                        rt_ms = (time.monotonic() - rt_start) * 1000.0
                        local_rt = True
                    else:
                        result = await _chat_openai(client, provider, payload, stream)

                if not stream:
                    data, pt, ct = result
                    cost = providers.price_for(provider, payload["model"], pt, ct)
                    record_usage(key_id, model, provider, pt, ct, cost)
                    if local_rt:
                        runtime.record_local_success(model, rt_ms, pt, ct)
                    return data, cost, provider
                gen, provider_used, upstream_model = result

                async def stream_with_usage(gen, provider_used, upstream_model, model, key_id):
                    usage_p = usage_c = 0
                    async for line in gen:
                        if line.startswith("data:"):
                            payload_line = line[5:].strip()
                            if payload_line and payload_line != "[DONE]":
                                try:
                                    obj = json.loads(payload_line)
                                    u = obj.get("usage")
                                    if u:
                                        usage_p = u.get("prompt_tokens", usage_p)
                                        usage_c = u.get("completion_tokens", usage_c)
                                except json.JSONDecodeError:
                                    pass
                        yield line
                    cost = providers.price_for(provider_used, upstream_model, usage_p, usage_c)
                    record_usage(key_id, model, provider_used, usage_p, usage_c, cost)

                return stream_with_usage(gen, provider_used, upstream_model, model, key_id), 0.0, provider
            except UpstreamError as e:
                last_err = e
                continue
    raise UpstreamError(
        502, provider=getattr(last_err, "provider", None), reason="all_providers_failed"
    )


async def available_models() -> list[str]:
    # Cloud/canonical routes, minus the stale legacy local aliases which are no
    # longer advertised (only actually-discovered `local:<tag>` models are).
    advertised = {m for m in providers.ROUTES if m not in providers.LEGACY_LOCAL_ALIASES}
    local = set(await local_model_ids())
    return sorted(advertised | local)
