"""LLM service: structured extraction and streamed audit narratives.

With a configured model, extraction uses ``llm.with_structured_output(Model)``
and narratives stream token-by-token from the model. Without one (or on LLM
timeout/failure) a deterministic engine produces the same Pydantic types, and
narratives stream through ``GenericFakeChatModel`` so the streaming path is
identical in CI and production.

LLM output is advisory: it never changes the numerical compliance score.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from sha_compliance_agent.config import AuditSettings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SUMMARY_TAG = "audit_summary"


class LLMService:
    """Wraps an optional chat model with timeouts and deterministic fallbacks."""

    def __init__(self, model: BaseChatModel | None, *, timeout_s: float) -> None:
        self._model = model
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(cls, settings: AuditSettings) -> LLMService:
        model: BaseChatModel | None = None
        if settings.llm_model:
            from langchain.chat_models import init_chat_model

            model = init_chat_model(settings.llm_model, max_retries=2)
        return cls(model, timeout_s=settings.llm_timeout_s)

    @property
    def online(self) -> bool:
        return self._model is not None

    async def extract(
        self,
        schema: type[T],
        *,
        system: str,
        content: str,
        fallback: Callable[[], T],
    ) -> tuple[T, str]:
        """Structured extraction into ``schema``.

        Returns:
            The parsed model and the engine that produced it:
            ``"llm"``, ``"deterministic"`` or ``"deterministic_fallback"``.
        """
        if self._model is None:
            return fallback(), "deterministic"
        try:
            runnable = self._model.with_structured_output(schema)
            raw = await asyncio.wait_for(
                runnable.ainvoke([SystemMessage(system), HumanMessage(content)]),
                timeout=self._timeout_s,
            )
            return schema.model_validate(raw), "llm"
        except GraphBubbleUp:
            raise
        except Exception:  # noqa: BLE001 - LLM failure must never block the audit
            logger.warning(
                "structured extraction into %s failed; using fallback",
                schema.__name__,
                exc_info=True,
            )
            return fallback(), "deterministic_fallback"

    async def stream_narrative(self, *, system: str, content: str, fallback_text: str) -> str:
        """Stream a narrative token-by-token and return the full text.

        Tokens surface to callers through ``graph.astream(stream_mode="messages")``
        or ``astream_events``, tagged with :data:`SUMMARY_TAG`.
        """
        messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(content)]
        if self._model is not None:
            try:
                return await self._stream(self._model, messages)
            except GraphBubbleUp:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("narrative streaming failed; using fallback", exc_info=True)
        fake = GenericFakeChatModel(messages=iter([AIMessage(fallback_text)]))
        return await self._stream(fake, messages)

    async def _stream(self, model: BaseChatModel, messages: list[BaseMessage]) -> str:
        parts: list[str] = []
        tagged = model.with_config(tags=[SUMMARY_TAG], run_name=SUMMARY_TAG)
        async with asyncio.timeout(self._timeout_s):
            async for chunk in tagged.astream(messages):
                parts.append(chunk.text)
        return "".join(parts)
