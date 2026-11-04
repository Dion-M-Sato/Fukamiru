"""Domain-specific exceptions."""


class FukamiruError(Exception):
    """Base exception for expected application failures."""


class ExternalServiceUnavailable(FukamiruError):
    """A remote capacity/rate failure, not a manuscript quality verdict."""


class NonRetryableFukamiruError(FukamiruError):
    """Expected failure for which repeating the same Node input cannot help."""


class EmptyEmbeddingOffsetsError(NonRetryableFukamiruError):
    """An extracted span cannot be tokenized; PDF inputs need fresh OCR."""


class SQLiteRecoveryExhaustedError(FukamiruError):
    """Bounded retries with fresh SQLite transactions have been exhausted."""


class InferenceEngineAuthorizationRequired(
    ExternalServiceUnavailable, NonRetryableFukamiruError
):
    """A V22 engine change is paused until its exact fingerprint is approved."""

    def __init__(
        self,
        message: str,
        *,
        previous_fingerprint: str,
        target_fingerprint: str,
    ) -> None:
        super().__init__(message)
        self.previous_fingerprint = previous_fingerprint
        self.target_fingerprint = target_fingerprint


class CoherenceResolutionRequired(NonRetryableFukamiruError):
    """A manuscript coherence defect needs research or human intervention."""


class RunCancelled(FukamiruError):
    """Raised inside a worker after its canonical run state was cancelled."""


class ConfigurationError(FukamiruError):
    """Raised when local configuration is missing or inconsistent."""


class MigrationError(FukamiruError):
    """Raised when a database migration cannot be applied safely."""


class ContractError(FukamiruError):
    """Raised when a workflow or node contract is invalid."""


class CanonicalHTMLValidationError(FukamiruError):
    """Raised when canonical HTML fails structural validation."""


class GitCheckpointError(FukamiruError):
    """Raised when a requested Git checkpoint cannot be created."""
