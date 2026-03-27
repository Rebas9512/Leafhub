"""
leafhub-sdk — Lightweight credential resolution for LeafHub consumers.

Zero external dependencies. Works with Python 3.10+.

Quick start::

    from leafhub_sdk import resolve

    # Get a ResolvedCredential with api_key, base_url, model, etc.
    cred = resolve("llm")
    print(cred.api_key, cred.model)

    # Or inject as environment variables (prefix from leafhub.toml)
    env = resolve("rewrite", as_env=True)
    os.environ.update(env)

Detection only (no credential resolution)::

    from leafhub_sdk import detect

    result = detect()
    if result.ready:
        hub = result.open_sdk()
"""

from leafhub_sdk.probe import ProbeResult, detect
from leafhub_sdk.resolve import (
    CredentialError,
    ResolvedCredential,
    resolve,
)
from leafhub_sdk.manifest import Binding, Manifest, get_default_alias, load_manifest

__version__ = "0.1.0"

__all__ = [
    # Credential resolution
    "resolve",
    "ResolvedCredential",
    "CredentialError",
    # Detection
    "detect",
    "ProbeResult",
    # Manifest
    "load_manifest",
    "get_default_alias",
    "Manifest",
    "Binding",
]
