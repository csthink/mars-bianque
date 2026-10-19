"""Redaction rule set.

Rules run first; the entropy fallback (``entropy``) runs after them. Each class of
sensitive content is one ``Rule``: a key-value or format regex plus an in-place rewrite
function. Detection and rewriting are separate: ``Rule.finditer`` only locates hits
(used by detection), and ``Rule.rewrite`` produces the masked text (used by the engine).

Covered classes:
  secret / token / password / API key / Authorization / cookie / DSN /
  private IP (generalized, structure kept) / internal domain suffix (generalized to
  ``*.internal``).

Design notes:
- **One placeholder prefix** ``<redacted:...>``. This is what makes redaction
  idempotent: placeholders are never matched by a rule again (they contain no quotes,
  no ``=`` and no long base64 run; see each rule and the engine's idempotence notes).
- **Private IPs keep their structure**: the first two octets are kept and the last two
  become ``x.x`` (``10.12.x.x``), so the subnet stays visible but the host does not.
- **Internal domains**: a FQDN that ends in a configured suffix is generalized to
  ``*.internal`` as a whole (no labels are kept). Deployments configure the suffixes
  with ``BIANQUE_REDACT_INTERNAL_DOMAINS``; the engine takes them as an explicit
  parameter, and reading the environment belongs to the config layer, not this module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

#: Common prefix of every placeholder. All masked output starts with it, which helps
#: (1) manual cross-reference and (2) idempotence: placeholder text deliberately has no
#: form that a rule or the entropy check would match again (no quoted key-value pair,
#: no run of 20+ base64 characters).
PLACEHOLDER_PREFIX = "<redacted:"

#: Sensitive key names for key-value pairs (case-insensitive). A match in the form
#: "key = value" or "key: value" masks the value. Covers the key side of
#: secret / token / password / API key / authorization / cookie.
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api[_-]?key",
    "apikey",
    "access[_-]?key",
    "secret[_-]?key",
    "authorization",
    "auth[_-]?token",
    "cookie",
    "set[_-]?cookie",
    "client[_-]?secret",
    "private[_-]?key",
)


@dataclass(frozen=True)
class Rule:
    """One redaction rule: name, regex, and in-place rewrite (one regex for both)."""

    name: str
    regex: re.Pattern[str]
    #: Rewrites a match into masked text; structure-preserving rules (such as private
    #: IPs) generalize here.
    rewrite: Callable[[re.Match[str]], str]

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        """Detection: locate every hit of this rule without rewriting."""
        return self.regex.finditer(text)


def _placeholder(kind: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{kind}>"


# -- Key-value: key=value / key: value, value optionally quoted -------------------
# Only the value is masked; the key and separator are kept so log lines stay readable.
# The value has mutually exclusive branches, quoted forms first:
#   1. quoted: mask everything up to the matching quote (spaces included);
#   2. unquoted: mask the non-empty run up to a delimiter (whitespace , ; &).
# The key lookbehind (?<![A-Za-z0-9]) lets compound keys joined with _ or - match
# (db_secret, x-api_key: the preceding separator is not alphanumeric), but does not cut
# into unrelated words (in "mysecretive:secret" the "secret" after "my" is preceded by a
# letter and is rejected).
_KV_VALUE = r"""(?:"(?P<vdq>[^"\r\n]+)"|'(?P<vsq>[^'\r\n]+)'|(?P<vraw>(?:(?![\s,;&])[^\r\n])+))"""
_kv_key_alt = "|".join(_SENSITIVE_KEYS)
_KV_RE = re.compile(
    rf"""(?<![A-Za-z0-9])(?P<key>(?:{_kv_key_alt}))(?P<sep>\s*[=:]\s*){_KV_VALUE}""",
    re.IGNORECASE,
)


def _rewrite_kv(m: re.Match[str]) -> str:
    # Keep the original quoting for cross-reference; replace only the value.
    if m.group("vdq") is not None:
        wrapped = f'"{_placeholder("secret")}"'
    elif m.group("vsq") is not None:
        wrapped = f"'{_placeholder('secret')}'"
    else:
        wrapped = _placeholder("secret")
    return f"{m.group('key')}{m.group('sep')}{wrapped}"


# -- Authorization / Bearer / Basic header (value follows the scheme) -------------
_AUTH_RE = re.compile(
    r"""(?P<scheme>\b(?:Bearer|Basic|Token)\s+)(?P<val>[A-Za-z0-9._+/=~-]{8,})""",
    re.IGNORECASE,
)


def _rewrite_auth(m: re.Match[str]) -> str:
    return f"{m.group('scheme')}{_placeholder('token')}"


# -- DSN / connection string: scheme://user:password@host..., mask user:password ---
# Covers postgres/mysql/redis/amqp/mongodb/http(s) and other URIs with credentials.
# Credentials must be masked up to the **last** @ (the userinfo/host separator);
# otherwise a password containing a bare @ (such as pa@ss) is cut at the first @ and its
# tail leaks.
# Structure: after scheme://, cred = user (may be empty) : password (may contain @,
# matched lazily up to the last @), followed by a host authority (a label that does not
# start with @, :, or whitespace) and then a port, path, query, fragment, whitespace, or
# end of line. That lookahead pins the last @ as the userinfo boundary and also rejects
# URIs without credentials (no user:pass@ part, no match).
_DSN_RE = re.compile(
    r"""(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]*://)"""
    r"""(?P<cred>[^:@/\s]*:[^\s]*?)(?P<at>@)"""
    r"""(?P<host>[^/@:\s]+)(?=[:/?#\s]|$)""",
)


def _rewrite_dsn(m: re.Match[str]) -> str:
    return f"{m.group('scheme')}{_placeholder('dsn-credential')}{m.group('at')}{m.group('host')}"


# -- Private IP (RFC 1918), generalized with structure kept -----------------------
# The three RFC 1918 blocks: 10/8, 172.16/12 and 192.168/16. Keep the first two octets;
# the last two become x.x.
_IPV4_RE = re.compile(r"\b(?P<a>\d{1,3})\.(?P<b>\d{1,3})\.(?P<c>\d{1,3})\.(?P<d>\d{1,3})\b")


def _is_rfc1918(a: int, b: int) -> bool:
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def _octets_valid(*vals: int) -> bool:
    return all(0 <= v <= 255 for v in vals)


def _rewrite_private_ip(m: re.Match[str]) -> str:
    a, b, c, d = (int(m.group(g)) for g in ("a", "b", "c", "d"))
    if not _octets_valid(a, b, c, d) or not _is_rfc1918(a, b):
        # Not RFC 1918 (public IPs are useful for diagnosis) or an invalid octet:
        # keep as is.
        return m.group(0)
    return f"{a}.{b}.x.x"


# -- Well-known token formats: AWS AKIA, GitHub ghp_/gho_, three-part JWT ----------
# These formats are distinctive enough to match directly, without relying on the
# entropy threshold, so edge cases are not missed.
_KNOWN_TOKEN_RE = re.compile(
    r"""(?<![A-Za-z0-9])(?:
        AKIA[0-9A-Z]{16}                                   # AWS access key id
      | (?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}           # GitHub token family
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}  # JWT
    )(?![A-Za-z0-9])""",
    re.VERBOSE,
)


def _rewrite_known_token(m: re.Match[str]) -> str:
    return _placeholder("token")


def _compile_internal_domain_rule(suffixes: Iterable[str]) -> Rule | None:
    """Build the internal domain rule from suffixes; return None when there are none."""
    cleaned = sorted({s.strip().lstrip(".").lower() for s in suffixes if s.strip()})
    if not cleaned:
        return None
    alt = "|".join(re.escape(s) for s in cleaned)
    # FQDN: one or more labels followed by a configured suffix, bounded by characters
    # that cannot belong to a domain name.
    regex = re.compile(
        rf"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?:{alt})(?![A-Za-z0-9.-])",
        re.IGNORECASE,
    )
    return Rule(
        name="internal-domain",
        regex=regex,
        rewrite=lambda m: "*.internal",
    )


#: Rules that do not depend on the suffix list. Order is rewrite priority: an earlier
#: rule rewrites first, and later rules do not touch its output. DSN runs before
#: key-value because the password@ part of a connection string has a different
#: structure; Authorization and well-known tokens are separate rules.
_STATIC_RULES: tuple[Rule, ...] = (
    Rule("dsn", _DSN_RE, _rewrite_dsn),
    Rule("authorization", _AUTH_RE, _rewrite_auth),
    Rule("known-token", _KNOWN_TOKEN_RE, _rewrite_known_token),
    Rule("kv-secret", _KV_RE, _rewrite_kv),
    Rule("private-ip", _IPV4_RE, _rewrite_private_ip),
)


def build_rules(internal_domain_suffixes: Iterable[str] = ()) -> tuple[Rule, ...]:
    """Assemble the full rule set: static rules plus an optional internal domain rule.

    The internal domain rule goes first so other rules do not split a FQDN before it is
    generalized. Domains contain no key-value or DSN trigger, but generalizing them
    first keeps the semantics simplest.
    """
    domain_rule = _compile_internal_domain_rule(internal_domain_suffixes)
    if domain_rule is None:
        return _STATIC_RULES
    return (domain_rule, *_STATIC_RULES)
