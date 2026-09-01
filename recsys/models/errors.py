class ModelInputError(ValueError):
    """The immutable data/config/model input failed validation."""


class ModelArtifactError(RuntimeError):
    """A model artifact is missing, unsafe, changed, or inconsistent."""


class TrainingCancelled(RuntimeError):
    """The caller requested cancellation at a safe training boundary."""
