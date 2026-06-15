"""
SarvamMLLMProcessor — Custom Pipecat LLM processor for sarvam-m

Pipecat's built-in SarvamLLMService only supports sarvam-30b/105b.
This processor wraps our raw-HTTP llm.py streaming implementation so that
sarvam-m can be used natively in the Pipecat pipeline.

Frame flow (same contract as OpenAILLMService):
  LLMContextFrame → LLMFullResponseStartFrame
                  → TextFrame (one per streamed token)
                  → LLMFullResponseEndFrame
"""
import logging
import time
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import llm as llm_module

logger = logging.getLogger("sarvam_m_llm")


class SarvamMLLMProcessor(FrameProcessor):
    """
    Drop-in replacement for SarvamLLMService that uses sarvam-m via raw HTTP.

    Receives LLMContextFrame (produced by context_pair.user()), streams the
    response via our llm.generate_response_streaming(), and emits TextFrames
    that the downstream TTS processor can consume.
    """

    def __init__(
        self,
        on_control_message: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        super().__init__()
        self._on_control = on_control_message

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            # Pass all non-LLM frames downstream unchanged
            await self.push_frame(frame, direction)
            return

        # Extract messages from the LLM context
        messages = frame.context.messages if frame.context.messages else []
        if not messages:
            logger.warning("[SarvamMLLM] LLMContextFrame has no messages, skipping")
            return

        # Split system prompt + history + last user turn
        system_prompt = ""
        history: list[dict] = []
        user_text = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            if role == "system":
                system_prompt = content
            elif role == "user":
                if user_text:
                    history.append({"role": "user", "content": user_text})
                user_text = content
            elif role == "assistant":
                # Commit any pending user turn BEFORE appending assistant.
                # Without this, the history ends up [assistant(T1), user(T1)]
                # instead of [user(T1), assistant(T1)], causing a 400.
                if user_text:
                    history.append({"role": "user", "content": user_text})
                    user_text = ""
                # Skip empty assistant turns (e.g. from a failed LLM call)
                if content.strip():
                    history.append({"role": "assistant", "content": content})

        if not user_text:
            logger.warning("[SarvamMLLM] No user message found in context, skipping")
            return

        # Keep last 6 messages (3 exchanges) — booking draft is injected fresh into
        # the system prompt every turn, so older history is redundant and just inflates
        # prompt tokens (~2950 → ~1600), cutting TTFT significantly.
        if len(history) > 6:
            history = history[-6:]

        logger.debug(f"[LLM] turn start: '{user_text[:80]}' history={len(history)}msgs")

        await self.push_frame(LLMFullResponseStartFrame())

        t_start = time.monotonic()
        t_first_token: Optional[float] = None
        response_parts: list[str] = []

        # Stateful bracket filter + sentence accumulator.
        #
        # Problem: pushing one TextFrame per token causes Pipecat's TTS sentence
        # assembler to re-scan the entire accumulated buffer on every new frame and
        # re-fire run_tts() for already-seen sentences (observed: 60+ repeats of the
        # same sentence per turn).
        #
        # Fix: accumulate tokens here and only push a TextFrame when a complete
        # sentence boundary is detected (. ? ! । \n). The TTS receives full sentences,
        # never partial tokens, so each sentence is synthesised exactly once.
        _in_bracket: bool = False
        _bracket_buf: str = ""
        _sentence_buf: str = ""
        _SENTENCE_END = set(".?!\n\u0964\u0965")  # includes Devanagari danda

        async def _flush_sentence(text: str) -> None:
            """Push a complete sentence as a single TextFrame.
            Trailing space ensures Pipecat's TTS buffer inserts a word gap
            between consecutive sentences instead of jamming them together.
            """
            text = text.strip()
            if text:
                logger.debug(f"[LLM] sentence → TTS: '{text[:80]}'")
                await self.push_frame(TextFrame(text + " "))

        try:
            async for chunk in llm_module.generate_response_streaming(
                system_prompt=system_prompt,
                history=history,
                user_text=user_text,
                with_extraction=False,
            ):
                if not (isinstance(chunk, str) and chunk):
                    continue

                # Walk character-by-character: bracket filter + sentence split
                for ch in chunk:
                    if _in_bracket:
                        if ch == "]":
                            if _bracket_buf.strip():
                                logger.debug(f"[LLM] Filtered bracket: [{_bracket_buf}]")
                            _bracket_buf = ""
                            _in_bracket = False
                        else:
                            _bracket_buf += ch
                        continue

                    if ch == "[":
                        _in_bracket = True
                        _bracket_buf = ""
                        continue

                    _sentence_buf += ch

                    if ch in _SENTENCE_END and _sentence_buf.strip():
                        if t_first_token is None:
                            t_first_token = time.monotonic()
                        response_parts.append(_sentence_buf)
                        await _flush_sentence(_sentence_buf)
                        _sentence_buf = ""

        except Exception as e:
            logger.error(f"[LLM] Streaming error: {e}")

        # Flush any remaining text (response didn't end with punctuation)
        if _sentence_buf.strip():
            if t_first_token is None:
                t_first_token = time.monotonic()
            response_parts.append(_sentence_buf)
            await _flush_sentence(_sentence_buf)

        await self.push_frame(LLMFullResponseEndFrame())

        ttft_ms = round((t_first_token - t_start) * 1000) if t_first_token else 0
        total_ms = round((time.monotonic() - t_start) * 1000)
        logger.info(f"[LLM] done — TTFT={ttft_ms}ms total={total_ms}ms chunks={len(response_parts)}")

        if self._on_control and response_parts:
            full_text = "".join(response_parts)
            try:
                await self._on_control({"type": "response", "text": full_text})
                await self._on_control({
                    "type": "metrics",
                    "llm_ttft_ms": ttft_ms,
                    "llm_total_ms": total_ms,
                })
            except Exception:
                pass
