"""Progress event payloads hold no field that could carry model text."""

from __future__ import annotations

from typing import Any

import pytest
from investigation_harness import investigate
from pydantic import ValidationError
from sqlalchemy import Engine

from bianque.agents.events import PAYLOADS, event_payload
from bianque.tools import registry


def _string_fields(
    schema: dict[str, Any], defs: dict[str, Any], path: str
) -> list[tuple[str, dict[str, Any]]]:
    """Every string-typed node of a JSON Schema, with its path."""
    if "$ref" in schema:
        return _string_fields(defs[schema["$ref"].rsplit("/", 1)[-1]], defs, path)
    found: list[tuple[str, dict[str, Any]]] = []
    for branch in schema.get("anyOf", []):
        found += _string_fields(branch, defs, path)
    if schema.get("type") == "string":
        found.append((path, schema))
    if schema.get("type") == "array":
        found += _string_fields(schema.get("items", {}), defs, f"{path}[]")
    for name, sub in schema.get("properties", {}).items():
        found += _string_fields(sub, defs, f"{path}.{name}")
    return found


@pytest.mark.parametrize("event_type", sorted(PAYLOADS))
def test_every_string_field_is_an_enumeration_or_an_identifier_pattern(event_type: str) -> None:
    schema = PAYLOADS[event_type].model_json_schema()
    assert schema.get("additionalProperties") is False
    for path, field in _string_fields(schema, schema.get("$defs", {}), event_type):
        assert "enum" in field or "const" in field or "pattern" in field, path
        if "pattern" in field:
            assert field["pattern"].startswith("^") and field["pattern"].endswith("$"), path


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("step_started", {"step_id": 1, "domain": "the pod looks fine", "status": "running"}),
        ("evidence_added", {"evidence_id": "ev_0123456789", "source": "git", "tool": "a b"}),
        ("report_rejected", {"attempt": 1, "violations": ["the report said kubectl delete"]}),
        ("investigation_started", {"trigger": "alert", "note": "free text"}),
        ("plan_created", {"plan_id": 1, "version": 1, "source": "model", "rationale": "x"}),
    ],
)
def test_free_text_and_unknown_fields_are_rejected(
    event_type: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        event_payload(event_type, payload)


def test_an_undeclared_event_type_is_rejected() -> None:
    with pytest.raises(KeyError):
        event_payload("model_said", {})


def test_tool_names_of_the_registry_fit_the_pattern() -> None:
    for definition in registry:
        event_payload(
            "evidence_added",
            {"evidence_id": "ev_0123456789", "source": "kubernetes", "tool": definition.name},
        )


@pytest.mark.parametrize("case", ["s1-a", "s1-d", "s1-f"])
async def test_every_stored_event_fits_its_payload_model(
    migrated: Engine, pg_url: str, case: str
) -> None:
    result = await investigate(pg_url, case)
    assert result.events
    for event in result.events:
        assert event_payload(event.type, event.payload) == event.payload
