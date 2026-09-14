"""Repository dotenv loading with explicit process-environment precedence."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def load_repository_environment(
    repo_root: Path,
    *,
    loader: Optional[Callable[..., object]] = None,
) -> Optional[Path]:
    """Load repository dotenv defaults without replacing process values.

    ``.env.dev`` remains preferred over ``.env``.  Explicit values inherited
    from systemd, a launcher, or a parent process always win.
    """
    if loader is None:
        from dotenv import load_dotenv

        loader = load_dotenv

    root = Path(repo_root)
    env_dev = root / ".env.dev"
    env_file = root / ".env"
    selected = env_dev if env_dev.exists() else env_file if env_file.exists() else None
    if selected is None:
        return None

    loader(selected, override=False)
    return selected
