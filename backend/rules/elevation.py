"""Compare elevations using the task's metre tolerance."""
from .measurement import run_measurement

CATEGORY = "标高"


def run_elevation(store, version_id, **options):
    return run_measurement(store, version_id, CATEGORY, **options)
