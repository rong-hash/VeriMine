from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .config import load_config
from .github_client import GitHubClient
from .pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardware repo crawler pipeline")
    parser.add_argument("--config", help="Path to JSON config", default=None)
    parser.add_argument(
        "--output",
        default="output/repo_cards.jsonl",
        help="Output JSONL path for accepted repos",
    )
    parser.add_argument(
        "--rejects",
        default="output/rejects.jsonl",
        help="Output JSONL path for rejected repos",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (defaults to GITHUB_TOKEN env)",
    )
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    config = load_config(args.config)
    env_token = os.environ.get("GITHUB_TOKEN")
    if args.token and env_token and args.token != env_token:
        logging.error("Token mismatch: --token differs from GITHUB_TOKEN")
        raise SystemExit(2)

    token = args.token or env_token
    if not token:
        logging.warning("GITHUB_TOKEN not set; rate limits will be low")

    client = GitHubClient(token=token)
    run_pipeline(client, config, Path(args.output), Path(args.rejects))


if __name__ == "__main__":
    main()
