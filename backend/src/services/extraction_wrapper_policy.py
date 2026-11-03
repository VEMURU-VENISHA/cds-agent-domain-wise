"""Shared policy for whether a CDS view can be offered an extraction wrapper."""

from collections.abc import Mapping
from typing import Any


def can_create_extraction_wrapper(cds_view: Any) -> bool:
    """Return true only for an explicitly UNKNOWN individual CDS extraction status."""
    if isinstance(cds_view, str):
        status = cds_view
    elif isinstance(cds_view, Mapping):
        status = cds_view.get("status", cds_view.get("data_extraction_status"))
    else:
        status = getattr(cds_view, "status", getattr(cds_view, "data_extraction_status", None))
    return isinstance(status, str) and status.strip().upper() == "UNKNOWN"
