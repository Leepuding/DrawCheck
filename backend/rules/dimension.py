"""Compare dimensions using the task's millimetre tolerance."""
from .measurement import run_measurement

CATEGORY = "尺寸"
# Historical machine questions with this reason are superseded by coverage.
NO_UNIT_REASON = "尺寸单位缺失或看不清，待确认"


def run_dimension(store, version_id, **options):
    return run_measurement(store, version_id, CATEGORY, **options)
