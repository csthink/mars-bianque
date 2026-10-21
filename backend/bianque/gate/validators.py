"""The seven validators of the evidence gate.

Each validator except ``schema`` is a pure function of the output, as the JSON object of
its parsed schema model, and the gate context, and returns the rules the output breaks.
None of them calls a model.
"""

from __future__ import annotations

import json
import re
import typing
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from bianque.gate.context import GateContext
from bianque.gate.forbidden import find_write_commands
from bianque.gate.result import Violation
from bianque.gate.schemas import SCHEMAS, OutputType
from bianque.gate.text import iter_strings, join, top_key

#: A validator: the output type, the output as JSON and the context, to violations.
Validator = Callable[[OutputType, Mapping[str, Any], GateContext], list[Violation]]

#: Anything that looks like a knowledge ID, also when it touches non-ASCII text
#: (``手册kn_…``): ``\b`` would see no boundary between a CJK character and ``k``.
KNOWLEDGE_ID_RE = re.compile(r"(?<![0-9A-Za-z_])kn_[0-9A-Za-z]+")


def _field_names(model: type[BaseModel], seen: set[str]) -> set[str]:
    if model.__name__ in seen:
        return set()
    seen.add(model.__name__)
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for nested in _models_in(field.annotation):
            names |= _field_names(nested, seen)
    return names


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


#: Every field name of every schema. A schema violation path keeps only these: a key the
#: model invented may hold any text, including a secret, and the path is stored and sent
#: back to the model.
_SCHEMA_FIELDS = frozenset(
    name for model in SCHEMAS.values() for name in _field_names(model, set())
)


def check_schema(
    output_type: OutputType, output: Mapping[str, Any] | BaseModel
) -> tuple[BaseModel | None, list[Violation]]:
    """Parse an output with the schema of its type.

    The ``schema`` validator differs from the others: it runs first and produces the
    parsed object the others check. The output is parsed as JSON in strict mode, the way
    the exported JSON Schema reads it: ``"yes"`` is not a boolean and a number is not a
    timestamp. Violations carry the pydantic error type and a path of schema field names
    only, never the input value or a key the model invented.

    Returns:
        The parsed output, or ``None`` and the violations.
    """
    data = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    try:
        text = json.dumps(data)
    except (TypeError, ValueError):
        return None, [Violation("schema", "(root)", "not_json")]
    try:
        return SCHEMAS[output_type].model_validate_json(text, strict=True), []
    except ValidationError as error:
        violations = [
            Violation("schema", _loc_path(item["loc"]) or "(root)", item["type"])
            for item in error.errors(include_url=False, include_input=False)
        ]
        return None, violations


def _loc_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        path = join(path, part if isinstance(part, int) or part in _SCHEMA_FIELDS else "*")
    return path


# -- citation_integrity -----------------------------------------------------------


def _cited_items(output_type: OutputType, data: Mapping[str, Any]) -> Iterator[tuple[str, bool]]:
    """Yield ``(path, needs_citation)`` for every item of an output that has citations."""
    if output_type is OutputType.REPORT:
        for index, _ in enumerate(data["timeline"]):
            yield join("timeline", index), True
        yield "root_cause", data["root_cause"]["status"] == "identified"
        for index, _ in enumerate(data["claims"]):
            yield join("claims", index), True
        for index, _ in enumerate(data["suggestions"]):
            yield join("suggestions", index), True
    elif output_type is OutputType.FOLLOWUP_ANSWER:
        for index, segment in enumerate(data["segments"]):
            yield join("segments", index), not segment["insufficient_evidence"]
    elif output_type is OutputType.FINDINGS:
        for index, _ in enumerate(data["findings"]):
            yield join("findings", index), True


def _get(data: Mapping[str, Any], path: str) -> Any:
    value: Any = data
    for part in re.findall(r"[^.\[\]]+", path):
        value = value[int(part)] if part.isdigit() else value[part]
    return value


def citation_integrity(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """Every cited ID belongs to the investigation, and every claim-like item cites one.

    A follow-up answer may cite suggestion IDs as well as evidence IDs.
    """
    known = context.evidence_ids
    if output_type is OutputType.FOLLOWUP_ANSWER:
        known = known | context.suggestion_ids
    violations = []
    for path, needs_citation in _cited_items(output_type, data):
        citations = _get(data, path)["citations"]
        if needs_citation and not citations:
            violations.append(Violation("citation_integrity", path, "missing_citation"))
        for index, cited in enumerate(citations):
            if cited not in known:
                violations.append(
                    Violation(
                        "citation_integrity", join(join(path, "citations"), index), "unknown_id"
                    )
                )
    return violations


# -- knowledge_boundary -----------------------------------------------------------

#: The field of each output type where knowledge IDs belong.
_KNOWLEDGE_FIELD = {
    OutputType.REPORT: "references",
    OutputType.FOLLOWUP_ANSWER: "references",
    OutputType.PLAN: "knowledge_refs",
}


def _referenced_knowledge(
    output_type: OutputType, data: Mapping[str, Any]
) -> Iterator[tuple[str, str]]:
    field = _KNOWLEDGE_FIELD.get(output_type)
    if field is None:
        return
    for index, item in enumerate(data[field]):
        if output_type is OutputType.REPORT:
            yield join(join(field, index), "knowledge_id"), item["knowledge_id"]
        else:
            yield join(field, index), item


#: A citation list element: ``...citations[<n>]``.
_CITATION_PATH = re.compile(r"(?:^|\.)citations\[\d+\]$")


def _names_skill(value: str, names: frozenset[str]) -> bool:
    """Whether ``value`` holds a skill name as a whole word (not inside a longer name)."""
    return any(
        re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", value, re.IGNORECASE)
        for name in names
    )


def knowledge_boundary(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """Knowledge IDs appear only where references belong, and only known ones.

    Knowledge is not evidence: a ``kn_`` ID in a citation, a statement or any other field
    is a violation, and so is a referenced knowledge ID that the model was not given.

    A skill is neither evidence nor knowledge: its name in a citation, in the references
    or in a plan's ``knowledge_refs`` is a violation (``skill_cited``). Prose may name a
    skill, for example a plan's rationale saying which skill it follows.
    """
    field = _KNOWLEDGE_FIELD.get(output_type)
    violations = [
        Violation("knowledge_boundary", path, "knowledge_outside_references")
        for path, value in iter_strings(data)
        if top_key(path) != field and KNOWLEDGE_ID_RE.search(value)
    ]
    violations += [
        Violation("knowledge_boundary", path, "unknown_knowledge_id")
        for path, knowledge_id in _referenced_knowledge(output_type, data)
        if knowledge_id not in context.knowledge_ids
    ]
    if context.skill_names:
        violations += [
            Violation("knowledge_boundary", path, "skill_cited")
            for path, value in iter_strings(data)
            if (top_key(path) == field or _CITATION_PATH.search(path))
            and _names_skill(value, context.skill_names)
        ]
    return violations


# -- unknown_discipline and missing_evidence_consistency (reports only) -------------


def unknown_discipline(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """A root cause without citations is ``unknown``, and an unknown one has low confidence."""
    root_cause = data["root_cause"]
    violations = []
    if not root_cause["citations"] and root_cause["status"] != "unknown":
        violations.append(
            Violation("unknown_discipline", "root_cause.status", "uncited_root_cause_not_unknown")
        )
    if root_cause["status"] == "unknown" and root_cause["confidence"] != "low":
        violations.append(
            Violation("unknown_discipline", "root_cause.confidence", "unknown_confidence_not_low")
        )
    return violations


def missing_evidence_consistency(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """Failed evidence calls are listed as missing evidence and limit the confidence."""
    violations = []
    if context.failed_calls and not data["missing_evidence"]:
        violations.append(
            Violation("missing_evidence_consistency", "missing_evidence", "failed_calls_not_listed")
        )
    capped = any(call.caps_confidence for call in context.failed_calls)
    if capped and data["root_cause"]["confidence"] == "high":
        violations.append(
            Violation(
                "missing_evidence_consistency",
                "root_cause.confidence",
                "high_confidence_after_failed_call",
            )
        )
    return violations


# -- forbidden_actions and redaction_residue (all output types) ---------------------

_CHANGE_SUGGESTION_TEXT = re.compile(r"suggestions\[(\d+)\]\.(action|command)")


def _write_commands_allowed(output_type: OutputType, data: Mapping[str, Any], path: str) -> bool:
    if output_type is not OutputType.REPORT:
        return False
    match = _CHANGE_SUGGESTION_TEXT.fullmatch(path)
    if match is None:
        return False
    return bool(data["suggestions"][int(match.group(1))]["risk"] == "change")


def forbidden_actions(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """Write commands appear only in report suggestions whose risk is ``change``."""
    return [
        Violation("forbidden_actions", path, f"write_command:{command}")
        for path, value in iter_strings(data)
        if not _write_commands_allowed(output_type, data, path)
        for command in find_write_commands(value)
    ]


def redaction_residue(
    output_type: OutputType, data: Mapping[str, Any], context: GateContext
) -> list[Violation]:
    """No text field holds sensitive content; the investigation's identifiers are exempt."""
    return [
        Violation("redaction_residue", path, f"residue:{finding.rule}")
        for path, value in iter_strings(data)
        for finding in context.engine.detect(value, preserve=context.preserved)
    ]
