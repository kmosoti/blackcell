from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackcell.control_plane.capabilities import (
    refresh_github_capabilities,
    write_github_capabilities,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh cached GitHub GraphQL capability metadata."
    )
    parser.add_argument("--output", type=Path, help="Manifest path to write.")
    args = parser.parse_args()

    manifest = refresh_github_capabilities()
    path = write_github_capabilities(manifest, path=args.output)
    print(json.dumps({"path": str(path), "manifest": manifest.to_mapping()}, sort_keys=True))


if __name__ == "__main__":
    main()
