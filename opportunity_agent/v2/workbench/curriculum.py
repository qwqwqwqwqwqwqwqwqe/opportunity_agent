"""Typed extraction boundary; legacy stored drafts remain readable."""
import re
from pydantic import Field, model_validator
from .schemas import Strict, Course, CourseRule


class AtomicCourse(Course):
    @model_validator(mode='after')
    def atomic(self):
        # Detect clear lists, not natural conjunctions in a single course title.
        codes=re.findall(r'(?<!\d)\d{2,4}[-–]\d{3,4}(?!\d)',self.name)
        if len(codes)>1 or re.search(r'[;；\n]',self.name):
            raise ValueError('COURSE_NOT_ATOMIC: one named course per item; move alternatives/lists to course_rules')
        if re.match(r'(?i)^(?:at least|choose|select|any |a \d+[- ]unit course|至少|任选|选修至少)',self.name.strip()):
            raise ValueError('COURSE_IS_RULE: move selection requirement to course_rules')
        if re.search(r'(?i)at least|choose|equivalent|至少|任选',self.credits):
            raise ValueError('COURSE_GROUP_CREDITS: group credit minimum is a course_rule')
        return self


class Curriculum(Strict):
    courses: list[AtomicCourse] = Field(default_factory=list)
    course_rules: list[CourseRule] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_courses(self):
        keys=[(re.sub(r'\s+',' ',c.name.strip().casefold()),c.group.strip().casefold()) for c in self.courses]
        if len(keys)!=len(set(keys)):
            raise ValueError('DUPLICATE_COURSE: duplicate name within group')
        return self
