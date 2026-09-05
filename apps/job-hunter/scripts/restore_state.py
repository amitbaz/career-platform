from __future__ import annotations

import argparse
import logging
from pathlib import Path

import requests

from job_hunter.github_state import load_latest_artifact_to_path

logger = logging.getLogger(__name__)


def restore_state(repo: str, token: str, name: str, dest: Path, http=None) -> bool:
    http = http or requests
    artifact = load_latest_artifact_to_path(
        repo,
        token,
        name,
        Path(dest),
        http=http,
    )
    if artifact is None:
        logger.info("No prior %r artifact found for %s; starting with fresh state.", name, repo)
        return False

    logger.info(
        "Restored %s from artifact %r (id=%s)",
        dest,
        name,
        artifact["id"],
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="restore_state")
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form")
    parser.add_argument("--token", required=True, help="GitHub token with actions:read access")
    parser.add_argument("--name", default="job-hunter-state", help="Artifact name to restore")
    parser.add_argument("--dest", default="var/job_hunter.sqlite3", help="Destination path for the SQLite file")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    restore_state(repo=args.repo, token=args.token, name=args.name, dest=Path(args.dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
