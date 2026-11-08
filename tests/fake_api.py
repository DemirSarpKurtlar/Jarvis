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


# --- BILGISAYAR: gerçek API'nin araç arama ve bilgisayar araç seti kuralları ---
def _bad(message: str) -> httpx2.Response:
    return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
                           headers={"request-id": "req_bad"})


def validate_request(body: dict, reject_tool_search: bool = False) -> str | None:
    """Gerçek API'de 400 dönecek durumlar (yalnızca JARVIS'in kullandığı özellikler için)."""
    tools = [t for t in body.get("tools", []) if isinstance(t, dict)]
    search = [t for t in tools if str(t.get("type", "")).startswith("tool_search_tool")]
    if search and reject_tool_search:
        return f"'{body.get('model')}' does not support tool types: {search[0]['type']}."
    deferred = [t for t in tools if t.get("defer_loading")]
    if tools and len(deferred) == len(tools):
        return "At least one tool must have defer_loading=false. All tools cannot be deferred."
    if any(t.get("defer_loading") for t in search):
        return "The tool search tool cannot have defer_loading set."
    if deferred and not search:
        return "Tools with defer_loading require a tool search tool."
    if any(t.get("cache_control") for t in deferred):
        return "A tool with defer_loading cannot have cache_control."
    for t in tools:
        if t.get("type") == "computer_20251124":
            return f"'{body.get('model')}' does not support tool types: computer_20251124."
        if t.get("type") == "computer_toolset_20260801":
            extra = set(t) - {"type", "configs", "cache_control"}
            if extra:
                return f"computer_toolset_20260801: unexpected fields {sorted(extra)}"
    if any(t.get("type") == "computer_toolset_20260801" for t in tools) and \
            any(t.get("name") == "computer" for t in tools):
        return "A tool named 'computer' cannot be combined with the computer toolset."
    choice = body.get("tool_choice") or {}
    if isinstance(choice, dict) and choice.get("type") in ("any", "tool"):
        return 'tool_choice: type "tool" and "any" are not supported for this model.'
    msgs = body.get("messages", [])
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        uses = [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        if not uses or nxt is None:
            continue   # son asistan mesajı (pause_turn) olabilir
        results = {b.get("tool_use_id"): b for b in (nxt.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"}
        for u in uses:
            r = results.get(u.get("id"))
            if r is None:
                return f"messages.{i + 1}: tool_use ids were found without tool_result blocks: {u.get('id')}"
            if u.get("toolset_name") and r.get("toolset_name") != u["toolset_name"]:
                return f"messages.{i + 1}: tool_result for a toolset tool_use must include toolset_name"
            if not u.get("toolset_name") and r.get("toolset_name"):
                return f"messages.{i + 1}: unexpected toolset_name on tool_result"
        for rid in results:
            if str(rid).startswith("srvtoolu_"):
                return f"messages.{i + 1}: tool_result for a server tool use"
    return None
# --- BILGISAYAR sonu ---


@dataclass
class FakeAPI:
    """Sırayla verilen yanıtları döndürür, gelen istekleri kaydeder."""

    responses: list = field(default_factory=list)  # bytes (SSE) | (status, dict) | Exception
    requests: list[dict] = field(default_factory=list)
    headers: list[dict] = field(default_factory=list)
    reject_tool_search: bool = False   # BILGISAYAR: araç aramayı desteklemeyen model gibi davran
    rejected: list[str] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.requests.append({"path": request.url.path, "body": body})
        self.headers.append(dict(request.headers))
        if request.url.path.startswith("/v1/models/"):
            return httpx2.Response(200, json={"type": "model", "id": request.url.path.rsplit("/", 1)[1],
                                              "display_name": "x", "created_at": "2026-01-01T00:00:00Z"})
        # Gerçek API ile aynı doğrulama: bir istekte en fazla 20 katı (strict) araç
        n_strict = sum(1 for t in body.get("tools", []) if isinstance(t, dict) and t.get("strict"))
        if n_strict > 20:
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error",
                "message": f"Too many strict tools ({n_strict}). The maximum number of strict tools supported is 20."}})
        # --- BILGISAYAR ---
        problem = validate_request(body, self.reject_tool_search) if request.url.path == "/v1/messages" else None
        if problem:
            self.rejected.append(problem)
            return _bad(problem)
        # --- BILGISAYAR sonu ---
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


def tool_use_stream(name: str, tool_input: dict, text_before: str = "", tool_id: str = "toolu_1",
                    model: str = "claude-haiku-5-5", stop_reason: str = "tool_use") -> bytes:
    """Claude'un bir istemci aracı çağırdığı akış."""
    events = [{"type": "message_start", "message": {
        "id": "msg_tool", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}}}]
    idx = 0
    events += [
        {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": idx, "delta": {"type": "signature_delta", "signature": "sig-t"}},
        {"type": "content_block_stop", "index": idx},
    ]
    idx += 1
    if text_before:
        events += [
            {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text_before}},
            {"type": "content_block_stop", "index": idx},
        ]
        idx += 1
    events += [
        {"type": "content_block_start", "index": idx,
         "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
        {"type": "content_block_delta", "index": idx,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input, ensure_ascii=False)}},
        {"type": "content_block_stop", "index": idx},
        {"type": "message_delta", "delta": {"type": "message_delta", "stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"input_tokens": 1500, "output_tokens": 40, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}},
        {"type": "message_stop"},
    ]
    return sse(*events)


# --- BILGISAYAR ---
def toolset_stream(calls: list[tuple], text: str = "", model: str = "claude-haiku-5-5",
                   stop_reason: str = "tool_use", usage: dict | None = None, id_prefix: str = "toolu_c") -> bytes:
    """Claude'un bir turda bir ya da daha çok bilgisayar araç seti üyesi (ya da özel araç) çağırdığı akış.

    `calls`: (ad, girdi) -> araç seti üyesi; (ad, girdi, None) -> özel araç (toolset_name yok)."""
    u = {"input_tokens": 2000, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    final_usage = {"input_tokens": 2000, "output_tokens": 60, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}
    final_usage.update(usage or {})
    events = [{"type": "message_start", "message": {
        "id": "msg_cu", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": u}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-cu"}},
        {"type": "content_block_stop", "index": 0}]
    idx = 1
    if text:
        events += [
            {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": idx}]
        idx += 1
    for n, call in enumerate(calls):
        name, tool_input = call[0], call[1]
        toolset = call[2] if len(call) > 2 else "computer"
        block = {"type": "tool_use", "id": f"{id_prefix}{n}", "name": name, "input": {}, "caller": {"type": "direct"}}
        if toolset:
            block["toolset_name"] = toolset
        events += [
            {"type": "content_block_start", "index": idx, "content_block": block},
            {"type": "content_block_delta", "index": idx,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input, ensure_ascii=False)}},
            {"type": "content_block_stop", "index": idx}]
        idx += 1
    events += [{"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": final_usage},
               {"type": "message_stop"}]
    return sse(*events)
# --- BILGISAYAR sonu ---
