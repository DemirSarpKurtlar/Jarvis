"""Anthropic Messages API'sini taklit eden sahte HTTP katmanı (gerçek SDK ile test için)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import anthropic
import httpx2

from jarvis.ai.claude_client import ClaudeClient


def sse(*events: dict) -> bytes:
    out = []
    for e in events:
        out.append(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n")
    return "".join(out).encode()


def text_stream(text_chunks: list[str], model: str = "claude-haiku-5-5", stop_reason: str = "end_turn",
                usage: dict | None = None, citations: list[dict] | None = None,
                stop_details: dict | None = None, thinking: bool = True) -> bytes:
    u = {"input_tokens": 1200, "output_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 900}
    final_usage = {"input_tokens": 1200, "output_tokens": 85, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 900}
    final_usage.update(usage or {})
    events = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": u}}]
    idx = 0
    if thinking:
        events += [
            {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": idx, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": idx},
        ]
        idx += 1
    events.append({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
    for c in citations or []:
        events.append({"type": "content_block_delta", "index": idx, "delta": {"type": "citations_delta", "citation": c}})
    for chunk in text_chunks:
        events.append({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": chunk}})
    events.append({"type": "content_block_stop", "index": idx})
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details:
        delta["stop_details"] = stop_details
    events.append({"type": "message_delta", "delta": delta, "usage": final_usage})
    events.append({"type": "message_stop"})
    return sse(*events)


def json_message(data: dict | None, model: str = "claude-haiku-5-5", stop_reason: str = "end_turn",
                 usage: dict | None = None, raw_text: str | None = None) -> tuple[int, dict]:
    """Akışsız `messages.create` yanıtı (yapılandırılmış JSON çıktısı). FakeAPI'ye (200, gövde) olarak verilir."""
    u = {"input_tokens": 1500, "output_tokens": 120, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0}
    u.update(usage or {})
    text = raw_text if raw_text is not None else json.dumps(data, ensure_ascii=False)
    return 200, {"id": "msg_json", "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "text", "text": text}] if text else [],
                 "stop_reason": stop_reason, "stop_sequence": None, "usage": u}


@dataclass
class FakeAPI:
    """Sırayla verilen yanıtları döndürür, gelen istekleri kaydeder."""

    responses: list = field(default_factory=list)  # bytes (SSE) | (status, dict) | Exception
    requests: list[dict] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.requests.append({"path": request.url.path, "body": body})
        self.headers.append(dict(request.headers))
        if request.url.path.startswith("/v1/models/"):
            return httpx2.Response(200, json={"type": "model", "id": request.url.path.rsplit("/", 1)[1],
                                              "display_name": "x", "created_at": "2026-01-01T00:00:00Z"})
        if not self.responses:
            raise AssertionError("Beklenmeyen istek: " + request.url.path)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            status, payload = r
            return httpx2.Response(status, json=payload, headers={"request-id": "req_err"})
        return httpx2.Response(200, content=r, headers={"content-type": "text/event-stream",
                                                         "request-id": "req_ok"})

    def client(self, key: str = "sk-ant-api03-" + "x" * 30, max_retries: int = 0) -> ClaudeClient:
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler))
        return ClaudeClient(key, max_retries=max_retries, http_client=http)
