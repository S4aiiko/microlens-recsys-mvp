"""Fail-closed exceptions for the data boundary."""


class DataPipelineError(RuntimeError):
    """Base class for deterministic data pipeline failures."""


class DataQualityError(DataPipelineError):
    """Raw data violates a declared quality rule."""


class ImmutableArtifactError(DataPipelineError):
    """An immutable artifact exists with unexpected content."""


class EventExportError(DataPipelineError):
    """An event export is malformed, inconsistent, or tampered."""


class HoldoutInsufficientError(EventExportError):
    """A quality-evaluation export lacks the predeclared later holdout."""
