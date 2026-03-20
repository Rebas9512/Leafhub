"""
Leafhub — local encrypted API key vault for LLM projects.

Usage:
    from leafhub import LeafHub

    hub    = LeafHub(token="lh-proj-xxx")
    key    = hub.get_key("gpt-4")
    client = hub.openai("gpt-4")

Auto-discovery (dotfile-based, no token required in code):
    from leafhub import LeafHub
    hub = LeafHub.from_directory()   # reads .leafhub in cwd or any parent

Onboarding detection:
    from leafhub import detect
    found = detect()
    if found.ready:
        hub = found.open_sdk()
"""

from .errors import (
    LeafHubError,
    InvalidTokenError,
    AliasNotBoundError,
    StorageNotFoundError,
    DecryptionError,
)
from .sdk import LeafHub, ProviderConfig
from .probe import ProbeResult, detect

__all__ = [
    "LeafHub",
    "ProviderConfig",
    "LeafHubError",
    "InvalidTokenError",
    "AliasNotBoundError",
    "StorageNotFoundError",
    "DecryptionError",
    # Onboarding / detection helpers
    "ProbeResult",
    "detect",
]
