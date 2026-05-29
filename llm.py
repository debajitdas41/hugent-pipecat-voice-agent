"""Sarvam AI LLM — raw HTTP to Sarvam Chat Completions API

Uses sarvam-m by default. reasoning_effort=null (JSON null) disables
reasoning/thinking mode for fast, low-latency responses.
"""
import os
import json
import re
import asyncio
import logging
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("llm")

_API_URL = "https://api.sarvam.ai/v1/chat/completions"


def _api_key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not set")
    return key


_MODEL = None


def _model() -> str:
    global _MODEL
    if _MODEL is None:
        _MODEL = os.getenv("LLM_MODEL", "sarvam-m")
    return _MODEL


# --------------------------------------------------------------------------
#  Low-level HTTP call
# --------------------------------------------------------------------------
_SENTINEL = object()


def _chat_completion(*, messages, temperature, max_tokens,
                     reasoning_effort=_SENTINEL):
    """POST to Sarvam completions with explicit reasoning_effort.

    reasoning_effort values:
      _SENTINEL  → omit field (server default = "medium")
      None       → send JSON null  (disables reasoning)
      "low"/"medium"/"high" → send that string
    """
    body: dict = {
        "model": _model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort is not _SENTINEL:
        body["reasoning_effort"] = reasoning_effort      # None → JSON null

    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": _api_key(),
    }
    
    # DEBUG: Log request details to diagnose 400 errors
    logger.debug(f"[_chat_completion DEBUG] Model: {body['model']}, Temp: {temperature}, Max tokens: {max_tokens}")
    logger.debug(f"[_chat_completion DEBUG] Messages count: {len(messages)}")
    for i, msg in enumerate(messages):
        content_preview = msg['content'][:200] if len(msg['content']) > 200 else msg['content']
        logger.debug(f"[_chat_completion DEBUG] Message {i} ({msg['role']}): {content_preview}")
    
    with httpx.Client(timeout=15.0) as client:
        try:
            resp = client.post(_API_URL, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"[_chat_completion] HTTP error: {e}")
            logger.error(f"[_chat_completion DEBUG] Response status: {e.response.status_code}")
            logger.error(f"[_chat_completion DEBUG] Response body: {e.response.text}")
            logger.error(f"[_chat_completion DEBUG] System prompt (first 500 chars): {messages[0]['content'][:500]}")
            raise


def _parse(data: dict):
    """Return (content, reasoning, finish_reason, usage) from API JSON."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    finish = choice.get("finish_reason", "unknown")
    usage = data.get("usage") or {}
    return content, reasoning, finish, usage


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from a completed string."""
    import re as _re
    return _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()


# --------------------------------------------------------------------------
#  Helpers: extract useful text from reasoning_content when content is empty
# --------------------------------------------------------------------------
def _extract_json_from_reasoning(reasoning: str) -> str:
    """Pull a JSON object out of reasoning_content."""
    if not reasoning:
        return ""
    match = re.search(r"\{[^{}]*\}", reasoning, re.DOTALL)
    return match.group(0) if match else ""


def _extract_response_from_reasoning(reasoning: str) -> str:
    """Extract a conversational reply from reasoning_content.

    Only matches double-quoted strings (single-quote matching picks up
    apostrophes in contractions like "Let's"/"What's").
    """
    if not reasoning:
        return ""
    candidates = []
    for m in re.finditer(r'"([^"]{10,300})"', reasoning):
        text = m.group(1).strip()
        if any(kw in text.lower() for kw in
               ["the user", "the model", "the instruction", "constraint",
                "my response", "the prompt", "the question", "the answer",
                "let me", "i should", "i need to", "i will", "i think",
                "the system", "reasoning"]):
            continue
        candidates.append(text)
    return candidates[-1] if candidates else ""


# --------------------------------------------------------------------------
#  Conversational response (used by session for general chat)
# --------------------------------------------------------------------------
async def generate_response(
    system_prompt: str,
    history: list[dict],
    user_text: str,
    timeout_s: float = 5.0,
) -> str:
    """Generate a conversational reply with reasoning_effort='low' for speed."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    def _call(temp: float):
        data = _chat_completion(
            messages=messages,
            temperature=temp,
            max_tokens=1000,
            reasoning_effort=None,   # null = non-think mode for sarvam-m
        )
        content, reasoning, finish, usage = _parse(data)
        content = _strip_think_tags(content)
        print(f"🧠 generate_response: finish={finish}, content={len(content)}ch, reasoning={len(reasoning)}ch, usage={usage}, temp={temp}")

        # Do NOT extract from reasoning — it contains internal thinking
        # (model self-evaluation, drafts) that produces garbage voice output.
        if not content:
            print(f"🧠 generate_response: content empty (finish={finish}), returning empty")

        return content

    for attempt, temp in enumerate([0.7, 0.8]):
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(_call, temp),
                timeout=timeout_s,
            )
            print(f"🧠 LLM generate_response (attempt {attempt+1}, temp={temp}): ({len(reply)} chars) '{reply[:200]}'")
            if reply:
                return reply
            if attempt == 0:
                print(f"⚠️ LLM generate_response empty at temp={temp}, retrying")
        except asyncio.TimeoutError:
            logger.warning(f"LLM generate_response timeout (attempt {attempt+1})")
            print(f"⏱️ LLM generate_response TIMEOUT (attempt {attempt+1})")
        except Exception as e:
            logger.error(f"LLM generate_response error: {e}")
            print(f"❌ LLM generate_response error: {e}")
            break
    return ""


# --------------------------------------------------------------------------
#  Streaming conversational response (low-latency: first token in ~100ms)
# --------------------------------------------------------------------------
async def generate_response_streaming(
    system_prompt: str,
    history: list[dict],
    user_text: str,
    with_extraction: bool = False,
    current_draft: dict = None,
    clinic_departments: list[str] = None,
) -> AsyncGenerator[str | dict, None]:
    """
    Stream a conversational reply via SSE.
    
    If with_extraction=True, prepends extraction instructions and yields
    a dict as the first item: {"__extraction__": {fields...}}
    Then yields normal text tokens for TTS.
    
    sarvam-m: reasoning_effort=null disables thinking mode for fast responses.
    Any reasoning_content deltas are silently skipped (safety net).
    """
    # Add extraction instructions if enabled
    if with_extraction:
        extraction_prefix = f"""IMPORTANT: Output format MUST be:

Line 1: JSON object with fields: {{"intent": "book|confirm|none", "name": "...", "age": "...", "department": "...", "date": "...", "time": "...", "confirm": true|false|null}}
Line 2 onwards: Your conversational response in the user's language

Extraction rules:
- Extract ONLY what user said in current turn. Do NOT copy from current draft.
- intent: "book" for appointment requests (including bare words like "appointment", "अपॉइंटमेंट", "booking"), "confirm" for yes/haan/theek hai, "none" for greetings/farewells
- name: Reject garbled single syllables. If speech is fragment like "मेरा नाम है।" WITHOUT name, return null
- age: Must be digit 1-120. Support Devanagari/Bengali numerals. NEVER extract from time/prices
- department: ONLY if user mentions symptom/body part/specialty THIS TURN. "doctor" alone → null
- date: WHEN to come. Duration phrases like "X दिन से" → null
- time: "HH:MM AM/PM" or "morning"/"evening". NEVER from age values
- confirm: true for yes/haan, false for no/nahi, null otherwise

Current draft (DO NOT copy): {current_draft or {{}}}
Clinic departments: {clinic_departments or []}

User said: "{user_text}"

Now output:
"""
        system_prompt = extraction_prefix + "\n\n" + system_prompt
    
    messages = [{"role": "system", "content": system_prompt}]
    # Filter out any empty-content messages — the API rejects them
    messages.extend(m for m in history if (m.get("content") or "").strip())
    messages.append({"role": "user", "content": user_text})

    body = {
        "model": _model(),
        "messages": messages,
        "temperature": 0.7,
        "stream": True,
        "reasoning_effort": None,  # JSON null → disable think mode on sarvam-m
    }
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": _api_key(),
    }

    content_chunks: list[str] = []
    reasoning_done = False
    extraction_yielded = False
    first_line_buffer = ""

    logger.debug(f"[LLM] streaming request: model={body['model']} msgs={len(messages)} turn_user='{user_text[:80]}'")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
        ) as client:
            async with client.stream("POST", _API_URL, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()  # must read body BEFORE exiting stream context
                    logger.error(f"[LLM] HTTP {resp.status_code}: {resp.text[:500]}")
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        # Before returning, handle extraction fallback if needed
                        if with_extraction and not extraction_yielded:
                            logger.warning(f"[Unified LLM] Stream ended, extraction_yielded={extraction_yielded}, buffer_len={len(first_line_buffer)}")
                            if first_line_buffer:
                                logger.warning(f"[Unified LLM] Stream ended without newline, buffer: {first_line_buffer[:100]}")
                                # Yield empty extraction dict
                                yield {"__extraction__": {}}
                                extraction_yielded = True
                                # Yield all buffered text
                                if first_line_buffer.strip():
                                    yield str(first_line_buffer)
                            else:
                                # No content at all, yield empty extraction
                                logger.warning(f"[Unified LLM] Stream ended with empty buffer, yielding empty extraction")
                                yield {"__extraction__": {}}
                                extraction_yielded = True
                        return
                    try:
                        chunk = json.loads(data)
                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}

                        # Skip reasoning-only chunks
                        if delta.get("reasoning_content") and not delta.get("content"):
                            reasoning_done = True
                            continue

                        content_delta = delta.get("content") or ""
                        if content_delta:
                            reasoning_done = True
                            content_chunks.append(content_delta)

                            # If extraction mode, buffer until first newline to parse JSON
                            if with_extraction and not extraction_yielded:
                                first_line_buffer += content_delta
                                # Log first few chunks to see what LLM outputs
                                if len(first_line_buffer) < 200:
                                    logger.debug(f"[Unified LLM] Buffering: '{content_delta}' (total: {len(first_line_buffer)} chars)")
                                if "\n" in first_line_buffer:
                                    # Split at first newline
                                    json_line, rest = first_line_buffer.split("\n", 1)
                                    json_line = json_line.strip()
                                    logger.info(f"[Unified LLM] Found newline, json_line: '{json_line[:200]}'")
                                    
                                    # Try to parse JSON from first line
                                    extraction_data = {}
                                    if json_line.startswith("{") and json_line.endswith("}"):
                                        try:
                                            extraction_data = json.loads(json_line)
                                            logger.info(f"[Unified LLM] Extracted JSON: {extraction_data}")
                                        except json.JSONDecodeError as je:
                                            logger.warning(f"[Unified LLM] Failed to parse JSON: {je}")
                                    else:
                                        logger.warning(f"[Unified LLM] First line not JSON format: '{json_line[:100]}'")
                                    
                                    # Yield extraction dict first (always as dict, even if empty)
                                    yield {"__extraction__": extraction_data}
                                    extraction_yielded = True
                                    
                                    # Yield remaining text as string
                                    if rest and rest.strip():
                                        yield str(rest)
                            else:
                                # Normal mode or after extraction - yield text directly as string
                                yield str(content_delta)
                    except Exception as e:
                        logger.warning(f"LLM streaming chunk error: {e}")
                        continue
    except httpx.HTTPStatusError as e:
        # aread() already called inside stream context; .text is available here
        logger.error(f"[LLM] HTTP {e.response.status_code} — body: {getattr(e.response, 'text', str(e))[:400]}")
    except Exception as e:
        logger.error(f"[LLM] Streaming error: {e}")

    if content_chunks:
        logger.debug(f"LLM streaming: {len(content_chunks)} chunks, "
                     f"{sum(len(c) for c in content_chunks)} chars")
    else:
        logger.warning("LLM streaming: no content chunks received"
                       f" (reasoning_done={reasoning_done})")


# --------------------------------------------------------------------------
#  Structured JSON extraction (used by extraction.py & name_correction.py)
# --------------------------------------------------------------------------
def _extract_json_from_text(text: str) -> dict:
    """Pull the first JSON object out of raw LLM output."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return {}


async def extract_json(
    system_prompt: str,
    user_prompt: str,
    timeout_s: float = 4.0,
) -> dict:
    """Call sarvam-m with reasoning DISABLED (null) for fast JSON extraction."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    def _call(temp: float):
        data = _chat_completion(
            messages=messages,
            temperature=temp,
            max_tokens=1500,
            reasoning_effort=None,          # JSON null → disable reasoning
        )
        content, reasoning, finish, usage = _parse(data)
        print(f"🔎 extract_json: finish={finish}, content={len(content)}ch, reasoning={len(reasoning)}ch, usage={usage}, temp={temp}")

        # Fallback: if content empty but reasoning has JSON, extract it
        if not content and reasoning:
            print("🔎 extract_json: content empty, extracting JSON from reasoning")
            content = _extract_json_from_reasoning(reasoning)

        return content

    for attempt, temp in enumerate([0.1, 0.4]):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_call, temp),
                timeout=timeout_s,
            )
            print(f"🔎 LLM extract_json raw (attempt {attempt+1}, temp={temp}): '{(raw or '')[:300]}'")
            result = _extract_json_from_text(raw or "")
            if result:
                return result
            print(f"⚠️ extract_json empty at temp={temp}, {'retrying' if attempt == 0 else 'giving up'}")
        except asyncio.TimeoutError:
            logger.warning(f"LLM extract_json timeout (attempt {attempt+1})")
            print(f"⏱️ LLM extract_json TIMEOUT (attempt {attempt+1})")
        except Exception as e:
            logger.error(f"LLM extract_json error (attempt {attempt+1}): {e}")
            print(f"❌ LLM extract_json error (attempt {attempt+1}): {e}")
            break

    return {}
