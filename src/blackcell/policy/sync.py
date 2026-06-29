"""Subprocess environment boundaries."""

import os
from collections.abc import Mapping

SECRET_NAMES = frozenset({"LINEAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"})


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(source or os.environ)
    for name in SECRET_NAMES:
        environment.pop(name, None)
    return environment
