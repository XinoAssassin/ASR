"""Turn a provider's ``Transcript`` into what gets stored: the finalize stage of a job.

Every provider's output goes through here before persistence (server job runner; ARCH_REVIEW
§4-b), so local and cloud transcripts get identical treatment:

- [5b] post-correction (``asr.correct``): the corrected text becomes ``segments``, the raw
  provider text stays as the immutable ``original`` and every automatic edit is a ``Correction``
  (D41, AGENTS rule 9);
- a correction failure never fails the job: the raw text is kept and a warning is added.

Pure and synchronous (callers run it in a thread); the input transcript is never mutated. Further
stages that should run on every finished transcript (e.g. L2 correction, summary-on-complete)
belong here, not in the job queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asr.correct import correct_segments
from asr.providers.base import Correction, JobOptions, Segment, Transcript

log = logging.getLogger(__name__)

CORRECTION_FAILED = "Automatic correction failed; showing raw text"


@dataclass(frozen=True)
class FinalizedTranscript:
    """What a finished job stores.

    ``transcript`` carries the corrected ``segments`` and the combined ``warnings``; ``original``
    is the provider's raw segments; ``corrections`` point into ``transcript.segments``.
    """

    transcript: Transcript
    original: list[Segment]
    corrections: list[Correction]

    @property
    def segments(self) -> list[Segment]:
        return self.transcript.segments

    @property
    def warnings(self) -> list[str]:
        return self.transcript.warnings


def finalize_transcript(
    transcript: Transcript, options: JobOptions, *, label: str = "transcript"
) -> FinalizedTranscript:
    """Apply post-correction per ``options.correction``; ``label`` names the job in logs."""
    original = list(transcript.segments)
    warnings = list(transcript.warnings)
    segments, corrections = original, []
    opts = options.correction
    if opts.terms or opts.repetition:
        try:
            segments, corrections = correct_segments(original, options.terms, opts)
        except Exception:
            log.exception("post-correction failed for %s", label)
            warnings.append(CORRECTION_FAILED)
            segments, corrections = original, []
    return FinalizedTranscript(
        transcript=transcript.model_copy(update={"segments": list(segments), "warnings": warnings}),
        original=original,
        corrections=list(corrections),
    )
