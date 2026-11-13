"""Environment-variable helpers shared by every service.

Each service reads its environment exactly once, in its own ``load_config``;
this module owns the required/optional/default/cast behaviour so the services
do not each grow their own copy.
"""

import os
from typing import Callable, TypeVar

T = TypeVar("T")

_MISSING = object()


def env(
    name: str,
    *,
    cast: Callable[[str], T] | None = None,
    default=_MISSING,
):
    """Read ``name`` from the environment.

    A missing or blank value returns ``default`` when one is supplied, and
    otherwise raises ``RuntimeError`` naming the variable. ``cast`` failures
    raise ``ValueError`` naming the variable, so a bad value is as easy to
    diagnose as a missing one.
    """
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        if default is not _MISSING:
            return default
        raise RuntimeError(f"Required environment variable '{name}' not set.")

    if cast is None:
        return value

    try:
        return cast(value)
    except Exception as exc:
        raise ValueError(
            f"Invalid value for environment variable '{name}': {exc}"
        ) from exc
