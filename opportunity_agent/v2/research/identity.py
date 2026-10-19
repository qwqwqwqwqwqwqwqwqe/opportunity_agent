"""Normalize registered university aliases without conflating distinct programmes."""
from functools import lru_cache
import re
import unicodedata

from ...official_research import OfficialDomainRegistry


# Degree-level synonyms only. Department overlap does not make MSCS, MCS,
# CSE or ECE interchangeable, unlike broad webpage relevance matching.
_PROGRAM_ALIASES = {
    "MSCS": ("MSCS", "Master of Science in Computer Science", "MS in Computer Science", "M.S. in Computer Science",
             "MSc CS", "MSc in Computer Science", "MS CS"),
    "MCS": ("MCS", "Master of Computer Science"),
    "CSE": ("CSE", "MS CSE", "Master of Science in Computer Science and Engineering",
            "Master of Science in Computer Science & Engineering"),
    "MSML": ("MSML", "Master of Science in Machine Learning"),
    "MSAII": ("MSAII", "Master of Science in Artificial Intelligence and Innovation"),
}


def program_aliases(name):
    if name.strip().casefold() in {"cse", "ms cse"}:
        # CSE can mean Computational Science and Engineering, among others.
        # Preserve the query; official pages establish its school-local meaning.
        return (name.strip(),)
    for aliases in _PROGRAM_ALIASES.values():
        if name.strip().casefold() in {a.casefold() for a in aliases}:
            return aliases
    return (name,)


def canonical_program(name):
    return program_aliases(name)[0].strip().casefold()


def identity_text(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()


def program_subject(value):
    """Degree/name formatting only. Never expand an ambiguous programme acronym."""
    value = identity_text(value)
    value = re.sub(r'\b(?:masters? of (?:science|arts)|bachelors? of (?:science|arts)|doctor of philosophy)\b', ' ', value)
    value = re.sub(r'\b(?:masters?|bachelors?|doctoral|doctorate|ph d|phd|dphil|m sc|msc|m s|ms|m eng|meng|b sc|bsc|b s|bs)\b', ' ', value)
    value = re.sub(r'\b(?:programmes?|programs?|degrees?)\s*$', '', value)
    return ' '.join(w for w in value.split() if w not in {'in', 'of', 'the', 'and'})


def scope_key(program):
    return (canonical_school(program.university),
            program.program_subject or program_subject(program.program),
            program.degree + (':' + program.degree_award if program.degree_award else ''), identity_text(program.home_unit),
            normalize_intake(program.intake).casefold())


def degree_award(value):
    """Only explicitly written credential formatting; no acronym expansion."""
    value = identity_text(value)
    if re.search(r'\b(?:master of science|masters of science|m sc|msc|m s|ms)\b', value):
        return 'science'
    if re.search(r'\b(?:master of arts|masters of arts|m a|ma)\b', value):
        return 'arts'
    if re.search(r'\b(?:master of engineering|masters of engineering|m eng|meng)\b', value):
        return 'engineering'
    if re.search(r'\bmasters? of\b', value):
        return 'professional'
    return ''


def is_excluded(program, exclusions):
    # Legacy three-part exclusions only exclude legacy, unscoped results.
    # They cannot silently exclude every degree/home unit of a new scoped item.
    return any(tuple(item) == program.scoped_identity if len(item) == 5 else
               tuple(item) == program.identity and not (program.degree or program.home_unit)
               for item in exclusions)


def requested_program(program, name):
    if not name:
        return True
    if program_subject(name) == (program.program_subject or program_subject(program.program)):
        return True
    from .extraction import same_program
    from .extraction import VERSION
    for evidence in program.evidence:
        proof = evidence.extraction_provenance
        identity = proof.get('identity', {})
        if (proof.get('version') == VERSION and identity.get('subject') == program.scoped_identity[1]
                and identity_text(name) in {identity_text(a) for a in identity.get('aliases',[])}):
            return True
    evidence_scope = '\n'.join(e.title + '\n' + e.excerpt for e in program.evidence)
    if same_program(name, program.program, evidence_scope):
        return True
    return not (program.degree or program.home_unit) and canonical_program(name) == canonical_program(program.program)


def excluded_scope(exclusions, university, program='', degree='', home_unit='', intake=''):
    for item in exclusions:
        if canonical_school(item[0]) != canonical_school(university):
            continue
        if intake:
            from .task import intake_matches
            if not intake_matches(intake, item[-1]):
                continue
        if len(item) == 3:
            if degree or home_unit:
                continue
            if program and canonical_program(item[1]) != canonical_program(program):
                continue
        else:
            if degree and item[2].split(':',1)[0] != degree or home_unit and item[3] != identity_text(home_unit):
                continue
            if program and item[1] not in {program_subject(v) for v in program_aliases(program)}:
                continue
        return True
    return False


@lru_cache(maxsize=1024)
def school_aliases(name):
    registry = OfficialDomainRegistry()
    record = registry.resolve(name)
    if not record:
        return (name,)
    for item in registry.items:
        if item.get("name") == record["university"]:
            return tuple(dict.fromkeys([item["name"], *item.get("aliases", [])]))
    return (name,)


def canonical_school(name):
    return school_aliases(name)[0].strip().casefold()


def normalize_intake(value):
    """Canonicalize only explicit year/season information; never infer Fall."""
    year = re.search(r"20\d{2}", value)
    if not year:
        return value.strip()
    for season, pattern in (("Fall", r"\bfall\b|\bautumn\b|秋季"),
                            ("Spring", r"\bspring\b|春季"), ("Summer", r"\bsummer\b|夏季")):
        if re.search(pattern, value, re.I):
            return year[0] + " " + season
    return year[0]


def intake_aliases(value):
    value = normalize_intake(value)
    parts = value.split()
    if len(parts) == 2:
        aliases = [value, parts[1] + " " + parts[0]]
        if parts[1] == "Fall":
            aliases += [parts[0] + " Autumn", "Autumn " + parts[0]]
        return aliases
    return [value]
