"""Recognize write commands in text.

A command is a command word (``kubectl``, ``oc``, ``helm``, ``git``) followed by its
verb. A command word is found wherever it is not part of a longer ASCII word, so text,
Markdown emphasis or full-width punctuation around it do not hide it (``执行kubectl``,
``**kubectl delete**``). After it, only ASCII words separated by whitespace other than a
line break, or by a shell line continuation, belong to the command; a quoted value
stays inside its word (``--context="kind"``). Any other character ends the command, and
so does a word that ends a clause with ``.``, ``,`` or ``:``; prose that goes on after
the command word (``git 仓库最近一次 commit``, ``kubectl. Delete nothing``) therefore
has no verb. Options are skipped, with their value for the options that take one, and
the first other word is the verb. Words that are not a write verb, such as ``kubectl
get`` or the prose in "use kubectl to delete", are not write commands.

A command that directly follows a determiner on the same line and has no arguments names
a thing, not a command to run: "the git commit author", "the kubectl apply action",
"The helm upgrade failed.", "这次 kubectl apply 操作". It has no arguments when the word
right after the command words, if any, is a conjunction, an auxiliary, a descriptive
noun, a verb that describes what happened (``changed``, ``failed``) or a time preposition
(``at``, ``before``), each from a fixed list, and no word in the rest of the sentence
looks like an argument: an option, an
assignment, a path, a ``kind/name`` with a Kubernetes kind, a YAML or JSON file name, or
a word that mixes letters and digits such as ``s1-app``. Evidence, knowledge, suggestion
and investigation IDs are left out first, so a cited ``ev_9cfa6c69db`` does not count,
and neither do plain numbers, timestamps, version numbers or ``ReplicaSet/Deployment``.
Otherwise it stays a command ("a kubectl rollout undo deployment/x", "the git push
origin main", "the kubectl delete of pod x"), and so it does after an imperative verb
right before the determiner ("Try the kubectl delete"). A command without a target
after a determiner ("a git push is needed") is taken as a noun on purpose: it names an
operation but gives nothing to run.

The verbs are listed one by one rather than recognized by an ``-ed`` ending: a past
participle has the same form, and right after a command it makes the command one to run,
with a target that need not look like an argument ("the git push required for origin
main"). Verbs of need or advice, modal verbs, and prepositions that introduce what a
command acts on (``of``, ``for``, ``to``, ``on``) are therefore left out, and so is any
verb not listed: it keeps the command a command.

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

#: An ASCII word preceded by whitespace other than a line break (a full-width or no-break
#: space counts) or by a shell line continuation; quoted parts belong to the word.
_NEXT_WORD = re.compile(r"""(?:[^\S\n]|\\\n)+((?:[A-Za-z0-9_./=:@+%,~-]|"[^"\n]*"|'[^'\n]*')+)""")

#: Punctuation that ends a clause when it ends a word.
_TRAILING = ".,:"

#: Determiners after which a command without arguments is a noun.
_DETERMINERS = frozenset(
    {"the", "a", "an", "this", "that", "these", "those", "its", "their", "which", "whose", "each"}
)
_ZH_DETERMINERS = (
    "这次",
    "此次",
    "那次",
    "这个",
    "那个",
    "这条",
    "那条",
    "该次",
    "该条",
    "该个",
    "该",
)
#: Characters before "该" that make it "should" ("应该", "本该", "接下来该").
_ZH_MODAL_BEFORE_GAI = "应本就早需活来也还都才"

#: Verbs that make a following determiner phrase an instruction again.
_IMPERATIVES = frozenset(
    {"run", "execute", "use", "issue", "perform", "try", "type", "enter", "do", "rerun"}
)
_ZH_IMPERATIVES = ("执行", "运行", "使用", "请", "需要", "可以")

#: Words that may follow a command used as a noun; any other word is an argument.
_NOUN_CONTINUATIONS = frozenset(
    {
        "and", "or", "but", "nor", "that", "which", "who",
        "is", "are", "was", "were", "has", "had", "did", "does",
        "isn't", "aren't", "wasn't", "weren't", "hasn't", "hadn't", "didn't", "doesn't",
        "author", "authors", "action", "actions", "step", "steps", "operation", "operations",
        "history", "record", "records", "entry", "event", "events", "output", "message",
        "hash", "time", "date", "timestamp", "timestamps", "itself",
    }
)  # fmt: skip

#: Verbs that describe what happened, past and present; they may also follow a command
#: used as a noun ("The helm upgrade failed").
_PREDICATES = frozenset(
    {
        "failed", "fails", "succeeded", "succeeds", "completed", "completes",
        "finished", "finishes", "ran", "runs", "happened", "happens", "occurred", "occurs",
        "started", "starts", "ended", "ends", "changed", "changes", "modified", "modifies",
        "updated", "updates", "introduced", "introduces", "removed", "removes", "added",
        "adds", "reverted", "reverts", "triggered", "triggers", "caused", "causes",
        "broke", "breaks", "created", "creates", "landed", "lands", "showed", "shows",
        "returned", "returns", "produced", "produces", "appeared", "appears",
        "contained", "contains", "touched", "touches", "edited", "edits", "timed",
    }
)  # fmt: skip

#: Time prepositions, which may also follow a command used as a noun ("The git commit at
#: 10:42:07Z").
_TIME_PREPOSITIONS = frozenset({"at", "before", "after", "during", "since", "until", "around"})

_NOUN_FOLLOWERS = _NOUN_CONTINUATIONS | _PREDICATES | _TIME_PREPOSITIONS

#: The last two ASCII words before a command word, on the same line; the first one only
#: when nothing but whitespace separates the two.
_PRECEDING = re.compile(r"(?:([A-Za-z]+)[^\S\n]+)?([A-Za-z]+)[^\S\n]+$")
_TRAILING_SPACE = re.compile(r"[^\S\n]+$")

#: The end of a sentence: ".", ";", "!" or "?" before whitespace or the text end, the
#: full-width ones, or a line break.
_SENTENCE_END = re.compile(r"[.;!?](?=\s|$)|[。；！？\n]")

#: Kubernetes resource kinds as kubectl writes them, lower case, before ``/name``.
_KINDS = frozenset(
    {
        "pod", "pods", "po", "deployment", "deployments", "deploy", "replicaset",
        "replicasets", "rs", "service", "services", "svc", "configmap", "configmaps", "cm",
        "secret", "secrets", "job", "jobs", "cronjob", "cronjobs", "statefulset",
        "statefulsets", "sts", "daemonset", "daemonsets", "ds", "node", "nodes",
        "namespace", "namespaces", "ns", "ingress", "ingresses", "ing", "pvc", "pv", "hpa",
        "serviceaccount", "sa",
    }
)  # fmt: skip

#: Timestamps and version numbers, which mix letters and digits but are no arguments.
_TIME_OR_VERSION = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}T?)?\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{4}-\d{2}-\d{2}|v?\d+(?:\.\d+)+"
)

#: An English word with an optional contraction or possessive.
_ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

#: Evidence, knowledge, suggestion and investigation IDs, which a report cites inline.
_IDS = re.compile(r"\b(?:ev|kn|sg|inv)_[0-9A-Za-z]+")

#: Punctuation around a word that is not part of it.
_WRAPPING = "()[]{}（）「」『』`\"'“”‘’*,.;:!?。，；：→—"

#: ``git commit`` directly followed by a commit hash names a commit; it is not a
#: command, since ``git commit`` takes no commit argument. For the same reason a hash in
#: the rest of the sentence is no argument of ``git commit``.
_COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")


def _is_argument(word: str, hit: str) -> bool:
    """Whether one word of a sentence looks like what a command acts on."""
    word = word.strip(_WRAPPING)
    if re.match(r"--?[A-Za-z]", word):
        return True
    left, slash, right = word.partition("/")
    if slash and (
        word.startswith(("/", "./", "~/"))
        or left in _KINDS
        or re.search(r"[0-9.-]", right) is not None
    ):
        return True
    if re.search(r"\.(?:ya?ml|json)$", word):
        return True
    if re.search(r"[^=\s]=[^=\s]", word):
        return True
    if hit == "git commit" and _COMMIT_HASH.fullmatch(word):
        return False
    if _TIME_OR_VERSION.fullmatch(word):
        return False
    return re.search(r"[A-Za-z]", word) is not None and re.search(r"[0-9]", word) is not None


def _follows_determiner(text: str, start: int) -> bool:
    """Whether the command word at ``start`` directly follows a determiner on its line."""
    line = text[text.rfind("\n", 0, start) + 1 : start]
    stripped = _TRAILING_SPACE.sub("", line)
    for determiner in _ZH_DETERMINERS:
        if stripped.endswith(determiner):
            before = _TRAILING_SPACE.sub("", stripped[: -len(determiner)])
            if determiner == "该" and before[-1:] and before[-1] in _ZH_MODAL_BEFORE_GAI:
                return False
            return not before.endswith(_ZH_IMPERATIVES)
    match = _PRECEDING.search(line)
    if match is None or match.group(2).lower() not in _DETERMINERS:
        return False
    return (match.group(1) or "").lower() not in _IMPERATIVES


def _has_arguments(text: str, end: int, hit: str) -> bool:
    """Whether the command ``hit``, whose words follow position ``end``, has arguments.

    An option among the command words, a next word that is not in
    ``_NOUN_FOLLOWERS``, or a word of the rest of the sentence that
    ``_is_argument`` accepts is one.
    """
    size = len(hit.split()) - 1
    position = end
    count = 0
    while count < size:
        match = _NEXT_WORD.match(text, position)
        if match is None:
            break
        position = match.end()
        token = match.group(1)
        word = token.rstrip(_TRAILING)
        if word.startswith("-"):
            return True
        if word:
            count += 1
        if token.endswith("."):
            return False
    sentence_end = _SENTENCE_END.search(text, position)
    stop = sentence_end.start() if sentence_end else len(text)
    following = _NEXT_WORD.match(text, position, stop)
    if following is not None:
        # A contraction or a possessive is one word here ("isn't", "author's").
        english = _ENGLISH_WORD.match(text, following.start(1), stop)
        word = english.group(0) if english else following.group(1).rstrip(_TRAILING)
        word = re.sub(r"['’]s$", "", word.lower())
        if word and word not in _NOUN_FOLLOWERS:
            return True
    rest = _IDS.sub(" ", text[position:stop])
    return any(_is_argument(word, hit) for word in rest.split())


def _names_a_thing(text: str, start: int, end: int, hit: str) -> bool:
    """Whether the command ``hit`` found at ``start`` is a noun rather than a command."""
    return _follows_determiner(text, start) and not _has_arguments(text, end, hit)


def _words(text: str, start: int, command: _Command) -> tuple[list[str], bool]:
    """Return the verb and the next word after a command word, skipping options.

    Returns:
        The words, and whether the second word directly follows the verb with no
        option in between.
    """
    words: list[str] = []
    adjacent = True
    skip_value = False
    position = start
    while len(words) < 2:
        match = _NEXT_WORD.match(text, position)
        if match is None:
            break
        position = match.end()
        token = match.group(1)
        word = token.rstrip(_TRAILING)
        if not word:
            break
        if skip_value:
            skip_value = False
        elif word.startswith("-"):
            skip_value = "=" not in word and word in command.value_options
            if words:
                adjacent = False
        else:
            words.append(word)
        if word != token:
            break
    return words, adjacent


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
        words, adjacent = _words(text, match.end(), command)
        if not words:
            continue
        verb = words[0].lower()
        following = words[1] if len(words) > 1 else ""
        if verb in command.verbs:
            names_commit = adjacent and _COMMIT_HASH.fullmatch(following)
            if name == "git" and verb == "commit" and names_commit:
                continue
            hit = f"{name} {verb}"
        elif following.lower() in command.sub_verbs.get(verb, frozenset()):
            hit = f"{name} {verb} {following.lower()}"
        else:
            continue
        if not _names_a_thing(text, match.start(), match.end(), hit):
            hits.append(hit)
    return hits
