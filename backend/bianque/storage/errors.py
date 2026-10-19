"""Errors raised by the repositories."""


class StorageError(RuntimeError):
    """Base class of repository errors."""


class DatabaseNotConfiguredError(StorageError):
    """``BIANQUE_DATABASE_URL`` is not set."""


class NotFoundError(StorageError):
    """The requested row does not exist."""


class AlreadyDecidedError(StorageError):
    """The suggestion already has a confirmation; each suggestion is decided once."""
