"""Tool errors.

Every failure of a tool call is a ``ToolError`` with a category. The category decides
whether the executor retries, and together with the object it forms the only error
text the model sees: ``<category>: <object>``. The text carries no timing and no retry
count, so the same failure always reads the same and recorded model responses keep
matching (the request key includes the tool results).
"""

from __future__ import annotations

from typing import Literal

#: Failure categories. The first four are retryable.
ErrorCategory = Literal[
    "timeout",
    "connection",
    "server_error",
    "throttled",
    "not_found",
    "forbidden",
    "invalid_arguments",
    "rejected",
    "unknown_tool",
    "failed",
]

RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {"timeout", "connection", "server_error", "throttled"}
)


class ToolError(Exception):
    """A tool call failed.

    Attributes:
        category: The failure category.
        target: What the call was about, such as ``pod demo-s1/s1-app-x``.
        detail: Extra detail for the call record only; never shown to the model.
    """

    def __init__(self, category: ErrorCategory, target: str, detail: str = "") -> None:
        """Store the category, the object and the detail."""
        super().__init__(f"{category}: {target}")
        self.category: ErrorCategory = category
        self.target = target
        self.detail = detail

    @property
    def retryable(self) -> bool:
        """Whether the executor may retry the call."""
        return self.category in RETRYABLE_CATEGORIES

    @property
    def model_text(self) -> str:
        """The error text given to the model: category and object only."""
        return f"{self.category}: {self.target}"


class ToolRegistrationError(RuntimeError):
    """A tool declaration violates a registry rule.

    Registration happens when the tool module is imported, so this error stops the
    process from starting.
    """
