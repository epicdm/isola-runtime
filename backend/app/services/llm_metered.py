"""Metered wrapper around any LLM client.

Wiring this at the two call sites that create clients is what closes the
bypass: ``heartbeat.py::_execute_heartbeat`` (up to 20 ``complete()`` calls
per autonomous wake, currently unmetered) and ``websocket.py`` (human chat,
which increments a counter nothing reads).

Usage -- one line at each call site::

    client = MeteredLLMClient(
        create_llm_client(provider=..., api_key=..., model=..., base_url=...),
        agent_id=agent_id, tenant_id=tenant_id,
        provider=model_provider, model=model_model, model_id=model_id,
    )

Every ``await client.complete(...)`` then:
  1. reserves budget atomically -- refused calls never reach the provider;
  2. calls the provider;
  3. writes one ``llm_call_telemetry`` row (success or failure) with cost.

Retries: each attempt reserves separately, because each attempt is a real
provider call. A caller that wants a retry budget should set the agent cap
accordingly rather than exempting retries.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from loguru import logger

from app.services.usage_meter import (
    UsageLimitExceeded,
    record_llm_call,
    reserve_llm_call,
)


class MeteredLLMClient:
    """Wraps an LLM client so no provider call happens without budget."""

    def __init__(
        self,
        inner,
        *,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str | None = None,
        model: str | None = None,
        model_id: uuid.UUID | None = None,
        session_factory=None,
        intent: str | None = None,
        cost_billed_to: str = "epic",
    ):
        self._inner = inner
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._provider = provider
        self._model = model
        self._model_id = model_id
        self._intent = intent
        self._cost_billed_to = cost_billed_to
        if session_factory is None:
            from app.database import async_session as session_factory  # noqa: PLC0415
        self._session_factory = session_factory

    async def complete(self, *args, **kwargs):
        return await self._metered("complete", *args, **kwargs)

    async def stream(self, *args, **kwargs):
        """MUST be metered too.

        The human-chat tool loop (app/services/llm/caller.py) calls
        ``client.stream()``, not ``complete()``. Metering only ``complete()``
        would leave chat entirely unmetered while __getattr__ passed
        ``stream`` straight through to the raw client -- i.e. the same
        fail-open shape as the bug being fixed, one method along.
        """
        return await self._metered("stream", *args, **kwargs)

    async def _metered(self, method: str, *args, **kwargs):
        async with self._session_factory() as db:
            # Raises UsageLimitExceeded -> the provider is never called.
            await reserve_llm_call(self._agent_id, db)

        started = time.monotonic()
        try:
            response = await getattr(self._inner, method)(*args, **kwargs)
        except Exception as exc:
            await self._record(None, None, started, success=False,
                               error_class=type(exc).__name__)
            raise

        usage = getattr(response, "usage", None) or {}
        await self._record(
            usage.get("prompt_tokens") or usage.get("input_tokens"),
            usage.get("completion_tokens") or usage.get("output_tokens"),
            started,
            success=True,
        )
        return response

    async def _record(self, in_tok, out_tok, started, *, success, error_class=None):
        try:
            async with self._session_factory() as db:
                await record_llm_call(
                    db,
                    tenant_id=self._tenant_id,
                    agent_id=self._agent_id,
                    provider=self._provider,
                    model=self._model,
                    model_id=self._model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=success,
                    error_class=error_class,
                    intent=self._intent,
                    cost_billed_to=self._cost_billed_to,
                    now=datetime.now(timezone.utc),
                )
        except Exception as exc:  # accounting must never break the call path
            logger.warning(f"[usage_meter] telemetry write failed for {self._agent_id}: {exc}")

    #: Methods that reach a provider. Anything added to a client class that
    #: makes a billable call MUST be listed here, or __getattr__ will pass it
    #: through unmetered.
    _PROVIDER_METHODS = ("complete", "stream")

    def __getattr__(self, name):
        # close() and other non-billable helpers pass through untouched.
        if name in self._PROVIDER_METHODS:          # pragma: no cover - defensive
            raise AttributeError(
                f"{name} must be metered explicitly, not passed through"
            )
        return getattr(self._inner, name)


__all__ = ["MeteredLLMClient", "UsageLimitExceeded"]
