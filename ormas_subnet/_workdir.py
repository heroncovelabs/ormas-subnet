"""Shared validation for untrusted identities used as local path components."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# The landed runner task-id grammar. Generated assignment ids
# (``asgn_<12 lowercase hex>``) are a strict subset; retaining the safe
# component superset preserves legacy checker identities and public fixtures.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")


def require_safe_component(value: Any, *, field_name: str) -> str:
    """Return a bounded ASCII path component or reject it without reinterpretation."""
    if not isinstance(value, str) or _SAFE_COMPONENT_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}")
    return value


def contained_workdir(root: Path, value: Any, *, field_name: str) -> Path:
    """Return exactly ``root/<validated value>`` and refuse a symlink leaf."""
    component = require_safe_component(value, field_name=field_name)
    parent = Path(root)
    child = parent / component
    if child.parent != parent or child.name != component:
        raise ValueError(f"invalid {field_name}")
    if child.is_symlink():
        raise ValueError(f"{field_name} workdir is a symlink")
    return child
