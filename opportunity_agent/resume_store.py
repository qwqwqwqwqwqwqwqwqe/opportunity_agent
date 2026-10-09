"""Atomic JSON journal for resume drafts; never mixed into browser snapshots."""
from __future__ import annotations
import json
import os
import threading
from pathlib import Path

from .resume_models import ResumeImportJob


class ResumeImportStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, ResumeImportJob]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {key: ResumeImportJob.model_validate(value) for key, value in data["jobs"].items()}

    def write(self, jobs: dict[str, ResumeImportJob]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "jobs": {k: v.model_dump(mode="json") for k, v in jobs.items()}},
                      handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

