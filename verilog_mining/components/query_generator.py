"""
Query Generator for Verilog task mining.

Phase 2: Generate user query from git diff (base..target)
Phase 3: Quality check with iterative improvement (max 3 iterations)

Adapted from agent-task-craft/new_feature/components/query_generator.py
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agents.claude_code_executor import ClaudeCodeExecutor

logger = logging.getLogger(__name__)


class QueryGenerator:
    """Generates and validates task queries from PR diffs."""

    def __init__(
        self,
        model: str = "",
        flash_model: str = "",
        quality_threshold: float = 6.5,
        task_type: str = "all",
    ):
        self.model = model
        self.flash_model = flash_model or model
        self.quality_threshold = quality_threshold
        self.task_type = task_type

        # Load prompt templates
        prompts_dir = Path(__file__).parent.parent / "config" / "prompts"
        self.generate_prompt = self._load_prompt(prompts_dir / "generate_query_verilog.txt")
        self.quality_prompt = self._load_prompt(prompts_dir / "quality_evaluation_verilog.txt")
        self.improve_prompt = self._load_prompt(prompts_dir / "improve_query_verilog.txt")

    def _load_prompt(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning(f"Prompt template not found: {path}")
        return ""

    # ------------------------------------------------------------------
    # Phase 2: Query Generation
    # ------------------------------------------------------------------

    async def generate_query(
        self,
        repo_path: Path,
        base_commit: str,
        target_commit: str,
        task_dir: Path,
        log: logging.Logger,
    ) -> Optional[str]:
        """
        Generate a user query from the diff between base and target commits.

        Args:
            repo_path: Path to cloned repository
            base_commit: Base commit SHA
            target_commit: Target commit SHA
            task_dir: Task output directory
            log: Logger

        Returns:
            Generated query string, or None on failure
        """
        gen_dir = task_dir / "generate_query"
        gen_dir.mkdir(parents=True, exist_ok=True)

        # Ensure commits exist
        self._ensure_commit_exists(repo_path, base_commit, log)
        self._ensure_commit_exists(repo_path, target_commit, log)

        # Get diff
        diff = self._get_diff(repo_path, base_commit, target_commit, log)
        if not diff:
            log.error("Failed to get diff between commits")
            return None

        # Checkout base commit
        self._checkout(repo_path, base_commit, log)

        # Build prompt
        prompt = self.generate_prompt.format(
            repo_path=repo_path,
            base_commit=base_commit[:8],
            target_commit=target_commit[:8],
            diff=diff[:15000],  # Limit diff size
            task_type=self.task_type,
        )

        # Save prompt
        (gen_dir / "prompt.md").write_text(prompt, encoding="utf-8")

        # Execute with Claude Code
        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=gen_dir,
            model=self.model,
            task_name="generate_query",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt,
                continue_conversation=False,
                timeout=6000,
                max_retries=10,
                must_include_keys=["query"],
            )

            if result and "query" in result:
                query = result["query"]
                (gen_dir / "generated_query.txt").write_text(query, encoding="utf-8")
                log.info(f"Generated query ({len(query)} chars)")
                return query
            else:
                log.error("Failed to generate query: no 'query' key in response")
                return None

        except Exception as e:
            log.error(f"Query generation failed: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Phase 3: Quality Check (iterative improvement)
    # ------------------------------------------------------------------

    async def quality_check(
        self,
        query: str,
        repo_path: Path,
        base_commit: str,
        target_commit: str,
        task_dir: Path,
        log: logging.Logger,
        max_iterations: int = 3,
    ) -> Tuple[str, float]:
        """
        Iteratively improve query quality.

        Args:
            query: Initial query
            repo_path: Repository path
            base_commit: Base commit
            target_commit: Target commit
            task_dir: Output directory
            log: Logger
            max_iterations: Max improvement iterations

        Returns:
            Tuple of (final_query, final_score)
        """
        qc_dir = task_dir / "quality_check"
        qc_dir.mkdir(parents=True, exist_ok=True)

        iteration_history = []
        current_query = query
        best_score = 0.0
        best_query = query

        for iteration in range(max_iterations):
            log.info(f"Quality check iteration {iteration + 1}/{max_iterations}")

            # Evaluate
            eval_result = await self._evaluate_query(
                current_query, repo_path, base_commit, target_commit, qc_dir, log
            )

            if not eval_result:
                log.warning("Quality evaluation failed, keeping current query")
                break

            score = eval_result.get("overall_score", 0)
            feedback = eval_result.get("feedback", "")
            dimensions = eval_result.get("dimensions", {})

            iteration_history.append({
                "iteration": iteration + 1,
                "query": current_query,
                "score": score,
                "feedback": feedback,
                "dimensions": dimensions,
            })

            log.info(f"  Score: {score:.1f}/10")

            if score > best_score:
                best_score = score
                best_query = current_query

            # Check if quality threshold met
            if score >= self.quality_threshold:
                log.info(f"Quality threshold met ({score:.1f} >= {self.quality_threshold})")
                break

            # Improve if below threshold and not last iteration
            if iteration < max_iterations - 1:
                log.info(f"  Score below threshold, improving...")
                improved = await self._improve_query(
                    current_query, feedback, repo_path, base_commit,
                    target_commit, qc_dir, log
                )
                if improved:
                    current_query = improved
                else:
                    log.warning("  Query improvement failed, stopping iterations")
                    break

        # Save iteration history
        history_file = qc_dir / "quality_evaluation.json"
        with open(history_file, "w") as f:
            json.dump({
                "iterations": iteration_history,
                "final_score": best_score,
                "final_query": best_query,
                "threshold": self.quality_threshold,
            }, f, indent=2, ensure_ascii=False)

        log.info(f"Quality check complete: score={best_score:.1f}")
        return best_query, best_score

    async def _evaluate_query(
        self,
        query: str,
        repo_path: Path,
        base_commit: str,
        target_commit: str,
        output_dir: Path,
        log: logging.Logger,
    ) -> Optional[Dict]:
        """Evaluate query quality using Claude Code."""
        diff = self._get_diff(repo_path, base_commit, target_commit, log)

        prompt = self.quality_prompt.format(
            user_query=query,
            repo=repo_path,
            commit=f"{base_commit[:8]}..{target_commit[:8]}",
        )

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=output_dir,
            model=self.flash_model,
            task_name="quality_eval",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt,
                timeout=3000,
                max_retries=5,
                must_include_keys=["overall_score"],
            )
            if result:
                # Save evaluation
                (output_dir / "quality_eval.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            return result
        except Exception as e:
            log.error(f"Quality evaluation failed: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    async def _improve_query(
        self,
        query: str,
        feedback: str,
        repo_path: Path,
        base_commit: str,
        target_commit: str,
        output_dir: Path,
        log: logging.Logger,
    ) -> Optional[str]:
        """Improve query based on feedback."""
        prompt = self.improve_prompt.format(
            user_query=query,
            evaluation_feedback=feedback,
            repo_path=repo_path,
            base_commit=base_commit[:8],
            target_commit=target_commit[:8],
        )

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=output_dir,
            model=self.model,
            task_name="improve_query",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt,
                timeout=3000,
                max_retries=5,
                must_include_keys=["query"],
            )
            if result and "query" in result:
                return result["query"]
            return None
        except Exception as e:
            log.error(f"Query improvement failed: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _ensure_commit_exists(self, repo_path: Path, sha: str, log: logging.Logger):
        """Fetch commit from remote if not available locally."""
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.info(f"Commit {sha[:8]} not found locally, fetching...")
            subprocess.run(
                ["git", "fetch", "origin", sha],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )

    def _checkout(self, repo_path: Path, sha: str, log: logging.Logger):
        """Checkout a specific commit."""
        subprocess.run(
            ["git", "checkout", sha, "--force"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            ["git", "clean", "-fdx", "-e", ".venv", "-e", "test_cc"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )

    def _get_diff(
        self, repo_path: Path, base: str, target: str, log: logging.Logger
    ) -> Optional[str]:
        """Get diff between two commits."""
        try:
            result = subprocess.run(
                ["git", "diff", base, target],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return result.stdout
            log.error(f"git diff failed: {result.stderr}")
        except Exception as e:
            log.error(f"git diff exception: {e}")
        return None
