from __future__ import annotations

import json
from pathlib import Path

from .models import Job, UserProfile


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class LocalRepository:
    """Read-only local datasets used by the V1 perception demo."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._users = self._load_models("users.json", UserProfile, "user_id")
        self._jobs = self._load_models("jobs.json", Job, "job_id")

    def _load_models(self, filename: str, model_type, key: str):
        rows = json.loads((self.data_dir / filename).read_text(encoding="utf-8"))
        return {row[key]: model_type.model_validate(row) for row in rows}

    def get_user(self, user_id: str) -> UserProfile:
        try:
            return self._users[user_id]
        except KeyError as exc:
            raise ValueError(f"Unknown user_id: {user_id}") from exc

    def get_job(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise ValueError(f"Unknown job_id: {job_id}") from exc

    def list_jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def search_jobs(
        self,
        keyword: str | None = None,
        location: str | None = None,
        skills: list[str] | None = None,
        company: str | None = None,
    ) -> list[Job]:
        wanted_skills = {skill.casefold() for skill in skills or []}
        matches: list[Job] = []
        for job in self._jobs.values():
            haystack = " ".join([job.title, job.description, *job.requirements, *job.skills]).casefold()
            if keyword and keyword.casefold() not in haystack:
                continue
            if location and location.casefold() not in job.location.casefold():
                continue
            if company and company.casefold() not in job.company.casefold():
                continue
            job_skills = {skill.casefold() for skill in job.skills}
            if wanted_skills and not wanted_skills.issubset(job_skills):
                continue
            matches.append(job)
        return matches
