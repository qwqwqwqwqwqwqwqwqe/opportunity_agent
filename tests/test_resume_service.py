import json
import threading
import time
from pathlib import Path

import pytest
pytest.importorskip("docx", reason="install the resume extra for file/service tests")

from opportunity_agent.conversation_store import ConversationStore, DeletedConversation, RevisionConflict
from opportunity_agent.resume_models import ParsedResumeDocument, ResumeBlock, ResumeDraft, ResumeExperience, ResumeFact, utcnow
from opportunity_agent.resume_service import ResumeImportService
from opportunity_agent.session_service import SessionService
from resume_helpers import document, docx_bytes, wait_job


def complete_draft():
    evidence = "synthetic"
    values = {
        "school":"Synthetic University", "major":"Computer Science", "academic_year":2,
        "degree_years":4, "graduation_year":2029, "target_countries":["US"],
        "target_degree":"MS", "target_fields":["Artificial Intelligence"], "gpa_raw":3.9,
        "gpa_scale":4.0, "toefl_score":105,
    }
    facts = [ResumeFact(field=field, normalized_value=value, raw_value=value, confidence=.95,
                         evidence=evidence, block_ids=["p1"]) for field,value in values.items()]
    experience = ResumeExperience(kind="research", name="Signal classification", period="2026",
         role="复现", methods="Python, PyTorch", outcomes="精度 90%，延迟下降 10%",
         evidence=evidence, block_ids=["p1"])
    return ResumeDraft(facts=facts, experiences=[experience])


class CompleteExtractor:
    mode, error = "llm", None
    def generate(self, parsed): return complete_draft()


class RuleExtractor:
    mode, error = "rule", None
    def generate(self, parsed):
        return ResumeDraft(facts=[ResumeFact(field="toefl_score", value=105, confidence=.98,
            evidence="TOEFL: 105", block_ids=["p1"])])


@pytest.fixture
def setup(tmp_path):
    store = ConversationStore(tmp_path/"conversations.json")
    SessionService(store).import_conversation({"session_id":"s1","conversation_title":"Synthetic"})
    services = []
    def make(**kw):
        service=ResumeImportService(store, **kw);services.append(service);return service
    yield store,make
    for service in services:
        service.close()


def test_upload_is_async_private_and_unconfirmed_profile_unchanged(setup):
    store,make=setup
    service=make(parser=lambda path: document(), extractor_factory=CompleteExtractor)
    result=service.upload("s1","r1","resume.docx",docx_bytes())
    assert result["status"]=="queued" and "temp_path" not in result and "file_hash" not in result
    job=wait_job(service,"s1",result["import_id"])
    record=store.get("s1")
    assert record["state"]["profile"]["school"] is None
    assert job["draft"]["facts"] and job["parsed"]
    assert not any((service.temp_dir).glob("*"))
    raw=(tmp_path_from_store(store)/"resume_imports.json").read_text("utf-8")
    assert "synthetic@example.com" not in raw
    assert "MINERU_API_TOKEN" not in raw and "temp_path" in raw  # key is persisted, value is null
    assert job["current_profile"]["school"] is None


def test_reextract_rebuilds_an_unconfirmed_draft_from_retained_text(setup):
    _, make = setup
    calls = []

    class CountingExtractor:
        mode, error = "rule", None

        def generate(self, parsed):
            calls.append(parsed.text)
            return complete_draft()

    service = make(parser=lambda path: document("TOEFL: 105"), extractor_factory=CountingExtractor)
    queued = service.upload("s1", "reextract-1", "resume.docx", docx_bytes())
    job = wait_job(service, "s1", queued["import_id"])
    assert job["status"] == "review" and len(calls) == 1

    restarted = service.reextract("s1", job["import_id"])
    assert restarted["status"] in {"queued", "extracting"}
    rebuilt = wait_job(service, "s1", job["import_id"])
    assert rebuilt["status"] == "review" and len(calls) == 2
    assert rebuilt["parsed"]["blocks"]  # the original file is not needed again


def tmp_path_from_store(store): return store.path.parent


def test_edit_confirm_classified_experience_and_no_comma_fragmentation(setup):
    store,make=setup
    service=make(parser=lambda path: document(), extractor_factory=CompleteExtractor)
    queued=service.upload("s1","r2","resume.docx",docx_bytes())
    job=wait_job(service,"s1",queued["import_id"])
    draft=ResumeDraft.model_validate(job["draft"])
    item=draft.experiences[0]
    item.outcomes="准确率 92%，延迟下降 12%，保留逗号"
    job=service.save_draft("s1",job["import_id"],{"revision":job["revision"],
        "profile_revision":job["current_profile_revision"],"draft":draft.model_dump(mode="json")})
    result=service.confirm("s1",job["import_id"],{"revision":job["revision"],"generate_plan":False})
    profile=result["snapshot"]["profile"]
    assert profile["school"]=="Synthetic University" and profile["gpa"]==3.9
    assert len(profile["research_experiences"])==1
    assert "92%" in profile["research_experiences"][0] and "保留逗号" in profile["research_experiences"][0]
    assert not profile["project_experiences"]
    original_fact=next(f for f in profile["facts"] if f["field"]=="research_experiences")
    assert "90%" in original_fact["raw_value"][0] and "92%" in original_fact["normalized_value"][0]
    assert "简历 " in original_fact["evidence"] and "原始抽取置信度" in original_fact["evidence"]
    assert result["planning_action"] is None
    assert result["snapshot"]["roadmap"] is None  # save-only does not silently create a plan
    repeat=service.confirm("s1",job["import_id"],{"revision":job["revision"],"generate_plan":False})
    assert len(repeat["snapshot"]["profile"]["research_experiences"])==1


def test_confirm_and_generate_returns_explicit_plan_action(setup):
    _,make=setup
    service=make(parser=lambda path: document(), extractor_factory=CompleteExtractor)
    queued=service.upload("s1","r3","resume.pdf",b"%PDF-synthetic")
    job=wait_job(service,"s1",queued["import_id"])
    result=service.confirm("s1",job["import_id"],{"revision":job["revision"],"generate_plan":True})
    assert result["snapshot"]["roadmap"]["supported"]
    assert result["planning_action"]=="enrich" and not result["needs_profile"]


def test_existing_profile_conflict_is_deselected_and_not_overwritten(setup):
    store,make=setup
    SessionService(store).execute("/api/chat",{"session_id":"s1","request_id":"score","message":"托福110"})
    service=make(parser=lambda path: document(), extractor_factory=RuleExtractor)
    queued=service.upload("s1","r4","resume.pdf",b"%PDF-synthetic")
    job=wait_job(service,"s1",queued["import_id"])
    fact=job["draft"]["facts"][0]
    assert fact["normalized_value"]==105 and not fact["selected"]
    result=service.confirm("s1",job["import_id"],{"revision":job["revision"],"generate_plan":False})
    assert result["snapshot"]["profile"]["toefl_score"]==110


def test_resume_courses_are_merged_with_existing_confirmed_courses(setup):
    store, make = setup
    SessionService(store).execute("/api/onboarding", {"session_id": "s1", "request_id": "course-form", "profile": {
        "school": "Synthetic University", "major": "Computer Science", "academic_year": 2,
        "graduation_year": 2029, "target_countries": ["US"], "target_degree": "MS",
        "target_fields": ["Artificial Intelligence"], "completed_courses": ["数据结构", "线性代数"],
    }})

    class CourseExtractor:
        mode, error = "rule", None

        def generate(self, parsed):
            return ResumeDraft(facts=[ResumeFact(
                field="completed_courses", raw_value="Relevant Coursework: Data Structures; Algorithms",
                normalized_value=["Data Structures", "Algorithms"], confidence=.9,
                evidence="Relevant Coursework: Data Structures; Algorithms", block_ids=["p1"],
            )])

    service = make(parser=lambda path: document(), extractor_factory=CourseExtractor)
    queued = service.upload("s1", "courses", "resume.docx", docx_bytes())
    job = wait_job(service, "s1", queued["import_id"])
    fact = job["draft"]["facts"][0]
    assert fact["selected"]
    assert fact["normalized_value"] == ["数据结构", "线性代数", "Data Structures", "Algorithms"]

    confirmed = service.confirm("s1", job["import_id"], {"revision": job["revision"], "generate_plan": False})
    assert confirmed["snapshot"]["profile"]["completed_courses"] == fact["normalized_value"]


def test_cross_browser_profile_revision_must_be_reviewed(setup):
    store,make=setup
    service=make(parser=lambda path: document(), extractor_factory=CompleteExtractor)
    queued=service.upload("s1","r5","resume.docx",docx_bytes())
    stale=wait_job(service,"s1",queued["import_id"])
    SessionService(store).execute("/api/chat",{"session_id":"s1","request_id":"new","message":"托福110"})
    with pytest.raises(RevisionConflict,match="另一窗口"):
        service.save_draft("s1",stale["import_id"],{"revision":stale["revision"],
            "profile_revision":stale["current_profile_revision"],"draft":stale["draft"]})
    newest=service.get("s1",stale["import_id"])
    assert newest["current_profile"]["toefl_score"]==110
    with pytest.raises(RevisionConflict,match="画像已更新"):
        service.confirm("s1",stale["import_id"],{"revision":stale["revision"],"generate_plan":False})


def test_idempotent_request_and_one_active_import_per_session(setup):
    _,make=setup
    release=threading.Event()
    def slow(path):
        release.wait(2);return document()
    service=make(parser=slow, extractor_factory=CompleteExtractor)
    data=docx_bytes()
    first=service.upload("s1","same","r.docx",data)
    second=service.upload("s1","same","r.docx",data)
    assert first["import_id"]==second["import_id"]
    with pytest.raises(RevisionConflict,match="当前"):
        service.upload("s1","other","r.docx",data)
    with pytest.raises(RevisionConflict,match="请求 ID"):
        service.upload("s1","same","r.pdf",b"%PDF-different")
    release.set()
    wait_job(service,"s1",first["import_id"])


def test_delete_conversation_cancels_task_and_late_result_cannot_resurrect(setup):
    store,make=setup
    entered,release=threading.Event(),threading.Event()
    def slow(path):
        entered.set();release.wait(3);return document()
    service=make(parser=slow, extractor_factory=CompleteExtractor)
    job=service.upload("s1","late","r.docx",docx_bytes())
    assert entered.wait(2)
    store.delete("s1");service.delete_session("s1");release.set()
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        with service.store.lock:
            saved=service.store.read()[job["import_id"]]
        if saved.status=="cancelled": break
        time.sleep(.02)
    assert saved.status=="cancelled" and saved.draft.facts==[]
    assert store.is_deleted("s1") and store.get("s1") is None
    assert not list(service.temp_dir.glob("*"))
    with pytest.raises(DeletedConversation):
        service.get("s1",job["import_id"])


def test_consent_required_no_cloud_without_accept_and_expiry_cleanup(setup,monkeypatch):
    _,make=setup
    parsed=ParsedResumeDocument(needs_cloud=True,blocks=[])
    service=make(parser=lambda path: parsed, extractor_factory=CompleteExtractor)
    job=service.upload("s1","scan","scan.pdf",b"%PDF-scan")
    job=wait_job(service,"s1",job["import_id"],"awaiting_consent")
    assert list(service.temp_dir.glob("*"))
    with pytest.raises(ValueError,match="MINERU"):
        service.consent("s1",job["import_id"],True)
    service.consent("s1",job["import_id"],False) if job["parsed"]["blocks"] else None
    with service.store.lock:
        jobs=service.store.read()
        jobs[job["import_id"]].expires_at=utcnow()
        service.store.write(jobs)
    service.sweep()
    with service.store.lock:
        saved=service.store.read()[job["import_id"]]
    assert saved.status=="cancelled" and saved.parsed is None
    assert not list(service.temp_dir.glob("*"))


def test_restart_marks_incomplete_job_interrupted(tmp_path):
    store=ConversationStore(tmp_path/"conversations.json")
    SessionService(store).import_conversation({"session_id":"s","conversation_title":"Synthetic"})
    service=ResumeImportService(store,parser=lambda path:document(),extractor_factory=CompleteExtractor)
    job=service.upload("s","x","r.docx",docx_bytes())
    # Persist an in-flight shape, then close its completed worker and restore it.
    wait_job(service,"s",job["import_id"])
    with service.store.lock:
        jobs=service.store.read();saved=jobs[job["import_id"]]
        saved.status="extracting";saved.parsed=document();jobs[saved.import_id]=saved;service.store.write(jobs)
    service.close()
    restarted=ResumeImportService(store,extractor_factory=CompleteExtractor)
    try:
        status=restarted.get("s",job["import_id"])
        assert status["status"]=="interrupted" and status["parsed"]
        restarted.retry("s",job["import_id"])
        assert wait_job(restarted,"s",job["import_id"])["status"]=="review"
    finally: restarted.close()


def test_onboarding_experiences_only_split_lines():
    from opportunity_agent.models import OnboardingProfileInput
    value=OnboardingProfileInput.model_validate({"school":"S","major":"CS","target_countries":["US"],
      "target_degree":"MS","target_fields":["AI"],"project_experiences":"项目A，含逗号；成果\n项目B, another comma"})
    assert value.project_experiences==["项目A，含逗号；成果","项目B, another comma"]


def test_failed_upload_journal_does_not_leave_original(setup,monkeypatch):
    _,make=setup
    service=make()
    with monkeypatch.context() as context:
        def fail(jobs): raise OSError("synthetic disk error")
        context.setattr(service.store,"write",fail)
        with pytest.raises(OSError):
            service.upload("s1","disk-fail","r.docx",docx_bytes())
    assert not list(service.temp_dir.glob("*"))


def test_real_isolated_parser_process(setup, monkeypatch):
    monkeypatch.setenv("RESUME_PARSE_TIMEOUT_SECONDS", "10")
    _,make=setup
    service=make(extractor_factory=CompleteExtractor)
    job=service.upload("s1","real","real.docx",docx_bytes())
    result=wait_job(service,"s1",job["import_id"])
    assert result["parsed"]["blocks"]


def test_resume_save_preserves_existing_article_and_task_progress(setup):
    from opportunity_agent.progress import targets
    from opportunity_agent.session_state import restore_agent
    store,make=setup
    sessions=SessionService(store)
    sessions.execute("/api/onboarding", {"session_id":"s1","request_id":"form","profile":{
        "school":"Synthetic University","major":"Computer Science","academic_year":2,"graduation_year":2029,
        "target_countries":["US"],"target_degree":"MS","target_fields":["Artificial Intelligence"]}})
    agent=restore_agent("s1",store.get("s1")["state"])
    key=next(t for t in targets(agent.roadmap) if "background_portfolio" in t["aliases"])["target_id"]
    sessions.execute("/api/progress",{"session_id":"s1","request_id":"done","target_id":key,"action":"complete"})
    article=store.get("s1")["state"]["roadmap"]["article"]
    service=make(parser=lambda path:document(),extractor_factory=CompleteExtractor)
    queued=service.upload("s1","after","r.docx",docx_bytes())
    job=wait_job(service,"s1",queued["import_id"])
    result=service.confirm("s1",job["import_id"],{"revision":job["revision"],"generate_plan":False})
    assert result["snapshot"]["roadmap"]["article"]==article
    assert result["snapshot"]["task_progress"][0]["status"]=="completed"
    assert len(result["snapshot"]["task_progress"])==1
    assert result["planning_action"] is None
