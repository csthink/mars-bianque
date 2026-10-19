"""Recognize write commands in text.

A command is a command word (``kubectl``, ``oc``, ``helm``, ``git``) followed by its
verb. A command word is found wherever it is not part of a longer ASCII word, so text,
Markdown emphasis or full-width punctuation around it do not hide it (``执行kubectl``,
``**kubectl delete**``). The words after it are ASCII tokens; any other character
separates them, and a quoted value stays inside its token (``--context="kind"``). A
command ends at a shell separator or a full-width sentence end. Options are skipped,
with their value for the options that take one, and the first other word is the verb.
Words that are not a write verb, such as ``kubectl get`` or the prose in "use kubectl
to delete", are not write commands.

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
    #: Options that take a separate value.
    value_options: frozenset[str]


_KUBECTL = _Command(
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
            "debug",
        }
    ),
    sub_verbs={
        "rollout": frozenset({"undo", "restart", "pause", "resume"}),
        "certificate": frozenset({"approve", "deny"}),
    },
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
            "--as-uid",
            "--request-timeout",
            "--cache-dir",
            "--certificate-authority",
            "--client-certificate",
            "--client-key",
            "--tls-server-name",
            "--username",
            "--password",
            "-l",
            "--selector",
            "-v",
        }
    ),
)

_COMMANDS: dict[str, _Command] = {
    "kubectl": _KUBECTL,
    "oc": _KUBECTL,
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

#: A command word that is not part of a longer ASCII word; a path prefix is allowed.
_COMMAND_WORD = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_./-]*/)?(kubectl|oc|helm|git)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

#: An ASCII word; quoted parts belong to the word they are attached to.
_TOKEN = re.compile(r"""(?:[A-Za-z0-9_./=:@+%,~-]|"[^"\n]*"|'[^'\n]*')+""")

#: Shell separators and full-width sentence ends end a command.
_COMMAND_END = re.compile(r"[;|&`\n。；！？]")

#: Punctuation a sentence may attach to a word.
_TRAILING = ".,:"

#: ``git commit`` followed by a commit hash names a commit; it is not a command, since
#: ``git commit`` takes no commit argument.
_COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")


def _words(rest: str, command: _Command) -> list[str]:
    """Return the verb and the next word after a command word, skipping options."""
    words: list[str] = []
    skip_value = False
    for token in _TOKEN.findall(_COMMAND_END.split(rest, maxsplit=1)[0]):
        word = token.rstrip(_TRAILING)
        if not word:
            continue
        if skip_value:
            skip_value = False
            continue
        if word.startswith("-"):
            skip_value = "=" not in word and word in command.value_options
            continue
        words.append(word)
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
    for match in _COMMAND_WORD.finditer(text):
        name = match.group(1).lower()
        command = _COMMANDS[name]
        words = _words(text[match.end() :], command)
        if not words:
            continue
        verb = words[0].lower()
        following = words[1] if len(words) > 1 else ""
        if verb in command.verbs:
            if name == "git" and verb == "commit" and _COMMIT_HASH.fullmatch(following):
                continue
            hits.append(f"{name} {verb}")
        elif following.lower() in command.sub_verbs.get(verb, frozenset()):
            hits.append(f"{name} {verb} {following.lower()}")
    return hits
