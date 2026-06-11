"""MCP Agent System — server entrypoint.

Exposes 8 tools and 3 resources over either ``stdio`` (default, used by
Claude Desktop) or ``sse`` (used by remote MCP hosts).

Tools:
    start_session, send_prompt, get_context_usage, log_correction,
    transfer_context, get_session_state, query_codebase, create_invoice

Resources:
    agent://session/{id}, agent://corrections/{id}, agent://codebase/{session_id}

Sponsor integrations are wired here:
    * CLōD     - LLM gateway (replaces Anthropic SDK)
    * Greptile - codebase search (query_codebase tool)
    * AllScale - stablecoin invoicing (create_invoice tool)
    * BGA      - on-chain correction registry (log_correction + transfer_context)
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .agents.orchestrator import AgentOrchestrator
from .db.session import init_engine
from .integrations.allscale import AllScaleClient
from .integrations.chain import ChainClient
from .integrations.clod import ClodClient
from .integrations.greptile import GreptileClient
from .resources.codebase_resource import render_codebase_payload
from .resources.corrections_resource import render_corrections_payload
from .resources.session_resource import render_session_payload
from .schemas.tools import (
    CreateInvoiceInput,
    GetContextUsageInput,
    GetSessionStateInput,
    LogCorrectionInput,
    QueryCodebaseInput,
    SendPromptInput,
    StartSessionInput,
    TransferContextInput,
)
from .tools.create_invoice import handle_create_invoice
from .tools.get_context_usage import handle_get_context_usage
from .tools.get_session_state import handle_get_session_state
from .tools.log_correction import handle_log_correction
from .tools.query_codebase import handle_query_codebase
from .tools.send_prompt import handle_send_prompt
from .tools.start_session import handle_start_session
from .tools.transfer_context import handle_transfer_context
from .utils.env import get_settings
from .utils.logger import configure_logging, get_logger


def build_server() -> tuple[FastMCP, AgentOrchestrator, dict[str, Any]]:
    """Construct the MCP server with sponsor clients wired in.

    Returns the FastMCP instance, the orchestrator, and a dict of sponsor
    clients so callers (tests, scripts) can introspect or replace them.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("mcp_agent_system.server")

    init_engine(settings.database_url)

    clod = ClodClient()
    chain = ChainClient()
    greptile = GreptileClient()
    allscale = AllScaleClient()

    orchestrator = AgentOrchestrator(clod=clod, chain=chain)

    log.info(
        "boot.sponsors_wired",
        clod_active_model=clod.active_model,
        clod_summary_model=clod.summary_model,
        greptile_base=greptile.base_url,
        allscale_base=allscale.base_url,
        allscale_signed=bool(allscale.api_key and allscale.api_secret),
        bga_chain_id=chain.chain_id,
        bga_enabled=chain.enabled,
    )

    recovered = orchestrator.rehydrate_active_sessions(
        transfer_threshold=settings.context_transfer_threshold
    )
    if recovered:
        log.info("boot.recovered_sessions", session_ids=recovered)

    server = FastMCP(
        name="mcp-agent-system",
        instructions=(
            "Agent System MCP server. Call start_session first, then send_prompt, "
            "log_correction, query_codebase (Greptile), transfer_context, "
            "create_invoice (AllScale), and get_session_state. Corrections are "
            "anchored on-chain via the BGA CorrectionRegistry contract."
        ),
    )

    # ── Tools ────────────────────────────────────────────────────────────

    @server.tool()
    async def start_session(
        user_id: str,
        model: str = "anthropic/claude-opus-4",
        system_prompt: str | None = None,
    ) -> str:
        """Initialize a new ActiveAgent + HistoryAgent pair for a user session."""
        out = await handle_start_session(
            StartSessionInput(
                user_id=user_id, model=model, system_prompt=system_prompt
            ),
            orchestrator,
        )
        return out.model_dump_json()

    @server.tool()
    async def send_prompt(session_id: str, message: str) -> str:
        """Route a user message to the Active Agent. Auto-fires transfer at threshold."""
        out = await handle_send_prompt(
            SendPromptInput(session_id=session_id, message=message), orchestrator
        )
        return out.model_dump_json()

    @server.tool()
    async def get_context_usage(session_id: str) -> str:
        """Return the current token consumption stats for the session."""
        out = await handle_get_context_usage(
            GetContextUsageInput(session_id=session_id), orchestrator
        )
        return out.model_dump_json()

    @server.tool()
    async def log_correction(
        session_id: str,
        original: str,
        correction: str,
        severity: str = "low",
        category: str | None = None,
    ) -> str:
        """Record a user correction. Hashed proof is anchored on the BGA registry."""
        out = await handle_log_correction(
            LogCorrectionInput(
                session_id=session_id,
                original=original,
                correction=correction,
                severity=severity,  # type: ignore[arg-type]
                category=category,  # type: ignore[arg-type]
            ),
            orchestrator,
            chain,
        )
        return out.model_dump_json()

    @server.tool()
    async def transfer_context(session_id: str) -> str:
        """Trigger the context handoff protocol manually."""
        out = await handle_transfer_context(
            TransferContextInput(session_id=session_id), orchestrator
        )
        return out.model_dump_json()

    @server.tool()
    async def get_session_state(session_id: str) -> str:
        """Return a complete snapshot of a session."""
        out = await handle_get_session_state(
            GetSessionStateInput(session_id=session_id)
        )
        return out.model_dump_json()

    @server.tool()
    async def query_codebase(
        session_id: str,
        question: str,
        repo_remote: str | None = None,
        repository: str | None = None,
        branch: str = "main",
    ) -> str:
        """Ask a Greptile-grounded question about a connected GitHub repo."""
        out = await handle_query_codebase(
            QueryCodebaseInput(
                session_id=session_id,
                question=question,
                repo_remote=repo_remote,
                repository=repository,
                branch=branch,
            ),
            orchestrator,
            greptile,
        )
        return out.model_dump_json()

    @server.tool()
    async def create_invoice(session_id: str, stablecoin: str | None = None) -> str:
        """Create an AllScale stablecoin invoice for the session's token usage."""
        out = await handle_create_invoice(
            CreateInvoiceInput(session_id=session_id, stablecoin=stablecoin),
            orchestrator,
            allscale,
        )
        return out.model_dump_json()

    # ── Resources ────────────────────────────────────────────────────────

    @server.resource("agent://session/{session_id}")
    def session_resource(session_id: str) -> str:
        return json.dumps(render_session_payload(session_id), indent=2)

    @server.resource("agent://corrections/{session_id}")
    def corrections_resource(session_id: str) -> str:
        return json.dumps(render_corrections_payload(session_id), indent=2)

    @server.resource("agent://codebase/{session_id}")
    def codebase_resource(session_id: str) -> str:
        return json.dumps(render_codebase_payload(session_id), indent=2)

    return server, orchestrator, {
        "clod": clod,
        "greptile": greptile,
        "allscale": allscale,
        "chain": chain,
    }


_TOOL_SUMMARY = [
    ("start_session", "Initialize an ActiveAgent + HistoryAgent pair"),
    ("send_prompt", "Route a message to the Active Agent"),
    ("get_context_usage", "Token consumption stats for a session"),
    ("log_correction", "Record a correction, anchored on-chain (BGA)"),
    ("transfer_context", "Trigger the context handoff protocol"),
    ("get_session_state", "Full session snapshot"),
    ("query_codebase", "Greptile-grounded codebase Q&A"),
    ("create_invoice", "AllScale stablecoin invoice for usage"),
]


def _add_web_routes(app: Any) -> None:
    """Attach a human-friendly landing page and health check to the SSE app."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": "mcp-agent-system"})

    async def landing(request: Request) -> HTMLResponse:
        base = str(request.base_url).rstrip("/")
        sse_url = f"{base}/sse"
        tools_html = "".join(
            f"<tr><td><code>{name}</code></td><td>{desc}</td></tr>"
            for name, desc in _TOOL_SUMMARY
        )
        mcp_json = (
            '{\n  "mcpServers": {\n    "syncsentry": {\n'
            f'      "url": "{sse_url}"\n'
            "    }\n  }\n}"
        )
        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SyncSentry MCP Server</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background: #0b0f17; color: #e6edf3;
         font: 16px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 48px 24px 64px; }}
  h1 {{ font-size: 28px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 32px 0 8px; }}
  .ok {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
        background: #12351f; color: #3fb950; font-size: 13px; }}
  .muted {{ color: #8b949e; }}
  pre {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 14px 16px; overflow-x: auto; font-size: 14px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  td {{ border-top: 1px solid #21262d; padding: 7px 10px 7px 0; vertical-align: top; }}
  a {{ color: #58a6ff; }}
</style>
</head>
<body>
<main>
  <h1>SyncSentry <span class="muted">MCP Server</span></h1>
  <p><span class="ok">&#9679; running</span></p>
  <p class="muted">This is a Model Context Protocol server, not a website.
  Connect to it from an MCP client (Cursor, Claude Desktop, etc.).</p>

  <h2>Connect from Cursor / Claude</h2>
  <p>Add this to your <code>mcp.json</code> (Cursor: Settings &rarr; MCP):</p>
  <pre><code>{mcp_json}</code></pre>

  <h2>Quick test from a terminal</h2>
  <pre><code>curl -N {sse_url}</code></pre>
  <p class="muted">A healthy server replies with an <code>event: endpoint</code>
  line and keeps the connection open.</p>

  <h2>Available tools</h2>
  <table>{tools_html}</table>

  <h2>Endpoints</h2>
  <table>
    <tr><td><code>GET /sse</code></td><td>MCP SSE connection endpoint</td></tr>
    <tr><td><code>POST /messages/</code></td><td>MCP client message channel</td></tr>
    <tr><td><code>GET /healthz</code></td><td>Health check (JSON)</td></tr>
  </table>
</main>
</body>
</html>"""
        return HTMLResponse(html)

    app.router.routes.insert(0, Route("/", landing, methods=["GET"]))
    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))


def main() -> None:
    """CLI entrypoint. Selects stdio or SSE based on ``MCP_TRANSPORT``."""
    settings = get_settings()
    server, _, _ = build_server()
    if settings.mcp_transport == "sse":
        import uvicorn

        app = server.sse_app()
        _add_web_routes(app)
        uvicorn.run(
            app,
            host=settings.sse_host,
            port=settings.sse_port,
            # Respect X-Forwarded-* from Cloud Run's proxy so generated
            # URLs on the landing page use https and the public hostname.
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    else:
        import anyio

        anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
