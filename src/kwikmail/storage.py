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
        fields = {"to", "cc", "subject", "body", "in_reply_to"}
        if not isinstance(data, dict) or any(
            not (
                key in fields
                and isinstance(value, str)
                or key == "references"
                and isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
            for key, value in data.items()
        ):
            raise ValueError(f"Invalid draft at {self.path}")
        if "references" in data:
            data["references"] = tuple(data["references"])
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
