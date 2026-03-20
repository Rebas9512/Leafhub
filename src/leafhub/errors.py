"""
Leafhub exception hierarchy.

Defined in a standalone module to avoid circular imports:
  __init__.py imports LeafHub (from sdk.py) AND errors,
  sdk.py needs to import errors — keeping errors here breaks the cycle.
"""


class LeafHubError(Exception):
    """Base class for all Leafhub errors."""


class InvalidTokenError(LeafHubError):
    """Project token is invalid or the project is inactive."""


class AliasNotBoundError(LeafHubError):
    """The requested model alias is not bound to this project."""


class StorageNotFoundError(LeafHubError):
    """~/.leafhub/ does not exist or has not been initialised."""


class DecryptionError(LeafHubError):
    """Master key is wrong, or providers.enc is corrupt."""
