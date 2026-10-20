"""nmemory — Grounded memory MCP server for LLM agents.

Python port of the canonical Rust nmemory crate.
"""


class NMemoryError(Exception):
    """Base exception for all nMEMORY errors."""


__version__ = "0.8.0"
