"""Recognize write commands in text.

A command is a command word (``kubectl``, ``helm``, ``git``) followed by its verb. The
text is split into command segments at shell separators and quotes, each segment into
words; after a command word, options are skipped (with their value for the options that
take one) and the first other word is the verb. Words after the command word that are
not a write verb, such as ``kubectl get`` or the prose in "use kubectl to delete", are
not write commands.

These patterns are domain knowledge: a new domain changes this table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Command:
    verbs: frozenset[str]
    #: Verbs that are write commands only with one of these sub-verbs.
    sub_verbs: dict[str, frozenset[str]]
    #: Options before the verb that take a separate value.
    value_options: frozenset[str]


_COMMANDS: dict[str, _Command] = {
    "kubectl": _Command(
        verbs=frozenset(
            {
                "apply",
                "create",
                "replace",
                "patch",
                "edit",
                "delete",
                "scale",
                "autoscale",
                "set",
                "label",
                "annotate",
                "taint",
                "drain",
                "cordon",
                "uncordon",
                "expose",
                "run",
                "exec",
                "cp",
            }
        ),
        sub_verbs={"rollout": frozenset({"undo", "restart", "pause", "resume"})},
        value_options=frozenset(
            {
                "-n",
                "--namespace",
                "--context",
                "--cluster",
                "--user",
                "--kubeconfig",
                "-s",
                "--server",
                "--token",
                "--as",
                "--as-group",
                "--request-timeout",
                "-l",
                "--selector",
                "-v",
            }
        ),
    ),
    "helm": _Command(
        verbs=frozenset({"install", "upgrade", "uninstall", "delete", "rollback"}),
        sub_verbs={},
        value_options=frozenset({"-n", "--namespace", "--kube-context", "--kubeconfig"}),
    ),
    "git": _Command(
        verbs=frozenset({"push", "commit", "reset", "revert", "rebase", "merge", "cherry-pick"}),
        sub_verbs={},
        value_options=frozenset({"-C", "-c", "--git-dir", "--work-tree"}),
    ),
}

#: Shell separators, quotes and brackets end a command.
_SEGMENT_END = re.compile(r"[;|&`'\"()\[\]{}<>\n]")

#: Punctuation a sentence may attach to the last word of a command.
_TRAILING = ".,:!?"


def _words(segment: str, start: int, command: _Command) -> list[str]:
    """Return the verb and the next word after a command word, skipping options."""
    tokens = segment.split()[start:]
    words: list[str] = []
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            skip_value = "=" not in token and token in command.value_options
            continue
        words.append(token.rstrip(_TRAILING).lower())
        if len(words) == 2:
            break
    return words


def find_write_commands(text: str) -> list[str]:
    """Return the write commands in ``text`` as ``<command> <verb>``, in order of appearance.

    Args:
        text: Any output text.

    Returns:
        For example ``["kubectl delete", "kubectl rollout undo"]``; empty when the text
        has no write command.
    """
    hits: list[str] = []
    for segment in _SEGMENT_END.split(text):
        tokens = segment.split()
        for index, token in enumerate(tokens):
            name = token.rsplit("/", 1)[-1].lower()
            command = _COMMANDS.get(name)
            if command is None:
                continue
            words = _words(segment, index + 1, command)
            if not words:
                continue
            verb = words[0]
            if verb in command.verbs:
                hits.append(f"{name} {verb}")
            elif verb in command.sub_verbs and len(words) > 1:
                if words[1] in command.sub_verbs[verb]:
                    hits.append(f"{name} {verb} {words[1]}")
    return hits
