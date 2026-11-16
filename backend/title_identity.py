"""Field-level manual title authority shared by extraction, catalogue and rules."""

import re

MANUAL_NUMBER_FIELD = "manual_drawing_number_confirmed"
_PROJECT_NUMBER = re.compile(
    r"[0-9]{2}-(?P<project>[A-Z][A-Z0-9]*[0-9][A-Z0-9]*)-[A-Z]+[0-9]{3,6}(?:-[0-9]{2,}[A-Z]?)?",
    re.I,
)


def project_code(number):
    """Only the project segment of the observed, structured title-block format."""
    match = _PROJECT_NUMBER.fullmatch((number or "").strip())
    return match["project"].upper() if match else None


def manual_number_mode(unit):
    """Legacy flags cannot prove which field was edited.

    Preserve an existing nonempty, non-uncertain legacy manual number for
    compatibility. An explicitly uncertain historical identity stays uncertain.
    New name/specialty-only edits always carry an explicit False marker.
    """
    meta = unit.get("metadata")
    if not isinstance(meta, dict):
        meta = unit  # Persisted catalogue rows carry their field markers directly.
    if MANUAL_NUMBER_FIELD in meta:
        return "explicit" if meta[MANUAL_NUMBER_FIELD] is True else None
    if (meta.get("manual_correction") and (unit.get("drawing_number") or "").strip()
            and meta.get("title_identity_uncertain") is not True):
        return "legacy_unverified_field"
    return None


def has_manual_number(unit):
    # Explicit blank is still a human decision: do not restore an older value.
    return manual_number_mode(unit) is not None
