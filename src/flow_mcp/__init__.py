"""Flow MCP — Google Flow image generation as an MCP tool for Claude Code.

Calls the ``batchGenerateImages`` API directly from a Playwright browser
context, bypassing the Flow Agent chat quota (~10/day).
"""

from __future__ import annotations

import sys

import structlog

__version__ = "0.1.0"

# ── Global structlog config: always write to stderr ─────────────────────
# The MCP transport uses stdout for JSON-RPC — any stray log line there
# corrupts the protocol. We configure structlog to always target stderr.
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    cache_logger_on_first_use=True,
)
