# coding=utf-8

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class AlignmentMode:
    kind: str            # "sibling" | "offset" | "overlap"
    offset_ms: int = 0   # for "offset" only; 0 otherwise


_SIBLING_LEN_TOL = 2
_SIBLING_VARIANCE_MS = 200
_OFFSET_TOLERANCE_MS = 500
_MAX_OFFSET_MS = 30000


def detect_mode(primary, secondary):
    """Return the alignment mode for this primary/secondary pair.

    primary, secondary: lists of pysubs2 SSAEvent (start/end in ms).
    Uses index-based pairing when cue counts are similar. Offset detection
    via overlap pairing fails when the offset exceeds typical cue duration,
    so we look at the diffs of i-th cue starts directly.
    """
    if not primary or not secondary:
        return AlignmentMode(kind="overlap")

    if abs(len(primary) - len(secondary)) <= _SIBLING_LEN_TOL:
        common = min(len(primary), len(secondary))
        diffs = [secondary[i].start - primary[i].start for i in range(common)]
        if diffs:
            spread = max(diffs) - min(diffs)
            med = int(median(diffs))

            if spread < _SIBLING_VARIANCE_MS and abs(med) < _SIBLING_VARIANCE_MS:
                return AlignmentMode(kind="sibling", offset_ms=0)

            if spread < _OFFSET_TOLERANCE_MS and abs(med) <= _MAX_OFFSET_MS:
                return AlignmentMode(kind="offset", offset_ms=med)

    return AlignmentMode(kind="overlap")
