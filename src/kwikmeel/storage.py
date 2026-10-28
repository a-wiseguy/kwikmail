import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import Draft


class DraftStore:
    """One recoverable draft per account, atomically replaced with owner-only permissions."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Draft:
        if not self.path.exists():
            return Draft()
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or any(
            key not in ("to", "cc", "subject", "body") or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise ValueError(f"Invalid draft at {self.path}")
        return Draft(**data)

    def save(self, draft: Draft) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".draft-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(asdict(draft), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
