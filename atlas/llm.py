"""Claude for specialists: one structured-JSON call, with clean failure.

Specialists use this for judgment over text (is this headline material? why
did this stock move?). Numbers never come from here; they come from the
books. If Claude is unavailable, refuses, or returns nothing usable,
`complete_json` returns None and the caller falls back to plain code.
"""

from __future__ import annotations

import json
import logging

from atlas.config import Settings

log = logging.getLogger(__name__)


class StructuredLLM:
    def __init__(self, settings: Settings, client=None, model: str | None = None, effort: str = "low"):
        self.settings = settings
        self.model = model or settings.worker_model
        self.effort = effort
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self.settings.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 8000) -> dict | None:
        if not self.available:
            return None
        import anthropic

        try:
            resp = self._get_client().beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as exc:  # auth, rate limit, connection, 4xx/5xx
            log.warning("Claude call failed (%s); falling back to rules", type(exc).__name__)
            return None
        if resp.stop_reason in {"refusal", "max_tokens"}:
            log.warning("Claude stopped with %s; falling back to rules", resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("Claude returned non-JSON; falling back to rules")
            return None
        return data if isinstance(data, dict) else None
