"""The exported JSON Schemas validate the same samples as the pydantic schemas."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from gate_cases import load_cases

from bianque.cli import main
from bianque.gate import OutputType, export_json_schemas, json_schema

#: The rule the JSON Schema does not state; pydantic checks it (gate/schemas.py).
CROSS_FIELD_CODES = {"value_error"}


def test_exported_schemas_are_valid_json_schemas() -> None:
    schemas = export_json_schemas()
    assert set(schemas) == {f"{t}.schema.json" for t in OutputType}
    for schema in schemas.values():
        jsonschema.Draft202012Validator.check_schema(schema)


def test_json_schema_agrees_with_the_schema_validator() -> None:
    assert "date-time" in jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers
    for case in load_cases():
        validator = jsonschema.Draft202012Validator(
            json_schema(case.output_type),
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )
        schema_codes = {code for name, _, code in case.violations if name == "schema"}
        valid = validator.is_valid(case.output)
        if not schema_codes or schema_codes <= CROSS_FIELD_CODES:
            assert valid, (case.id, [e.message for e in validator.iter_errors(case.output)])
        else:
            assert not valid, case.id


def test_cli_writes_one_file_per_output_type(tmp_path: Path) -> None:
    assert main(["gate", "export-schemas", "--out", str(tmp_path)]) == 0
    written = {path.name: json.loads(path.read_text()) for path in tmp_path.iterdir()}
    assert written == export_json_schemas()
