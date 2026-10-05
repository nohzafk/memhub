"""Where everything lives. Read from the environment once, never from disk.

Construction must not touch the filesystem: `memhub.app:app` is built at import
time by uvicorn, and by the test suite on a machine that has no /var/lib/memhub.
Every path here is resolved lazily by whoever opens it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA = "/var/lib/memhub"
DEFAULT_TOKEN_FILE = "/etc/memhub/token"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token_file: Path
    vendor_dir: Path
    # Projects whose memories are machine-bound by default — a config repo for
    # one box. Whether a project is machine-bound is a property of the project,
    # identical on every machine, so it is held here once rather than in each
    # client's environment, where three copies could disagree about one repo.
    host_projects: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("MEMHUB_DATA") or DEFAULT_DATA),
            token_file=Path(env.get("MEMHUB_TOKEN_FILE") or DEFAULT_TOKEN_FILE),
            vendor_dir=Path(
                env.get("MEMHUB_VENDOR")
                or Path(__file__).resolve().parent.parent / "vendor"
            ),
            host_projects=tuple(
                part.strip()
                for part in (env.get("MEMHUB_HOST_PROJECTS") or "").split(":")
                if part.strip()
            ),
        )

    @property
    def optmem_dir(self) -> Path:
        return self.data_dir / "optmem"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".write.lock"

    @property
    def memo_py(self) -> Path:
        return self.vendor_dir / "memo.py"
