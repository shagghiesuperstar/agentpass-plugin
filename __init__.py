"""AgentPass — agent-readiness (ARI-100) scanning as Hermes tools.

Wiring only: two schemas → two handlers, plus plugin settings → runtime config.
Nothing here opens a socket. The MCP client is constructed per tool call, so
plugin registration stays offline (``hermes plugins doctor`` blocks sockets
while ``register(ctx)`` runs) and a dead endpoint can never slow startup.
"""

from __future__ import annotations

import logging

try:  # Hermes imports the plugin as a package
    from . import schemas, tools
except ImportError:  # direct import (tests)
    import schemas  # type: ignore[no-redef]
    import tools  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

TOOLSET = "agentpass"


def _apply_settings(ctx) -> dict:
    """Read plugin settings, falling back to env vars and defaults.

    Every ctx call is defensive: a plugin that raises in register() is disabled
    entirely, and these keys are convenience, not correctness.
    """
    read = getattr(ctx, "get_config", None)
    base = tools.get_config()
    values: dict = {}
    if callable(read):
        for setting, key in (
            ("mcp_url", "endpoint"),
            ("timeout_seconds", "timeout"),
            ("max_retries", "max_retries"),
            ("cache_ttl_seconds", "cache_ttl"),
        ):
            try:
                values[key] = read(setting, default=base[key])
            except Exception:  # noqa: BLE001 - settings are optional
                continue
    return tools.configure(
        endpoint=values.get("endpoint"),
        timeout=values.get("timeout"),
        max_retries=values.get("max_retries"),
        cache_ttl=values.get("cache_ttl"),
    )


def register(ctx) -> None:
    """Called once by Hermes at plugin load."""
    config = _apply_settings(ctx)
    logger.debug("agentpass: endpoint=%s timeout=%ss retries=%s", config["endpoint"], config["timeout"], config["max_retries"])

    ctx.register_tool(
        name="agent_readiness_scan",
        toolset=TOOLSET,
        schema=schemas.SCAN,
        handler=tools.agent_readiness_scan,
    )
    ctx.register_tool(
        name="agent_readiness_fix_prompt",
        toolset=TOOLSET,
        schema=schemas.FIX_PROMPT,
        handler=tools.agent_readiness_fix_prompt,
    )
