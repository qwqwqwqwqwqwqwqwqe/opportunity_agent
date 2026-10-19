from datetime import date as Date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, model_validator, computed_field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Track(Strict):
    name: str
    description: str = ''
    requirements: str = ''


class Course(Strict):
    name: str
    group: str = ''
    credits: str = ''


class CourseRule(Strict):
    group: str = ''
    requirement: str
    course_codes: list[str] = Field(default_factory=list)
    choose: int | None = Field(default=None, ge=1)
    credits: str = ''


class Deadline(Strict):
    label: str = '申请截止'
    raw_date: str = ''
    date: Date | None = None
    time: str = ''
    timezone: str = ''
    applicants: str = ''

    @model_validator(mode='after')
    def valid_time(self):
        if self.time:
            datetime.strptime(self.time, '%H:%M')
            if not self.date or not self.timezone:
                raise ValueError('具体时间需要明确日期和时区')
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError:
                raise ValueError('无效 IANA 时区')
        return self


class Language(Strict):
    exam: str
    version: str = ''
    total: str = ''
    sections: str = ''
    conditions: str = ''


class Material(Strict):
    name: str
    category: str = 'other'
    requirement: str = ''
    prompt: str = ''
    limit: int | None = Field(default=None, ge=0)
    unit: Literal['words', 'characters', 'pages', 'none'] = 'none'
    count: int | None = Field(default=None, ge=0, le=20)


class ProjectData(Strict):
    university: str = Field(default='', max_length=200)
    program: str = Field(default='', max_length=200)
    intake: str = Field(default='', max_length=80)
    aliases: list[str] = Field(default_factory=list)
    degree: str = ''
    degree_award: str = ''
    country: str = ''
    campus: str = ''
    home_unit: str = ''
    summary: str = ''
    objectives: str = ''
    background: str = ''
    study_type: str = ''
    duration: str = ''
    credits: str = ''
    tracks: list[Track] = Field(default_factory=list)
    tracks_complete: bool = False
    courses: list[Course] = Field(default_factory=list)
    course_rules: list[CourseRule] = Field(default_factory=list)
    courses_complete: bool = False
    graduation: str = ''
    language: list[Language] = Field(default_factory=list)
    gre: Literal['unknown', 'required', 'optional', 'not_required', 'not_accepted'] = 'unknown'
    prerequisites: str = ''
    gpa: str = ''
    materials: list[Material] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    fees: str = ''
    currency: str = ''
    warnings: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    observed_identity: str = ''


class SourceInput(Strict):
    kind: Literal['overview', 'curriculum', 'requirements'] = 'overview'
    url: str = Field(default='', max_length=2048)
    text: str = Field(default='', max_length=200000)


class ProjectInput(Strict):
    data: ProjectData
    sources: list[SourceInput] = Field(default_factory=list, max_length=5)


class Command(Strict):
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(default=1, ge=1)


class CreateProject(Command, ProjectInput):
    pass


class ImportBatch(Command):
    projects: list[ProjectInput] = Field(min_length=1, max_length=10)


class EditProject(Command):
    data: ProjectData


class SourceEdit(Command):
    source: SourceInput


Critical = Literal['deadlines', 'timezone', 'language', 'gre', 'materials']


class ConfirmProject(Command):
    confirmations: list[Critical] = Field(default_factory=list)
    approved_sources: list[str] = Field(default_factory=list)


class Progress(Command):
    status: Literal['planned', 'in_progress', 'completed', 'submitted', 'not_applicable']
    evidence: str = Field(default='', max_length=2000)
    invited: int | None = Field(default=None, ge=0, le=20)
    received: int | None = Field(default=None, ge=0, le=20)


class EventData(Strict):
    title: str = Field(min_length=1, max_length=300)
    kind: Literal['exam', 'visa', 'deadline', 'task', 'custom'] = 'custom'
    date: Date
    time: str = ''
    timezone: str = 'Asia/Shanghai'
    notes: str = Field(default='', max_length=4000)
    reminder_at: datetime | None = None
    email: bool = False

    @model_validator(mode='after')
    def check_time(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError('无效 IANA 时区')
        if self.time:
            datetime.strptime(self.time, '%H:%M')
            local=datetime.fromisoformat(str(self.date)+'T'+self.time)
            zone=ZoneInfo(self.timezone)
            aware=local.replace(tzinfo=zone)
            if (aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)!=local
                    or aware.utcoffset()!=local.replace(tzinfo=zone,fold=1).utcoffset()):
                raise ValueError('日程时间处于夏令时跳变或重复区间，请换明确时间或UTC')
        if self.reminder_at and self.reminder_at.utcoffset() is None:
            raise ValueError('提醒时间必须带时区')
        return self

    @computed_field
    @property
    def starts_at(self) -> datetime | None:
        if not self.time:return None
        return datetime.fromisoformat(str(self.date)+'T'+self.time).replace(tzinfo=ZoneInfo(self.timezone)).astimezone(timezone.utc)


class EventCommand(Command):
    data: EventData
    reminder_local: datetime | None = None
    task_id: str | None = None


class Experience(Strict):
    id: str = Field(min_length=1, max_length=128)
    category: Literal['research', 'project', 'internship', 'competition', 'paper']
    name: str = Field(min_length=1, max_length=300)
    role: str = ''
    dates: str = ''
    description: str = Field(default='', max_length=20000)
    skills: str = ''
    outcomes: str = ''


class Background(Command):
    # The legacy profile remains readable. Structured experiences live alongside it.
    fields: dict = Field(default_factory=dict)
    experiences: list[Experience] = Field(default_factory=list, max_length=100)


class Query(Strict):
    query: str = Field(default='', max_length=10000)
    project_ids: list[str] = Field(default_factory=list, max_length=100)
    country: str = ''
    degree: str = ''
    limit: int = Field(default=5, ge=1, le=20)


class ChecklistItem(Strict):
    stable_key: str = Field(min_length=1,max_length=160)
    title: str = Field(min_length=1,max_length=300)
    category: str = Field(default='other',max_length=32)
    requirement: str = Field(default='',max_length=10000)
    required_count: int = Field(default=1,ge=1,le=20)
    invited: int = Field(default=0,ge=0,le=20)
    received: int = Field(default=0,ge=0,le=20)
    suggested_date: Date | None = None
    date_note: str = ''


class ChecklistEdit(Command):
    items: list[ChecklistItem] = Field(default_factory=list,max_length=100)
