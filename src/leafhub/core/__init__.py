from pathlib import Path


def default_hub_dir() -> Path:
    """Return ~/.leafhub/, creating it with mode 700 if needed."""
    d = Path.home() / ".leafhub"
    d.mkdir(mode=0o700, exist_ok=True)
    return d
