import re

from bianque.storage import ids


def test_investigation_ids_are_unique_ulids() -> None:
    first, second = ids.new_investigation_id(), ids.new_investigation_id()
    assert first != second
    for value in (first, second):
        assert re.fullmatch(r"inv_[0-9A-HJKMNP-TV-Z]{26}", value)


def test_evidence_id_is_derived_from_content() -> None:
    params = {"namespace": "demo-s1", "name": "s1-app"}
    first = ids.evidence_id("get_events", params, "redacted text")
    assert first == ids.evidence_id("get_events", dict(params), "redacted text")
    assert re.fullmatch(r"ev_[0-9a-f]{10}", first)


def test_evidence_id_ignores_parameter_order() -> None:
    a = ids.evidence_id("get_pod", {"namespace": "n", "name": "p"}, "x")
    b = ids.evidence_id("get_pod", {"name": "p", "namespace": "n"}, "x")
    assert a == b


def test_evidence_id_changes_with_any_field() -> None:
    base = ids.evidence_id("get_pod", {"name": "p"}, "x")
    assert ids.evidence_id("get_logs", {"name": "p"}, "x") != base
    assert ids.evidence_id("get_pod", {"name": "q"}, "x") != base
    assert ids.evidence_id("get_pod", {"name": "p"}, "y") != base


def test_field_boundaries_are_unambiguous() -> None:
    assert ids.knowledge_id("ab", "c", 0) != ids.knowledge_id("a", "bc", 0)


def test_alert_evidence_id_is_separate_from_tool_evidence() -> None:
    first = ids.alert_evidence_id("fp1", "text")
    assert first == ids.alert_evidence_id("fp1", "text")
    assert first != ids.alert_evidence_id("fp2", "text")
    assert first != ids.evidence_id("alertmanager", {}, "text")
    assert re.fullmatch(r"ev_[0-9a-f]{10}", first)


def test_suggestion_id_is_derived_from_content() -> None:
    args = (1, "Roll back the image", "kubectl rollout undo deploy/s1-app", "change")
    first = ids.suggestion_id(*args, ["ev_1", "ev_2"])
    assert first == ids.suggestion_id(*args, ["ev_2", "ev_1"])
    assert first != ids.suggestion_id(2, *args[1:], ["ev_1", "ev_2"])
    assert first != ids.suggestion_id(*args[:3], "read_only", ["ev_1", "ev_2"])
    assert first != ids.suggestion_id(1, "Roll back the image", None, "change", ["ev_1", "ev_2"])
    assert re.fullmatch(r"sg_[0-9a-f]{10}", first)


def test_knowledge_id_is_stable_across_imports() -> None:
    first = ids.knowledge_id("runbooks/oom.md", "OOMKilled > Diagnosis", 3)
    assert first == ids.knowledge_id("runbooks/oom.md", "OOMKilled > Diagnosis", 3)
    assert first != ids.knowledge_id("runbooks/oom.md", "OOMKilled > Diagnosis", 4)
    assert re.fullmatch(r"kn_[0-9a-f]{12}", first)
