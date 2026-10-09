"""Consolidate repeated frozen pages without changing evidence identities."""
from __future__ import annotations

import copy


def page_types(record: dict) -> list[str]:
    return list(dict.fromkeys(filter(None, [record.get("page_type"), *record.get("page_types", [])])))


def consolidate_sources(sources: list[dict]) -> list[dict]:
    """One source per ID; search intents are roles of a page, not new snapshots."""
    output, by_id = [], {}
    for source in sources:
        identifier = source.get("id")
        if not identifier:
            output.append(copy.deepcopy(source))
            continue
        if identifier not in by_id:
            item = copy.deepcopy(source)
            by_id[identifier] = item
            output.append(item)
            continue
        previous = by_id[identifier]
        for field in ("program_id", "url", "content_hash"):
            if previous.get(field) != source.get(field):
                raise ValueError(f"Conflicting source snapshots for {identifier}: {field}")
        previous["page_types"] = list(dict.fromkeys([*page_types(previous), *page_types(source)]))
        for field in ("facts", "policy_fields", "classifier_evidence"):
            if field in previous or field in source:
                values = previous.setdefault(field, [])
                for value in source.get(field, []):
                    if value not in values:
                        values.append(copy.deepcopy(value))
    return output


def deduplicate_documents(documents: list[dict]) -> list[dict]:
    """Collapse identical IDs; reject different evidence masquerading as one chunk."""
    output, by_id = [], {}

    def identity(item):
        metadata = {key: value for key, value in item.get("metadata", {}).items()
                    if key not in {"page_type", "page_types"}}
        return {**item, "metadata": metadata}

    for document in documents:
        identifier = document["id"]
        if identifier not in by_id:
            item = copy.deepcopy(document)
            by_id[identifier] = item
            output.append(item)
            continue
        previous = by_id[identifier]
        if identity(previous) != identity(document):
            raise ValueError(f"Conflicting documents for chunk ID {identifier}; cannot silently deduplicate")
        previous.setdefault("metadata", {})["page_types"] = list(dict.fromkeys([
            *page_types(previous.get("metadata", {})), *page_types(document.get("metadata", {}))]))
    return output


def repair_corpus_duplicates(data: dict) -> dict:
    """Return a repaired copy retaining queries, source IDs, chunk IDs and labels."""
    return {**data, "sources": consolidate_sources(data.get("sources", [])),
            "documents": deduplicate_documents(data.get("documents", []))}
