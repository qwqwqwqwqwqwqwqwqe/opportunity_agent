"""Bounded background resume jobs and revision-checked review/confirmation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .config import _setting
from .conversation_store import DeletedConversation, RevisionConflict
from .resume_extraction import ResumeExtractionSkill
from .resume_mineru import MinerUParser
from .resume_models import (EXPERIENCE_FIELDS, MAX_TEXT_CHARS, ParsedResumeDocument, ResumeBlock,
                            ResumeDraft, ResumeImportJob, RESUME_FIELDS, utcnow)
from .resume_parsers import redact_contacts, validate_file
from .resume_store import ResumeImportStore
from .session_service import SessionService

RUNNING = {"queued", "reading", "cloud_parsing", "extracting"}
ACTIVE = RUNNING | {"awaiting_consent", "review", "confirming"}


class ResumeImportService:
    def __init__(self, conversations, parser=None, extractor_factory=ResumeExtractionSkill, cloud=None):
        self.conversations = conversations
        self.store = ResumeImportStore(conversations.path.with_name("resume_imports.json"))
        self.temp_dir = conversations.path.parent / ".resume_tmp"
        self.parser, self.extractor_factory, self.cloud = parser, extractor_factory, cloud or MinerUParser()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resume")
        self.stop = threading.Event()
        with self.store.lock:
            jobs = self.store.read()
            for job in jobs.values():
                if job.status in RUNNING:
                    job.status, job.error = "interrupted", "服务重启中断了解析，可重试已读取文本或重新上传"
                    self._drop_file(job)
                if job.status == "confirming":
                    record = self.conversations.get(job.session_id)
                    event = next((e for e in (record or {}).get("state", {}).get("user_events", [])
                                  if e["request_id"] == "resume:" + job.import_id and e["status"] == "processed"), None)
                    job.status = "confirmed" if event else "review"
                    if event:
                        job.parsed = None
                        job.confirmed_revision = record["revision"]
            if jobs:
                self.store.write(jobs)
        self.sweeper = threading.Thread(target=self._sweep_loop, daemon=True)
        self.sweeper.start()

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.store.lock:
            jobs = self.store.read()
            for job in jobs.values():
                if job.status in RUNNING | {"awaiting_consent"}:
                    job.status, job.error = "interrupted", "服务已停止，请重试文本或重新上传"
                    job.revision += 1
                    self._drop_file(job)
            if jobs:
                self.store.write(jobs)
        self.sweep()

    def _conversation(self, sid):
        if self.conversations.is_deleted(sid):
            raise DeletedConversation("会话已删除")
        record = self.conversations.get(sid)
        if not record:
            raise ValueError("请先创建会话")
        return record

    def _drop_file(self, job):
        if job.temp_path:
            path = Path(job.temp_path).resolve()
            if path.parent != self.temp_dir.resolve() or path.stem != job.import_id:
                raise ValueError("临时文件路径校验失败")
            try:
                path.unlink(missing_ok=True)
                job.temp_path = None
            except PermissionError:
                # A cancelled Windows parser can briefly retain its file handle.
                # The sweeper retries after the child exits.
                pass

    def _sweep_loop(self):
        while not self.stop.wait(30):
            try:
                self.sweep()
            except (OSError, ValueError):
                pass

    def sweep(self):
        with self.store.lock:
            jobs = self.store.read()
            changed = False
            for job in jobs.values():
                deleted = self.conversations.is_deleted(job.session_id)
                if deleted or job.status == "awaiting_consent" and job.expires_at and job.expires_at <= utcnow():
                    job.status = "cancelled"
                    job.error = "已取消或等待授权超时，请重新上传"
                    job.parsed = None
                    changed = True
                if job.temp_path and job.status not in RUNNING | {"awaiting_consent"}:
                    self._drop_file(job)
                    changed = True
                if deleted:
                    job.draft, job.original_draft, job.confirmation_request = ResumeDraft(), None, None
            if changed:
                self.store.write(jobs)

    def _public(self, job, record):
        output = job.model_dump(mode="json", exclude={"temp_path", "confirmation_request", "file_hash", "original_draft"})
        output["cloud_configured"] = self.cloud.enabled
        output["current_profile_revision"] = record.get("revision", 0)
        output["current_profile"] = {k: record["state"].get("profile", {}).get(k)
                                      for k in RESUME_FIELDS}
        return output

    def list(self, sid):
        record = self._conversation(sid)
        self.sweep()
        with self.store.lock:
            return [self._public(j, record) for j in self.store.read().values()
                    if j.session_id == sid and j.status != "cancelled"]

    def get(self, sid, import_id):
        record = self._conversation(sid)
        self.sweep()
        with self.store.lock:
            job = self._job(self.store.read(), sid, import_id)
            return self._public(job, record)

    @staticmethod
    def _job(jobs, sid, import_id):
        job = jobs.get(import_id)
        if not job or job.session_id != sid:
            raise ValueError("找不到此会话的简历导入")
        return job

    def upload(self, sid, request_id, filename, data, enhanced=False):
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id 无效")
        if not isinstance(filename, str) or not filename or len(filename) > 240 or any(ord(c) < 32 for c in filename):
            raise ValueError("文件名无效")
        digest = hashlib.sha256(data).hexdigest()
        with self.conversations.session_lock(sid), self.store.lock:
            record = self._conversation(sid)
            jobs = self.store.read()
            existing = next((j for j in jobs.values() if j.session_id == sid and j.request_id == request_id), None)
            if existing:
                if existing.file_hash != digest:
                    raise RevisionConflict("同一请求 ID 不能上传不同文件")
                return self._public(existing, record)
            if any(j.session_id == sid and j.status in ACTIVE for j in jobs.values()):
                raise RevisionConflict("请先确认或取消当前简历导入")
            suffix = validate_file(filename, data)
            import_id = uuid4().hex
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            path = self.temp_dir / (import_id + suffix)
            job = ResumeImportJob(import_id=import_id, session_id=sid, request_id=request_id,
                filename=Path(filename.replace("\\", "/")).name[:200], file_hash=digest,
                temp_path=str(path), base_profile_revision=record["revision"],
                mode="cloud_requested" if enhanced else "pending")
            jobs[import_id] = job
            try:
                with path.open("xb") as handle:
                    handle.write(data)
                self.store.write(jobs)
                self.pool.submit(self._run, sid, import_id, "local")
            except Exception:
                self._drop_file(job)
                job.status, job.error = "failed", "上传未能保存，请重新上传"
                try:
                    self.store.write(jobs)
                except OSError:
                    pass
                raise
            return self._public(job, record)

    def _update(self, sid, import_id, **changes):
        with self.store.lock:
            jobs = self.store.read()
            job = self._job(jobs, sid, import_id)
            if job.status in {"cancelled", "confirmed"} or self.conversations.is_deleted(sid):
                return None
            for field, value in changes.items():
                setattr(job, field, value)
            job.revision += 1
            job.updated_at = utcnow()
            self.store.write(jobs)
            return job

    def _cancelled(self, sid, import_id):
        with self.store.lock:
            job = self.store.read().get(import_id)
            return self.stop.is_set() or not job or job.status == "cancelled" or self.conversations.is_deleted(sid)

    def _parse(self, path, sid, import_id):
        if self.parser:
            return self.parser(path)
        try:
            limit = max(10, min(240, int(_setting("RESUME_PARSE_TIMEOUT_SECONDS") or "120")))
        except ValueError:
            limit = 120
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        child = subprocess.Popen([sys.executable, "-B", "-m", "opportunity_agent.resume_worker", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + limit
        try:
            while True:
                if self._cancelled(sid, import_id) or time.monotonic() >= deadline:
                    raise TimeoutError("文件读取已取消或超时")
                try:
                    out, _ = child.communicate(timeout=1)
                    result = json.loads(out.decode("utf-8"))
                    if "error" in result:
                        raise ValueError(result["error"])
                    return ParsedResumeDocument.model_validate(result["document"])
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def _run(self, sid, import_id, mode):
        try:
            job = self._update(sid, import_id, status="cloud_parsing" if mode == "cloud" else "reading",
                               error=None)
            if not job:
                return
            if mode == "retry":
                document = job.parsed
                if not document:
                    raise ValueError("已无可重试文本，请重新上传")
            elif mode == "cloud":
                try:
                    timeout = int(_setting("RESUME_PARSE_TIMEOUT_SECONDS") or "120")
                except ValueError:
                    timeout = 120
                document = self.cloud.parse(Path(job.temp_path),
                    cancelled=lambda: self._cancelled(sid, import_id), timeout=max(10, min(240, timeout)))
            else:
                document = self._parse(Path(job.temp_path), sid, import_id)
            if mode == "local" and (document.needs_cloud or job.mode == "cloud_requested"):
                self._update(sid, import_id, status="awaiting_consent", parsed=document,
                             expires_at=utcnow() + timedelta(minutes=30))
                return
            job = self._update(sid, import_id, status="extracting", parsed=document)
            if not job:
                return
            # Parse complete: the binary is no longer needed during LLM/review.
            with self.store.lock:
                jobs = self.store.read()
                self._drop_file(jobs[import_id])
                self.store.write(jobs)
            skill = self.extractor_factory()
            completed_components = set(job.completed_components)

            def persist_partial(partial_draft, components):
                # A later component can time out or the service can restart.
                # Write each valid component before issuing the next request so
                # the review sidebar never loses already extracted rows.
                nonlocal completed_components
                completed_components = set(components)
                self._update(sid, import_id, status="extracting", draft=partial_draft,
                             completed_components=sorted(completed_components), mode="llm")

            if isinstance(skill, ResumeExtractionSkill):
                draft = skill.generate(document, existing=job.draft,
                                       completed_components=completed_components,
                                       on_progress=persist_partial)
            else:
                # Keep the narrow extractor test/dedicated extension contract:
                # third-party extractors only need generate(document).
                draft = skill.generate(document)
            record = self._conversation(sid)
            profile = record["state"].get("profile", {})
            from .normalizer import ProfileNormalizer
            for fact in draft.facts:
                old = profile.get(fact.field)
                normalized = ProfileNormalizer().normalize_fact(fact).value
                # Completed coursework is cumulative evidence, unlike a
                # scalar score or a target preference.  A resume should add
                # clearly listed courses to the courses the user has already
                # confirmed, not silently deselect the entire field because
                # the two lists differ.  The merged result remains a review
                # draft and is only committed after the user confirms it.
                if fact.field == "completed_courses" and isinstance(old, list) and isinstance(normalized, list):
                    fact.normalized_value = list(dict.fromkeys([*old, *normalized]))
                    fact.selected = True
                    continue
                if old not in (None, [], "") and old != normalized:
                    fact.selected = False
            self._update(sid, import_id, status="review", draft=draft, original_draft=draft.model_copy(deep=True),
                base_profile_revision=record["revision"], mode=skill.mode, error=skill.error,
                completed_components=sorted(completed_components))
        except Exception as exc:
            # Never persist provider error bodies, URLs or tokens.
            error = str(exc) if isinstance(exc, ValueError) and mode != "cloud" else "解析服务不可用或超时，可重试文本或重新上传"
            self._update(sid, import_id, status="failed", error=error)
        finally:
            self.sweep()

    def consent(self, sid, import_id, accept):
        with self.store.lock:
            job = self._job(self.store.read(), sid, import_id)
            if job.status != "awaiting_consent":
                raise RevisionConflict("当前任务未等待云端授权")
            if accept and not self.cloud.enabled:
                raise ValueError("请在 .env 配置 MINERU_API_TOKEN 后重试；尚未发送任何文件")
            if not accept and not job.parsed:
                raise ValueError("无本地文本，请粘贴文本或重新上传")
            self._update(sid, import_id, status="queued", cloud_consent=accept,
                         cloud_consent_at=utcnow() if accept else None)
            self.pool.submit(self._run, sid, import_id, "cloud" if accept else "retry")
        return self.get(sid, import_id)

    def save_draft(self, sid, import_id, payload):
        draft = ResumeDraft.model_validate(payload["draft"])
        with self.conversations.session_lock(sid), self.store.lock:
            record = self._conversation(sid)
            jobs = self.store.read()
            job = self._job(jobs, sid, import_id)
            if job.status != "review" or payload.get("revision") != job.revision:
                raise RevisionConflict("草稿已变更，请刷新后核对")
            if payload.get("profile_revision") != record["revision"]:
                raise RevisionConflict("另一窗口更新了画像，请重新查看当前值后保存")
            job.draft = draft
            job.base_profile_revision = record["revision"]
            job.revision += 1
            job.updated_at = utcnow()
            self.store.write(jobs)
            return self._public(job, record)

    def retry(self, sid, import_id, text=None):
        with self.store.lock:
            job = self._job(self.store.read(), sid, import_id)
            if job.status not in {"review", "failed", "interrupted", "awaiting_consent"}:
                raise RevisionConflict("此任务当前不能重试")
            parsed = job.parsed
            if text is not None:
                if not isinstance(text, str) or not 0 < len(text.strip()) <= MAX_TEXT_CHARS:
                    raise ValueError("粘贴文本不能为空且不能超过 60000 字符")
                parsed = ParsedResumeDocument(parser="user_paste", blocks=[
                    ResumeBlock(block_id="paste1", text=redact_contacts(text.strip()), locator="用户粘贴文本")])
            if not parsed or not parsed.blocks:
                raise ValueError("没有可重试文本，请重新上传或粘贴文本")
            # Pasted text is a new source, so old component coverage and its
            # draft must not suppress extraction of the replacement text.
            retry_changes = {"status": "queued", "parsed": parsed, "attempt": job.attempt + 1}
            if text is not None:
                retry_changes.update(draft=ResumeDraft(), original_draft=None, completed_components=[])
            self._update(sid, import_id, **retry_changes)
            self.pool.submit(self._run, sid, import_id, "retry")
        return self.get(sid, import_id)

    def reextract(self, sid, import_id):
        """Rebuild an unconfirmed draft from the securely retained parsed text.

        This is deliberately distinct from ``retry``: retry resumes only
        incomplete AI components and keeps the user's draft edits, whereas a
        re-extraction reruns rules and every AI component after parser or
        extraction rules have changed.
        """
        with self.store.lock:
            job = self._job(self.store.read(), sid, import_id)
            if job.status not in {"review", "failed", "interrupted", "awaiting_consent"}:
                raise RevisionConflict("此任务当前不能重新提取")
            if not job.parsed or not job.parsed.blocks:
                raise ValueError("已无可重新提取的文本，请重新上传简历")
            self._update(sid, import_id, status="queued", draft=ResumeDraft(), original_draft=None,
                         completed_components=[], attempt=job.attempt + 1, error=None)
            self.pool.submit(self._run, sid, import_id, "retry")
        return self.get(sid, import_id)

    def confirm(self, sid, import_id, payload):
        with self.conversations.session_lock(sid), self.store.lock:
            record = self._conversation(sid)
            jobs = self.store.read()
            job = self._job(jobs, sid, import_id)
            if job.status == "confirmed":
                return {"job": self._public(job, record),
                        "snapshot": SessionService.response(record, "此简历已经确认，不会重复应用"),
                        "planning_action": None}
            if job.status != "review" or payload.get("revision") != job.revision:
                raise RevisionConflict("草稿已变更，请保存并核对最新版")
            if job.base_profile_revision != record["revision"]:
                raise RevisionConflict("画像已更新，请重新查看差异并保存草稿")
            job.status = "confirming"
            job.confirmation_request = {"generate_plan": payload.get("generate_plan") is True}
            self.store.write(jobs)
            try:
                result = SessionService(self.conversations).confirm_resume(
                    sid, import_id, job.filename, job.draft, job.original_draft,
                    job.base_profile_revision, job.confirmation_request["generate_plan"])
            except Exception:
                job.status = "review"
                self.store.write(jobs)
                raise
            job.status, job.parsed = "confirmed", None
            job.confirmed_revision = result["snapshot"]["state_revision"]
            job.revision += 1
            self._drop_file(job)
            self.store.write(jobs)
            result["job"] = self._public(job, self._conversation(sid))
            return result

    def delete(self, sid, import_id):
        with self.store.lock:
            jobs = self.store.read()
            job = self._job(jobs, sid, import_id)
            job.status, job.parsed, job.original_draft, job.confirmation_request = "cancelled", None, None, None
            job.draft = ResumeDraft()
            job.revision += 1
            self._drop_file(job)
            self.store.write(jobs)
        return {"deleted": True, "notice": "导入记录已删除；已确认画像可通过编辑资料修改"}

    def delete_session(self, sid):
        with self.store.lock:
            ids = [j.import_id for j in self.store.read().values() if j.session_id == sid]
        for import_id in ids:
            self.delete(sid, import_id)
