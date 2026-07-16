from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import pytest

SECURE_TEST_UMASK = 0o022


def main(argv: Sequence[str] | None = None) -> int:
    """Run pytest under the repository's owner-write security boundary."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    previous_umask = os.umask(SECURE_TEST_UMASK)
    try:
        return int(pytest.main(arguments))
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
