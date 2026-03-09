"""
PR Discovery for Verilog/SystemVerilog repositories.

Adapted from agent-task-craft/new_feature/pr_discovery.py.
Uses git log for PR enumeration + Claude Code for intelligent ranking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from verilog_diff_classifier import (
    classify_file,
    detect_build_tool,
    detect_test_framework,
    has_code_and_test_changes,
)

logger = logging.getLogger(__name__)


class VerilogPRDiscovery:
    """
    Discovers and ranks PRs suitable for Verilog task generation.

    Uses a two-phase approach:
    1. Git-based PR enumeration (fast, reliable)
    2. LLM-based ranking (Claude Code selects top-N candidates)
    """

    def __init__(
        self,
        github_token: Optional[str] = None,
        model: str = "",
        flash_model: str = "",
        timeout: int = 6000,
        task_type: str = "all",  # "new_feature", "bugfix", or "all"
    ):
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "")
        self.flash_model = flash_model or self.model
        self.timeout = timeout
        self.task_type = task_type

        # Load prompt template
        prompt_file = Path(__file__).parent / "config" / "prompts" / "discover_suitable_prs_verilog.txt"
        if prompt_file.exists():
            self.prompt_template = prompt_file.read_text(encoding="utf-8")
        else:
            self.prompt_template = self._default_prompt()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_repository(
        self,
        repo_name: str,
        max_prs: int = 50,
        top_n: int = 10,
        temp_dir: Optional[Path] = None,
        repo_path: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
        require_code_and_test: bool = True,
    ) -> Dict[str, Any]:
        """
        Analyze a repository and find suitable PRs for task generation.

        Args:
            repo_name: owner/repo format
            max_prs: Maximum PRs to fetch
            top_n: Number of top PRs to recommend
            temp_dir: Temporary directory for intermediate files
            repo_path: Path to cloned repo (if available)
            logger: Logger instance
            require_code_and_test: Only include PRs with both code and test changes

        Returns:
            Dict with 'recommended_prs', 'candidate_prs', 'analysis_summary'
        """
        log = logger or logging.getLogger(__name__)

        if temp_dir is None:
            temp_dir = Path("/tmp/pr_discovery") / repo_name.replace("/", "_")
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Phase 1: Get PR list
        log.info(f"Phase 1: Fetching PR list for {repo_name}")
        pr_list = self._get_pr_list(repo_name, repo_path, max_prs, log, require_code_and_test)

        if not pr_list:
            log.warning(f"No suitable PRs found for {repo_name}")
            return {"recommended_prs": [], "candidate_prs": [], "analysis_summary": "No PRs found"}

        log.info(f"Found {len(pr_list)} candidate PRs")

        # Phase 2: LLM-based ranking
        log.info(f"Phase 2: Using Claude Code to rank top {top_n} PRs")
        result = await self._analyze_with_llm(
            repo_name=repo_name,
            pr_list=pr_list,
            top_n=top_n,
            logger=log,
            temp_dir=temp_dir,
            repo_path=repo_path,
        )

        return result

    # ------------------------------------------------------------------
    # Phase 1: PR enumeration
    # ------------------------------------------------------------------

    def _get_pr_list(
        self,
        repo_name: str,
        repo_path: Optional[Path],
        max_prs: int,
        log: logging.Logger,
        require_code_and_test: bool = True,
    ) -> List[Dict]:
        """Get PR list using git log (primary) + GitHub API (fallback)."""
        prs = []

        # Try git-based enumeration first
        if repo_path and repo_path.exists():
            prs = self._get_prs_from_git(repo_path, max_prs * 2, log)

        # Supplement with GitHub API if needed
        if len(prs) < max_prs and self.github_token:
            api_prs = self._get_prs_from_api(repo_name, max_prs, log)
            existing_numbers = {pr["number"] for pr in prs}
            for pr in api_prs:
                if pr["number"] not in existing_numbers:
                    prs.append(pr)

        # Filter for code+test changes
        if require_code_and_test:
            filtered = []
            for pr in prs:
                files = pr.get("files", [])
                if isinstance(files, list) and len(files) > 0:
                    if isinstance(files[0], str):
                        classification = has_code_and_test_changes(files)
                    else:
                        classification = has_code_and_test_changes(
                            [f.get("filename", f.get("path", "")) for f in files]
                        )
                    if classification["has_both"]:
                        pr["code_files"] = classification["code_files"]
                        pr["test_files"] = classification["test_files"]
                        filtered.append(pr)
                else:
                    # If no file info, include anyway (will be filtered later)
                    filtered.append(pr)
            log.info(f"After code+test filter: {len(filtered)}/{len(prs)} PRs")
            prs = filtered

        # Size filter: max 80 files, max 8000 diff lines
        filtered = []
        for pr in prs:
            additions = pr.get("additions", 0)
            deletions = pr.get("deletions", 0)
            changed_files = pr.get("changed_files", 0)
            total_diff = additions + deletions

            if changed_files <= 80 and total_diff <= 8000:
                filtered.append(pr)
            else:
                log.debug(
                    f"PR #{pr['number']}: skipped (files={changed_files}, diff={total_diff})"
                )
        prs = filtered

        # Sort by total changes descending
        prs.sort(key=lambda p: p.get("additions", 0) + p.get("deletions", 0), reverse=True)

        return prs[:max_prs]

    def _get_prs_from_git(
        self, repo_path: Path, max_count: int, log: logging.Logger
    ) -> List[Dict]:
        """Extract merged PRs from git log."""
        prs = []
        try:
            result = subprocess.run(
                [
                    "git", "log", "--merges", "--oneline",
                    f"--max-count={max_count}",
                    "--format=%H|%s|%aI",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log.warning(f"git log failed: {result.stderr}")
                return []

            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                sha, subject, date = parts

                # Extract PR number from merge commit message
                pr_match = re.search(r"#(\d+)", subject)
                if not pr_match:
                    continue

                pr_number = int(pr_match.group(1))

                # Get changed files for this merge
                files = self._get_merge_files(repo_path, sha, log)

                prs.append({
                    "number": pr_number,
                    "title": subject,
                    "merge_commit": sha[:8],
                    "merged_at": date,
                    "files": files,
                    "additions": 0,  # Will be filled later if needed
                    "deletions": 0,
                    "changed_files": len(files),
                    "labels": [],
                    "body": "",
                    "url": "",
                })

            log.info(f"Found {len(prs)} PRs from git log")
        except Exception as e:
            log.error(f"Failed to get PRs from git: {e}")

        return prs

    def _get_merge_files(
        self, repo_path: Path, merge_sha: str, log: logging.Logger
    ) -> List[str]:
        """Get list of files changed in a merge commit."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{merge_sha}^1..{merge_sha}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return [f for f in result.stdout.strip().split("\n") if f.strip()]
        except Exception:
            pass
        return []

    def _get_prs_from_api(
        self, repo_name: str, max_prs: int, log: logging.Logger
    ) -> List[Dict]:
        """Fetch PRs from GitHub REST API."""
        try:
            from github import Github

            g = Github(self.github_token)
            repo = g.get_repo(repo_name)

            prs = []
            for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
                if not pr.merged_at or not pr.merge_commit_sha:
                    continue

                files = [f.filename for f in pr.get_files()]
                labels = [l.name for l in pr.labels]

                prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "body": (pr.body or "")[:500],
                    "url": pr.html_url,
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                    "additions": pr.additions,
                    "deletions": pr.deletions,
                    "changed_files": pr.changed_files,
                    "labels": labels,
                    "files": files,
                    "base_commit": pr.base.sha[:8],
                    "merge_commit": pr.merge_commit_sha[:8],
                })

                if len(prs) >= max_prs:
                    break

            log.info(f"Fetched {len(prs)} PRs from GitHub API")
            return prs

        except Exception as e:
            log.error(f"GitHub API fetch failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Phase 2: LLM ranking
    # ------------------------------------------------------------------

    async def _analyze_with_llm(
        self,
        repo_name: str,
        pr_list: List[Dict],
        top_n: int,
        logger: logging.Logger,
        temp_dir: Path,
        repo_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Use Claude Code to rank PRs."""
        from agents.claude_code_executor import ClaudeCodeExecutor

        # Format PR list
        pr_text = self._format_pr_list(pr_list)

        # Build prompt
        prompt = self.prompt_template.format(
            repo_name=repo_name,
            pr_list=pr_text,
            top_n=top_n,
            task_type=self.task_type,
        )

        # Set up executor
        work_dir = repo_path if repo_path and repo_path.exists() else temp_dir
        output_dir = temp_dir / "pr_discovery_output"
        output_dir.mkdir(parents=True, exist_ok=True)

        executor = ClaudeCodeExecutor(
            work_dir=work_dir,
            output_dir=output_dir,
            model=self.model,
            task_name="pr_discovery",
        )

        try:
            result_data = await executor.execute_with_json_retry(
                query=prompt,
                continue_conversation=False,
                timeout=self.timeout,
                max_retries=10,
                must_include_keys=["recommended_prs"],
            )

            if result_data:
                recommended_prs = result_data.get("recommended_prs", [])

                # Merge file classification info
                pr_info_map = {pr["number"]: pr for pr in pr_list}
                for rec_pr in recommended_prs:
                    pr_num = rec_pr.get("pr_number")
                    if pr_num and pr_num in pr_info_map:
                        original = pr_info_map[pr_num]
                        rec_pr["test_files"] = original.get("test_files", [])
                        rec_pr["code_files"] = original.get("code_files", [])

                logger.info(f"LLM recommended {len(recommended_prs)} PRs")
                for i, pr in enumerate(recommended_prs[:top_n], 1):
                    logger.info(
                        f"  Top {i}: PR #{pr.get('pr_number', '?')}: "
                        f"{pr.get('pr_title', '')} (score: {pr.get('overall_score', 'N/A')})"
                    )

                # Save result
                with open(output_dir / "pr_discovery_result.json", "w") as f:
                    json.dump(result_data, f, indent=2, ensure_ascii=False)

                return result_data

            # Fallback: sort by change size
            logger.warning("LLM analysis failed, using size-based fallback")
            return self._fallback_ranking(pr_list, top_n, logger)

        except Exception as e:
            logger.error(f"LLM analysis exception: {e}")
            return self._fallback_ranking(pr_list, top_n, logger)
        finally:
            if hasattr(executor, "client") and hasattr(executor.client, "disconnect"):
                try:
                    await executor.client.disconnect()
                except Exception:
                    pass

    def _fallback_ranking(
        self, pr_list: List[Dict], top_n: int, log: logging.Logger
    ) -> Dict:
        """Fallback ranking by change size."""
        recommended = []
        for i, pr in enumerate(pr_list[:top_n], 1):
            log.info(f"Fallback Top {i}: PR #{pr['number']}: {pr['title']}")
            recommended.append({
                "pr_number": pr["number"],
                "pr_title": pr["title"],
                "overall_score": 7.0 - i * 0.1,
                "feature_type": "Unknown (fallback)",
                "reasoning": "LLM analysis failed; ranked by change size",
                "test_files": pr.get("test_files", []),
                "code_files": pr.get("code_files", []),
            })

        return {
            "recommended_prs": recommended,
            "candidate_prs": [{"pr_number": p["number"], "pr_title": p["title"]} for p in pr_list],
            "analysis_summary": f"Fallback: top {len(recommended)} by size",
            "fallback_used": True,
        }

    def _format_pr_list(self, pr_list: List[Dict]) -> str:
        """Format PR list as markdown text for LLM consumption."""
        lines = []
        for pr in pr_list:
            lines.append(f"### PR #{pr['number']}: {pr['title']}")
            if pr.get("url"):
                lines.append(f"- **URL**: {pr['url']}")
            lines.append(f"- **Merged**: {pr.get('merged_at', 'unknown')}")
            lines.append(
                f"- **Changes**: +{pr.get('additions', '?')} "
                f"-{pr.get('deletions', '?')} "
                f"({pr.get('changed_files', '?')} files)"
            )
            if pr.get("labels"):
                lines.append(f"- **Labels**: {', '.join(pr['labels'])}")

            body = (pr.get("body") or "No description")[:500]
            lines.append(f"- **Description**: {body}")

            if pr.get("files"):
                files = pr["files"][:10]
                if isinstance(files[0], str):
                    files_str = ", ".join(files)
                else:
                    files_str = ", ".join(f.get("filename", "") for f in files)
                if len(pr["files"]) > 10:
                    files_str += f" ... and {len(pr['files']) - 10} more"
                lines.append(f"- **Files**: {files_str}")

            lines.append("")

        return "\n".join(lines)

    def _default_prompt(self) -> str:
        """Default prompt if template file is missing."""
        return """You are analyzing Pull Requests from a Verilog/SystemVerilog hardware design repository: {repo_name}

Below is a list of merged PRs. Your task is to select the **top {top_n}** PRs that are most suitable for generating coding tasks.

**Task type**: {task_type}
- If task_type = "new_feature": look for PRs that add new RTL modules/interfaces/features
- If task_type = "bugfix": look for PRs that fix RTL logic errors, timing issues, protocol violations
- If task_type = "all": look for both types

**Selection criteria:**
1. PR must contain both **RTL code changes** (.v, .sv, .svh files) AND **test/testbench changes** (testbenches, cocotb tests, verification files)
2. PR should implement a **new feature** or **fix a bug** (depending on task_type)
3. The change should be **self-contained** and understandable from the diff
4. Avoid PRs that only modify build scripts, documentation, or configuration
5. Avoid PRs with proprietary EDA tool dependencies (VCS, Questa, Xcelium)
6. Prefer PRs where tests validate the functionality (not just infrastructure tests)

**PR List:**
{pr_list}

**Output:** Return a JSON object with this structure:
```json
{{
  "recommended_prs": [
    {{
      "pr_number": 123,
      "pr_title": "Add FIFO module with testbench",
      "overall_score": 8.5,
      "feature_type": "New RTL module",
      "reasoning": "Self-contained FIFO implementation with comprehensive testbench"
    }}
  ],
  "analysis_summary": "Found N suitable PRs with RTL + test changes"
}}
```
"""


def main():
    """CLI entry point for testing."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Verilog PR Discovery")
    parser.add_argument("repo", help="Repository name (owner/repo)")
    parser.add_argument("--max-prs", type=int, default=50)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
    log = logging.getLogger(__name__)

    discovery = VerilogPRDiscovery()
    result = asyncio.run(
        discovery.analyze_repository(
            repo_name=args.repo,
            max_prs=args.max_prs,
            top_n=args.top_n,
            logger=log,
        )
    )

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.info(f"Results saved to {args.output}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
