"""Stable, credential-free failures. Never attach driver errors to public results."""
from enum import StrEnum


class CommitUnknown(Exception):
    pg02_commit_unknown=True


class Code(StrEnum):
    CONFIG = "storage_config_invalid"
    NOT_READY = "postgres_application_not_ready"
    UNAVAILABLE = "storage_unavailable"
    PERMISSION = "storage_permission_denied"
    CONTEXT = "trusted_context_required"
    CONFLICT = "storage_conflict"
    INTEGRITY = "storage_integrity_error"
    ABORTED = "transaction_aborted"
    UNKNOWN_COMMIT = "commit_outcome_unknown"
    MIGRATION = "migration_history_conflict"


class StorageError(RuntimeError):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


def controlled_error(error, *, committing=False):
    state = getattr(error, "sqlstate", None)
    if committing and (state is None or str(state).startswith("08")):
        return StorageError(Code.UNKNOWN_COMMIT)
    code = {"23505": Code.CONFLICT, "23503": Code.INTEGRITY,
            "23514": Code.INTEGRITY, "23502": Code.INTEGRITY,
            "22003": Code.INTEGRITY, "42501": Code.PERMISSION,
            "25P02": Code.ABORTED}.get(state, Code.UNAVAILABLE)
    return StorageError(code)
