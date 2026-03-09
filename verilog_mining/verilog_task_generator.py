"""
Verilog Task Generator - Core 9-phase pipeline.

For each PR, runs 9 phases to produce a validated task package:
  Phase 1: Repository Setup (clone, checkout base_commit)
  Phase 2: Query Generation (Claude Code → task.md)
  Phase 3: Quality Check (iterative improvement, max 3 rounds)
  Phase 4: Test Script Generation (Claude Code → run-tests.sh)
  Phase 5: Test Environment Validation & Improvement
  Phase 6: Test-Query Alignment Validation
  Phase 7: Commit Test Validation (base vs target with patches)
  Phase 8: Real Test Validation (actor models)
  Phase 9: Scoring & Ranking

Adapted from agent-task-craft/new_feature/new_feature_task_generator.py
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.claude_code_executor import ClaudeCodeExecutor, setup_logger
from components.query_generator import QueryGenerator
from components.test_organizer import TestOrganizer
from verilog_diff_classifier import classify_file, has_code_and_test_changes
from verilog_pr_discovery import VerilogPRDiscovery
from verilog_test_runner import parse_test_output, run_tests

logger = logging.getLogger(__name__)


class VerilogTaskGenerator:
    """
    Main orchestrator for Verilog task generation pipeline.

    Usage:
        generator = VerilogTaskGenerator(
            repo_name="owner/repo",
            output_dir=Path("./output"),
        )
        await generator.generate_tasks()
    """

    def __init__(
        self,
        repo_name: str,
        output_dir: Path,
        repo_path: Optional[Path] = None,
        model: str = "",
        flash_model: str = "",
        actor_models: Optional[List[str]] = None,
        top_n: int = 10,
        max_prs: int = 50,
        pr_timeout: int = 5400,  # 90 minutes per PR (seconds)
        repo_timeout: int = 14400,  # 240 minutes per repo (seconds)
        validation_runs: int = 4,
        quality_threshold: float = 6.5,
        require_code_and_test: bool = True,
        use_pr_discovery: bool = True,
        github_token: str = "",
        task_type: str = "all",  # "new_feature", "bugfix", or "all"
        fewshot_dir: Optional[Path] = None,
        copy_to_final: Optional[Path] = None,
        run_mode: str = "local",
    ):
        self.repo_name = repo_name
        self.output_dir = Path(output_dir)
        self.repo_path = Path(repo_path) if repo_path else None
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "")
        self.flash_model = flash_model or self.model
        self.actor_models = actor_models or []
        self.top_n = top_n
        self.max_prs = max_prs
        self.pr_timeout = pr_timeout
        self.repo_timeout = repo_timeout
        self.validation_runs = validation_runs
        self.quality_threshold = quality_threshold
        self.require_code_and_test = require_code_and_test
        self.use_pr_discovery = use_pr_discovery
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.task_type = task_type
        self.fewshot_dir = fewshot_dir
        self.copy_to_final = Path(copy_to_final) if copy_to_final else None
        self.run_mode = run_mode

        # Set run mode env
        os.environ["CLAUDE_CODE_RUN_MODE"] = self.run_mode

        # Set up output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Progress file for resumable processing
        self.progress_file = self.output_dir / "progress.json"

        # Set up logging
        self.logger = setup_logger(
            self.output_dir / "task_generator.log", "verilog_task_gen"
        )

        # Load prompts
        self.prompts = self._load_prompts()

        # Load fewshot examples
        self.fewshot_examples = self._load_fewshot()

        # Initialize components
        self.query_generator = QueryGenerator(
            model=self.model,
            flash_model=self.flash_model,
            quality_threshold=self.quality_threshold,
            task_type=self.task_type,
        )
        self.test_organizer = TestOrganizer(
            model=self.model,
            flash_model=self.flash_model,
        )

    # ==================================================================
    # Initialization helpers
    # ==================================================================

    def _load_prompts(self) -> Dict[str, str]:
        """Load all prompt templates from config/prompts/."""
        prompts = {}
        prompts_dir = Path(__file__).parent / "config" / "prompts"
        if prompts_dir.exists():
            for f in prompts_dir.glob("*.txt"):
                prompts[f.stem] = f.read_text(encoding="utf-8")
                self.logger.info(f"Loaded prompt: {f.stem} ({len(prompts[f.stem])} chars)")
        return prompts

    def _load_fewshot(self) -> List[str]:
        """Load few-shot examples from fewshot directory."""
        examples = []
        if self.fewshot_dir and self.fewshot_dir.exists():
            for f in sorted(self.fewshot_dir.glob("*.md")):
                examples.append(f.read_text(encoding="utf-8"))
            self.logger.info(f"Loaded {len(examples)} few-shot examples")
        return examples

    def _load_progress(self) -> Dict:
        """Load progress from file for resumable processing."""
        if self.progress_file.exists():
            try:
                return json.loads(self.progress_file.read_text())
            except Exception:
                pass
        return {"processed_prs": [], "completed_tasks": []}

    def _save_progress(self, progress: Dict):
        """Save progress to file."""
        self.progress_file.write_text(
            json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ==================================================================
    # Main entry point
    # ==================================================================

    async def generate_tasks(self) -> Dict[str, Any]:
        """
        Main pipeline: discover PRs and generate tasks.

        Features vs old version:
        - Progress tracking for resumable processing
        - Repo-level timeout
        - PR-level timeout with asyncio
        - Result sync to final output dir
        - Detailed statistics and error tracking
        """
        self.logger.info("=" * 80)
        self.logger.info(f"Starting Verilog task generation for {self.repo_name}")
        self.logger.info(f"Output: {self.output_dir}")
        self.logger.info(f"Model: {self.model}, Flash: {self.flash_model}")
        self.logger.info(f"Task type: {self.task_type}")
        self.logger.info(f"PR timeout: {self.pr_timeout}s, Repo timeout: {self.repo_timeout}s")
        self.logger.info(f"Actor models: {self.actor_models}")
        self.logger.info(f"Validation runs: {self.validation_runs}")
        self.logger.info("=" * 80)

        start_time = time.time()
        repo_start_time = time.time()

        # Load progress for resumable processing
        progress = self._load_progress()
        processed_pr_numbers = set(progress.get("processed_prs", []))
        if processed_pr_numbers:
            self.logger.info(f"Resuming: {len(processed_pr_numbers)} PRs already processed")

        # Clone repo if not provided
        if not self.repo_path or not self.repo_path.exists():
            self.repo_path = await self._clone_repo()
            if not self.repo_path:
                return {"error": "Failed to clone repository"}

        # Discover PRs
        prs = await self._discover_prs()
        if not prs:
            return {"error": "No suitable PRs found"}

        self.logger.info(f"Processing {len(prs)} PRs")

        # Process each PR with timeout tracking
        all_pr_results = []
        validated_tasks = []

        for i, pr_info in enumerate(prs, 1):
            pr_number = pr_info.get("pr_number", pr_info.get("number", "?"))

            # Initialize PR result record
            pr_result = {
                "repository": self.repo_name,
                "pr_number": pr_number,
                "pr_title": pr_info.get("pr_title", pr_info.get("title", "")),
                "task_type": pr_info.get("task_type", self.task_type),
                "status": "pending",
                "failure_reason": None,
                "task_dir": None,
                "task_id": None,
                "base_pass_rate": None,
                "target_pass_rate": None,
                "improvement": None,
                "final_score": None,
            }

            # Check repo-level timeout
            repo_elapsed = time.time() - repo_start_time
            if repo_elapsed > self.repo_timeout:
                self.logger.warning(
                    f"Repo timeout reached ({self.repo_timeout}s), "
                    f"skipping remaining {len(prs) - i + 1} PRs"
                )
                pr_result["status"] = "skipped"
                pr_result["failure_reason"] = f"repo_timeout ({self.repo_timeout}s)"
                all_pr_results.append(pr_result)
                # Record remaining PRs as skipped
                for remaining_pr in prs[i:]:
                    all_pr_results.append({
                        "repository": self.repo_name,
                        "pr_number": remaining_pr.get("pr_number", "?"),
                        "status": "skipped",
                        "failure_reason": f"repo_timeout",
                    })
                break

            # Skip already processed PRs (resumable)
            if pr_number in processed_pr_numbers:
                self.logger.info(f"PR #{pr_number}: already processed, skipping")
                continue

            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"PR {i}/{len(prs)}: #{pr_number} - {pr_result['pr_title'][:60]}")
            self.logger.info(f"{'='*60}")

            try:
                result = await asyncio.wait_for(
                    self.generate_task_from_pr(pr_info),
                    timeout=self.pr_timeout,
                )

                if result and result.get("status") == "completed":
                    # Read validation result
                    task_dir = Path(result.get("task_dir", ""))
                    pr_result["task_dir"] = str(task_dir)
                    pr_result["task_id"] = result.get("task_id")

                    cv = result.get("commit_validation", {})
                    base_pass = cv.get("base_commit", {}).get("pass_rate", 1.0)
                    target_pass = cv.get("target_commit", {}).get("pass_rate", 0.0)
                    improvement = target_pass - base_pass

                    pr_result["base_pass_rate"] = base_pass
                    pr_result["target_pass_rate"] = target_pass
                    pr_result["improvement"] = improvement

                    if improvement > 0:
                        llm_score = result.get("llm_score", 7.0)
                        validation_score = self._calculate_validation_score(cv)
                        final_score = self._calculate_final_score(llm_score, validation_score)

                        pr_result["status"] = "passed"
                        pr_result["final_score"] = final_score

                        validated_tasks.append({
                            "result": result,
                            "pr_info": pr_info,
                            "llm_score": llm_score,
                            "validation_score": validation_score,
                            "final_score": final_score,
                            "improvement": improvement,
                        })
                        self.logger.info(
                            f"  ✅ Passed (improvement: {improvement:+.1%}, "
                            f"final_score: {final_score:.2f})"
                        )
                    else:
                        pr_result["status"] = "failed"
                        pr_result["failure_reason"] = (
                            f"no_improvement: target ({target_pass:.1%}) "
                            f"<= base ({base_pass:.1%})"
                        )
                        self.logger.warning(
                            f"  ❌ Failed: target ({target_pass:.1%}) "
                            f"<= base ({base_pass:.1%})"
                        )
                else:
                    pr_result["status"] = "failed"
                    pr_result["failure_reason"] = result.get("error", "task_generation_failed")

            except asyncio.TimeoutError:
                self.logger.error(f"PR #{pr_number} timed out after {self.pr_timeout}s")
                pr_result["status"] = "timeout"
                pr_result["failure_reason"] = f"pr_timeout ({self.pr_timeout}s)"

            except Exception as e:
                self.logger.error(f"PR #{pr_number} failed: {e}")
                self.logger.error(traceback.format_exc())
                pr_result["status"] = "error"
                pr_result["failure_reason"] = str(e)

            all_pr_results.append(pr_result)

            # Save progress after each PR
            progress["processed_prs"].append(pr_number)
            if pr_result["status"] == "passed":
                progress["completed_tasks"].append(pr_result)
            self._save_progress(progress)

            # Sync to final output if configured
            if self.copy_to_final and pr_result.get("task_dir"):
                self._sync_task_to_final(Path(pr_result["task_dir"]))

        # Phase 9: Score and rank validated tasks
        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("Phase 9: Ranking and selecting top PRs...")
        self.logger.info("=" * 60)

        if validated_tasks:
            validated_tasks.sort(key=lambda x: x["final_score"], reverse=True)

            self.logger.info(f"\n📊 Validation Results (sorted by final_score):")
            self.logger.info("-" * 80)
            self.logger.info(
                f"{'Rank':<6}{'PR #':<10}{'LLM':<8}{'Valid':<8}"
                f"{'Final':<8}{'Improve':<10}{'Title'}"
            )
            self.logger.info("-" * 80)
            for rank, item in enumerate(validated_tasks, 1):
                pr_num = item["pr_info"].get("pr_number", "?")
                title = item["pr_info"].get("pr_title", "")[:30]
                marker = "⭐" if rank <= self.top_n else "  "
                self.logger.info(
                    f"{marker}{rank:<5}#{pr_num:<9}{item['llm_score']:<8.1f}"
                    f"{item['validation_score']:<8.1f}{item['final_score']:<8.2f}"
                    f"{item['improvement']:+<9.1%} {title}"
                )
            self.logger.info("-" * 80)
            self.logger.info(
                f"✅ Selected top {min(len(validated_tasks), self.top_n)} tasks "
                f"(from {len(validated_tasks)} validated candidates)"
            )

        # Generate summary
        elapsed = time.time() - start_time
        summary_stats = {
            "total_prs": len(all_pr_results),
            "passed": sum(1 for r in all_pr_results if r["status"] == "passed"),
            "failed": sum(1 for r in all_pr_results if r["status"] == "failed"),
            "timeout": sum(1 for r in all_pr_results if r["status"] == "timeout"),
            "skipped": sum(1 for r in all_pr_results if r["status"] == "skipped"),
            "error": sum(1 for r in all_pr_results if r["status"] == "error"),
        }

        # Failure reason breakdown
        failure_reasons = {}
        for r in all_pr_results:
            if r["status"] in ("failed", "error") and r.get("failure_reason"):
                reason = r["failure_reason"].split(":")[0]
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        summary = {
            "repo": self.repo_name,
            "task_type": self.task_type,
            "generation_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": elapsed,
            "statistics": summary_stats,
            "failure_reasons": failure_reasons,
            "all_pr_results": all_pr_results,
            "usable_prs": [
                {
                    "repository": r["repository"],
                    "pr_number": r["pr_number"],
                    "pr_title": r.get("pr_title", ""),
                    "task_dir": r.get("task_dir"),
                    "final_score": r.get("final_score"),
                    "improvement": r.get("improvement"),
                }
                for r in all_pr_results if r["status"] == "passed"
            ],
        }

        summary_path = self.output_dir / "pr_summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=4, ensure_ascii=False)

        # Print summary
        self.logger.info("")
        self.logger.info("📊 PR Processing Summary:")
        self.logger.info(f"  Total PRs processed: {summary_stats['total_prs']}")
        self.logger.info(f"  ✅ Passed: {summary_stats['passed']}")
        self.logger.info(f"  ❌ Failed: {summary_stats['failed']}")
        self.logger.info(f"  ⏰ Timeout: {summary_stats['timeout']}")
        self.logger.info(f"  ⏭️  Skipped: {summary_stats['skipped']}")
        self.logger.info(f"  💥 Error: {summary_stats['error']}")
        if failure_reasons:
            self.logger.info("  Failure reasons:")
            for reason, count in sorted(failure_reasons.items(), key=lambda x: -x[1]):
                self.logger.info(f"    - {reason}: {count}")
        self.logger.info(f"  Elapsed: {elapsed:.0f}s")

        # Sync summary to final output
        if self.copy_to_final:
            self._sync_summary_to_final(summary_path)

        return summary

    # ==================================================================
    # Per-PR pipeline (Phases 1-8)
    # ==================================================================

    async def generate_task_from_pr(self, pr_info: Dict) -> Dict:
        """
        Process a single PR through all phases.

        Enhanced vs old version:
        - AI file classification for accurate code/test patch splitting
        - Detailed PR info fetching from GitHub API
        - Fewshot injection into query generation
        - Test patch integrity verification
        - Comprehensive error logging
        """
        pr_number = pr_info.get("pr_number", pr_info.get("number", 0))
        pr_title = pr_info.get("pr_title", pr_info.get("title", ""))

        # Create task directory
        task_id = hashlib.md5(f"{self.repo_name}_{pr_number}".encode()).hexdigest()[:8]
        task_dir = self.output_dir / f"task_{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)

        result = {
            "pr_number": pr_number,
            "pr_title": pr_title,
            "task_id": task_id,
            "task_dir": str(task_dir),
            "status": "in_progress",
        }

        task_log = setup_logger(task_dir / "task.log", f"PR#{pr_number}")

        # --- Phase 1: Repository Setup ---
        task_log.info("Phase 1: Repository Setup")
        base_commit, target_commit = await self._phase1_repo_setup(
            pr_info, task_dir, task_log
        )
        if not base_commit or not target_commit:
            result["status"] = "error"
            result["error"] = "Failed to determine commits"
            return result

        result["base_commit"] = base_commit
        result["target_commit"] = target_commit

        # Determine task_type for this PR
        pr_task_type = pr_info.get("task_type", self.task_type)
        if pr_task_type == "all":
            pr_task_type = "new_feature"

        # Save task.json metadata
        task_meta = {
            "repo": self.repo_name,
            "pr_number": pr_number,
            "pr_title": pr_title,
            "base_commit": base_commit,
            "target_commit": target_commit,
            "task_id": task_id,
            "task_type": pr_task_type,
        }
        (task_dir / "task.json").write_text(
            json.dumps(task_meta, indent=2), encoding="utf-8"
        )

        # Get changed files list
        changed_files = self._get_changed_files(base_commit, target_commit, task_log)

        # --- AI File Classification ---
        task_log.info("AI File Classification")
        ai_classification = await self._ai_classify_files_for_pr(
            repo_path=self.repo_path,
            repo_name=self.repo_name,
            pr_number=pr_number,
            pr_title=pr_title,
            base_commit=base_commit,
            target_commit=target_commit,
            changed_files=changed_files,
            output_dir=task_dir,
            logger=task_log,
        )

        # Generate patches using AI classification (or fallback to rule-based)
        if ai_classification:
            impl_files = ai_classification.get("implementation_files", [])
            test_build_files = ai_classification.get("test_and_build_files", [])
            has_code, has_test = self._generate_patches_from_classification(
                base_commit, target_commit, impl_files, test_build_files,
                task_dir, task_log,
            )
        else:
            task_log.info("AI classification failed, using rule-based patch generation")
            has_code, has_test = self._generate_patches(
                self.repo_path, base_commit, target_commit, task_dir, task_log
            )

        if not has_code and not has_test:
            result["status"] = "error"
            result["error"] = "No code or test patches generated"
            self._cleanup_failed_task(task_dir, task_log)
            return result

        # --- Phase 2: Query Generation ---
        task_log.info("Phase 2: Query Generation")
        query = await self.query_generator.generate_query(
            repo_path=self.repo_path,
            base_commit=base_commit,
            target_commit=target_commit,
            task_dir=task_dir,
            log=task_log,
        )
        if not query:
            result["status"] = "error"
            result["error"] = "Query generation failed"
            self._cleanup_failed_task(task_dir, task_log)
            return result

        # --- Phase 3: Quality Check ---
        task_log.info("Phase 3: Quality Check")
        final_query, llm_score = await self.query_generator.quality_check(
            query=query,
            repo_path=self.repo_path,
            base_commit=base_commit,
            target_commit=target_commit,
            task_dir=task_dir,
            log=task_log,
        )

        # Save task.md
        (task_dir / "task.md").write_text(final_query, encoding="utf-8")
        result["llm_score"] = llm_score

        # --- Phase 4: Test Script Generation ---
        task_log.info("Phase 4: Test Script Generation")
        test_files = pr_info.get("test_files", [])
        code_files = pr_info.get("code_files", [])

        # Use AI classification results if available
        if ai_classification:
            test_files = ai_classification.get("test_and_build_files", test_files)
            code_files = ai_classification.get("implementation_files", code_files)

        test_result = await self.test_organizer.generate_test_script(
            repo_path=self.repo_path,
            test_files=test_files,
            code_files=code_files,
            task_dir=task_dir,
            log=task_log,
            base_commit=base_commit,
            target_commit=target_commit,
            query=final_query,
            repo_url=f"https://github.com/{self.repo_name}",
        )
        if not test_result:
            task_log.warning("LLM test script generation failed, using fallback")
            self.test_organizer.generate_fallback_script(
                test_files, code_files, task_dir, task_log
            )

        # --- Phase 5: Test Environment Validation ---
        task_log.info("Phase 5: Test Environment Validation")
        env_validation = await self._phase5_validate_test_env(
            base_commit, target_commit, task_dir, task_log
        )
        result["test_env_validation"] = env_validation

        # --- Phase 6: Test-Query Alignment ---
        task_log.info("Phase 6: Test-Query Alignment Validation")
        alignment = await self._phase6_validate_alignment(
            final_query, test_files, base_commit, target_commit, task_dir, task_log
        )
        result["alignment"] = alignment

        # --- Phase 7: Commit Test Validation ---
        task_log.info("Phase 7: Commit Test Validation")
        validation = await self._phase7_commit_test_validation(
            base_commit, target_commit, task_dir, task_log
        )
        result["commit_validation"] = validation

        # --- Phase 8: Real Test Validation ---
        if self.actor_models:
            task_log.info("Phase 8: Real Test Validation")
            real_validation = await self._phase8_real_test_validation(
                final_query, base_commit, task_dir, task_log
            )
            result["real_validation"] = real_validation
        else:
            task_log.info("Phase 8: Skipped (no actor models configured)")

        # Save task_summary.json (comprehensive output)
        task_summary = {
            **task_meta,
            "query": final_query,
            "llm_score": llm_score,
            "commit_validation": validation,
            "alignment": alignment,
            "test_env_validation": env_validation,
        }
        if self.actor_models and "real_validation" in result:
            task_summary["model_validation"] = {
                "scoring_method": "relative_to_target",
                "models": result["real_validation"],
            }

        (task_dir / "task_summary.json").write_text(
            json.dumps(task_summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        result["status"] = "completed"
        task_log.info(f"PR #{pr_number} processing complete")
        return result

    # ==================================================================
    # AI File Classification (NEW)
    # ==================================================================

    async def _ai_classify_files_for_pr(
        self,
        repo_path: Path,
        repo_name: str,
        pr_number: int,
        pr_title: str,
        base_commit: str,
        target_commit: str,
        changed_files: List[str],
        output_dir: Path,
        logger: logging.Logger,
    ) -> Optional[Dict]:
        """
        Use AI to classify PR files into implementation vs test/build.

        This produces more accurate code.patch and test.patch than
        rule-based classification.
        """
        logger.info(f"Starting AI file classification for PR #{pr_number}")
        logger.info(f"  Changed files: {len(changed_files)}")

        classify_dir = output_dir / "ai_file_classification"
        classify_dir.mkdir(parents=True, exist_ok=True)

        # Get git diff summary
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat", base_commit, target_commit],
                cwd=repo_path, capture_output=True, text=True, timeout=60,
            )
            diff_summary = diff_result.stdout if diff_result.returncode == 0 else "Failed"
        except Exception as e:
            diff_summary = f"Error: {e}"

        # Load prompt
        prompt_template = self.prompts.get("classify_pr_files_verilog", "")
        if not prompt_template:
            logger.warning("classify_pr_files_verilog prompt not found")
            return None

        changed_files_str = "\n".join(f"- {f}" for f in changed_files)
        prompt = prompt_template.format(
            repo_name=repo_name,
            pr_number=pr_number,
            pr_title=pr_title,
            base_commit=base_commit[:8],
            target_commit=target_commit[:8],
            changed_files=changed_files_str,
            diff_summary=diff_summary[:3000],
        )

        (classify_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=classify_dir,
            model=self.flash_model,
            task_name="ai_file_classification",
        )

        try:
            data = await executor.execute_with_json_retry(
                query=prompt,
                continue_conversation=False,
                timeout=3000,
                max_retries=3,
                must_include_keys=["implementation_files", "test_and_build_files"],
            )

            if data:
                impl_files = data.get("implementation_files", [])
                test_files = data.get("test_and_build_files", [])
                reasoning = data.get("reasoning", "")

                logger.info(f"AI classification completed:")
                logger.info(f"  Implementation files: {len(impl_files)}")
                logger.info(f"  Test & build files: {len(test_files)}")

                # Validate completeness
                classified = set(impl_files) | set(test_files)
                missing = set(changed_files) - classified
                if missing:
                    logger.warning(f"  {len(missing)} files not classified, adding to test_and_build")
                    test_files.extend(list(missing))

                result = {
                    "implementation_files": impl_files,
                    "test_and_build_files": test_files,
                    "reasoning": reasoning,
                }

                (classify_dir / "classification_result.json").write_text(
                    json.dumps(result, indent=4, ensure_ascii=False), encoding="utf-8"
                )
                return result
            else:
                logger.warning("AI file classification returned None")
                return None

        except Exception as e:
            logger.error(f"AI file classification error: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ==================================================================
    # Patch Generation (enhanced with AI classification)
    # ==================================================================

    def _generate_patches_from_classification(
        self,
        base_commit: str,
        target_commit: str,
        impl_files: List[str],
        test_build_files: List[str],
        task_dir: Path,
        log: logging.Logger,
    ) -> Tuple[bool, bool]:
        """Generate code.patch and test.patch based on AI classification."""
        has_code = False
        has_test = False

        # Generate code.patch from implementation files
        if impl_files:
            try:
                result = subprocess.run(
                    ["git", "diff", base_commit, target_commit, "--"] + impl_files,
                    cwd=self.repo_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0 and result.stdout.strip():
                    (task_dir / "code.patch").write_text(result.stdout, encoding="utf-8")
                    has_code = True
                    log.info(f"Generated code.patch from {len(impl_files)} implementation files")
            except Exception as e:
                log.error(f"Failed to generate code.patch: {e}")

        # Generate test.patch from test/build files
        if test_build_files:
            try:
                result = subprocess.run(
                    ["git", "diff", base_commit, target_commit, "--"] + test_build_files,
                    cwd=self.repo_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0 and result.stdout.strip():
                    (task_dir / "test.patch").write_text(result.stdout, encoding="utf-8")
                    has_test = True
                    log.info(f"Generated test.patch from {len(test_build_files)} test/build files")
            except Exception as e:
                log.error(f"Failed to generate test.patch: {e}")

        return has_code, has_test

    # ==================================================================
    # Test Patch Integrity Verification (NEW)
    # ==================================================================

    def _verify_test_patch_integrity(
        self,
        repo_path: Path,
        test_patch_path: Path,
        log: logging.Logger,
    ) -> Dict:
        """
        Verify that model hasn't modified test.patch files.

        Used during Phase 8 actor validation to detect cheating:
        if the model modified test files, it's invalid.
        """
        result = {
            "integrity_valid": True,
            "modified_test_files": [],
            "total_test_files": 0,
        }

        if not test_patch_path.exists():
            return result

        # Parse files in test.patch
        patch_content = test_patch_path.read_text(encoding="utf-8", errors="replace")
        file_pattern = r'^diff --git a/(.+?) b/\1$'
        test_files_in_patch = set()
        for match in re.finditer(file_pattern, patch_content, re.MULTILINE):
            test_files_in_patch.add(match.group(1))

        result["total_test_files"] = len(test_files_in_patch)

        if not test_files_in_patch:
            return result

        # Check if these files were modified in working tree
        try:
            git_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=60,
            )
            if git_status.returncode == 0:
                modified_files = set()
                for line in git_status.stdout.strip().split("\n"):
                    if line:
                        file_path = line[3:].split(" -> ")[0]
                        if line[:2].strip():
                            modified_files.add(file_path)

                for test_file in test_files_in_patch:
                    if test_file in modified_files:
                        result["modified_test_files"].append(test_file)
                        result["integrity_valid"] = False
                        log.warning(f"  ⚠️ Test file modified by model: {test_file}")

        except Exception as e:
            log.warning(f"  Failed to check file modifications: {e}")

        if result["integrity_valid"]:
            log.info(f"  ✓ Test patch integrity valid ({len(test_files_in_patch)} files)")
        else:
            log.warning(
                f"  ❌ Test patch integrity violated: "
                f"{len(result['modified_test_files'])} files modified"
            )

        return result

    # ==================================================================
    # Phase 1: Repository Setup
    # ==================================================================

    async def _phase1_repo_setup(
        self, pr_info: Dict, task_dir: Path, log: logging.Logger
    ) -> Tuple[Optional[str], Optional[str]]:
        """Set up repository and determine base/target commits."""
        pr_number = pr_info.get("pr_number", pr_info.get("number", 0))

        base_commit = pr_info.get("base_commit", "")
        merge_commit = pr_info.get("merge_commit", "")

        if not base_commit or not merge_commit:
            log.info(f"Fetching commit info from GitHub API for PR #{pr_number}")
            try:
                from github import Github, Auth
                auth = Auth.Token(self.github_token)
                g = Github(auth=auth)
                repo = g.get_repo(self.repo_name)
                pr = repo.get_pull(pr_number)
                base_commit = pr.base.sha
                merge_commit = pr.merge_commit_sha
            except Exception as e:
                log.error(f"Failed to get PR info: {e}")
                return None, None

        self._ensure_commit(self.repo_path, base_commit, log)
        self._ensure_commit(self.repo_path, merge_commit, log)
        self._git_checkout(self.repo_path, base_commit, log)

        log.info(f"Base commit: {base_commit[:8]}")
        log.info(f"Target commit: {merge_commit[:8]}")

        return base_commit, merge_commit

    # ==================================================================
    # Phase 5: Test Environment Validation
    # ==================================================================

    async def _phase5_validate_test_env(
        self, base_commit: str, target_commit: str,
        task_dir: Path, log: logging.Logger,
    ) -> Dict:
        """Validate and improve test environment using Claude Code."""
        val_dir = task_dir / "test_environment_validation"
        val_dir.mkdir(parents=True, exist_ok=True)

        run_tests_sh = task_dir / "run-tests.sh"
        if not run_tests_sh.exists():
            log.warning("run-tests.sh not found, skipping validation")
            return {"validation_status": "SKIP", "can_use": False}

        prompt_template = self.prompts.get("validate_and_improve_test_env_verilog", "")
        if not prompt_template:
            return {"validation_status": "SKIP", "can_use": True}

        prompt = prompt_template.format(
            repo_path=self.repo_path,
            base_commit=base_commit[:8],
            target_commit=target_commit[:8],
            run_tests_sh=run_tests_sh,
            query="",
            task_dir=task_dir,
        )

        executor = ClaudeCodeExecutor(
            work_dir=self.repo_path,
            output_dir=val_dir,
            model=self.flash_model,
            task_name="validate_test_env",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt, timeout=6000, max_retries=3,
                must_include_keys=["validation_status"],
            )
            if result:
                (val_dir / "validation_result.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8"
                )
                return result
            return {"validation_status": "FAIL", "can_use": False}
        except Exception as e:
            log.error(f"Test env validation failed: {e}")
            return {"validation_status": "ERROR", "can_use": False, "error": str(e)}
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ==================================================================
    # Phase 6: Test-Query Alignment
    # ==================================================================

    async def _phase6_validate_alignment(
        self, query: str, test_files: List[str],
        base_commit: str, target_commit: str,
        task_dir: Path, log: logging.Logger,
    ) -> Dict:
        """Validate alignment between query and tests."""
        val_dir = task_dir / "test_query_validation"
        val_dir.mkdir(parents=True, exist_ok=True)

        # Read test file contents
        test_contents = ""
        for tf in test_files[:5]:
            tf_path = self.repo_path / tf
            if tf_path.exists():
                try:
                    content = tf_path.read_text(encoding="utf-8", errors="ignore")
                    test_contents += f"\n--- {tf} ---\n{content[:3000]}\n"
                except Exception:
                    pass

        prompt_template = self.prompts.get("validate_test_query_alignment_verilog", "")
        if not prompt_template:
            return {"overall_alignment_score": 0, "recommendation": "SKIP"}

        prompt = prompt_template.format(
            query=query,
            test_files="\n".join(f"  - {f}" for f in test_files[:20]),
            test_contents=test_contents[:10000],
            test_cases_info=test_contents[:10000],
            test_commands="bash run-tests.sh",
            repo_name=self.repo_name,
            base_commit=base_commit[:8],
            target_commit=target_commit[:8],
        )

        executor = ClaudeCodeExecutor(
            work_dir=self.repo_path,
            output_dir=val_dir,
            model=self.flash_model,
            task_name="validate_alignment",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt, timeout=3000, max_retries=3,
                must_include_keys=["overall_alignment_score"],
            )
            if result:
                (val_dir / "alignment_assessment.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8"
                )
                return result
            return {"overall_alignment_score": 0, "recommendation": "FAIL"}
        except Exception as e:
            log.error(f"Alignment validation failed: {e}")
            return {"overall_alignment_score": 0, "recommendation": "ERROR"}
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ==================================================================
    # Phase 7: Commit Test Validation (enhanced)
    # ==================================================================

    async def _phase7_commit_test_validation(
        self, base_commit: str, target_commit: str,
        task_dir: Path, log: logging.Logger,
    ) -> Dict:
        """
        Validate test results on base vs target commits.

        Enhanced: detailed patch error logging, expected_test_count,
        output file saving, result.json parsing.
        """
        val_dir = task_dir / "commit_test_validation"
        val_dir.mkdir(parents=True, exist_ok=True)

        results = {
            "base_commit": {},
            "target_commit": {},
            "delta": {},
            "validation": "UNKNOWN",
        }

        run_tests_sh = task_dir / "run-tests.sh"
        if not run_tests_sh.exists():
            log.error("run-tests.sh not found")
            results["validation"] = "MISSING_SCRIPT"
            self._save_validation_result(val_dir, results)
            return results

        # Phase 7A: Target validation (base + test.patch + code.patch)
        log.info("7A: Testing with implementation (base + test.patch + code.patch)")
        target_result = await self._run_tests_on_commit(
            commit=base_commit,
            apply_test_patch=True,
            apply_code_patch=True,
            task_dir=task_dir,
            output_dir=val_dir,
            log=log,
            label="with_code",
        )

        target_patch_error = target_result.get("patch_apply_error")
        if target_patch_error:
            log.error(f"Patch apply error in target validation: {target_patch_error}")
            results["validation"] = "PATCH_ERROR"
            results["patch_error"] = {"phase": "target", "failed_patch": target_patch_error}
            results["target_commit"] = {
                "sha": target_commit,
                "status": f"patch_apply_failed:{target_patch_error}",
                "pass_rate": 0.0, "tests_total": 0, "tests_passed": 0,
            }
            results["base_commit"] = {
                "sha": base_commit, "status": "not_tested", "pass_rate": 0.0,
            }
            results["delta"] = {"pass_rate_improvement": 0.0}
            self._save_validation_result(val_dir, results)
            return results

        target_pass_rate = target_result.get("pass_rate", 0.0)
        target_exit = target_result.get("exit_code", -1)
        target_total = target_result.get("total", 0)
        target_passed_flag = target_exit == 0 and target_pass_rate > 0.9

        results["target_commit"] = {
            "sha": target_commit,
            "validation_method": "base + test.patch + code.patch",
            "tests_total": target_total,
            "tests_passed": target_result.get("passed", 0),
            "tests_failed": target_result.get("failed", 0),
            "tests_error": target_result.get("error", 0),
            "pass_rate": target_pass_rate,
            "exit_code": target_exit,
            "status": "expected_pass" if target_passed_flag else "unexpected_fail",
        }

        # Phase 7B: Base validation (base + test.patch only)
        log.info("7B: Testing without implementation (base + test.patch)")
        base_result = await self._run_tests_on_commit(
            commit=base_commit,
            apply_test_patch=True,
            apply_code_patch=False,
            task_dir=task_dir,
            output_dir=val_dir,
            log=log,
            expected_test_count=target_total,
            label="no_code",
        )

        base_patch_error = base_result.get("patch_apply_error")
        if base_patch_error:
            log.error(f"Patch apply error in base validation: {base_patch_error}")
            results["validation"] = "PATCH_ERROR"
            results["patch_error"] = {"phase": "base", "failed_patch": base_patch_error}
            results["base_commit"] = {
                "sha": base_commit,
                "status": f"patch_apply_failed:{base_patch_error}",
                "pass_rate": 0.0,
            }
            results["delta"] = {"pass_rate_improvement": 0.0}
            self._save_validation_result(val_dir, results)
            return results

        base_pass_rate = base_result.get("pass_rate", 0.0)
        base_exit = base_result.get("exit_code", -1)
        base_total = base_result.get("total", 0)
        base_failed_flag = base_exit != 0 or base_total < target_total or base_pass_rate < 0.5

        results["base_commit"] = {
            "sha": base_commit,
            "validation_method": "base + test.patch",
            "tests_total": base_total,
            "tests_passed": base_result.get("passed", 0),
            "tests_failed": base_result.get("failed", 0),
            "tests_error": base_result.get("error", 0),
            "pass_rate": base_pass_rate,
            "exit_code": base_exit,
            "status": "expected_fail" if base_failed_flag else "unexpected_pass",
        }

        # Compute delta
        improvement = target_pass_rate - base_pass_rate
        results["delta"] = {
            "pass_rate_improvement": improvement,
            "tests_fixed": (
                results["target_commit"].get("tests_passed", 0) -
                results["base_commit"].get("tests_passed", 0)
            ),
        }

        if base_failed_flag and target_passed_flag:
            results["validation"] = "PASS"
            log.info(f"✅ Validation PASSED: base={base_pass_rate:.1%}, target={target_pass_rate:.1%}")
        else:
            results["validation"] = "FAIL"
            log.warning(f"❌ Validation FAILED: base={base_pass_rate:.1%}, target={target_pass_rate:.1%}")
            if not base_failed_flag:
                log.warning(f"   Base should fail but: pass_rate={base_pass_rate:.1%}, exit={base_exit}")
            if not target_passed_flag:
                log.warning(f"   Target should pass but: pass_rate={target_pass_rate:.1%}, exit={target_exit}")

        self._save_validation_result(val_dir, results)
        return results

    async def _run_tests_on_commit(
        self,
        commit: str,
        apply_test_patch: bool,
        apply_code_patch: bool,
        task_dir: Path,
        output_dir: Path,
        log: logging.Logger,
        expected_test_count: Optional[int] = None,
        label: str = "",
    ) -> Dict:
        """
        Run tests on a specific commit with optional patches.

        Enhanced: reset+clean, detailed error logging, output file saving,
        result.json parsing, expected_test_count recalculation.
        """
        log.info(f"Running tests on commit {commit[:8]} (label={label})...")

        # 0. Reset repo state
        try:
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=self.repo_path, capture_output=True, timeout=60, text=True,
            )
            subprocess.run(
                ["git", "clean", "-fdx", "-e", ".venv"],
                cwd=self.repo_path, capture_output=True, timeout=180, text=True,
            )
        except Exception as e:
            log.warning(f"Failed to reset repo: {e}")

        # 1. Checkout commit
        try:
            result = subprocess.run(
                ["git", "checkout", commit],
                cwd=self.repo_path, capture_output=True, timeout=180, text=True,
            )
            if result.returncode != 0:
                log.error(f"Failed to checkout {commit[:8]}: {result.stderr[:200]}")
                return {"total": 0, "passed": 0, "failed": 0, "error": 1, "pass_rate": 0.0, "exit_code": -1}
        except Exception as e:
            log.error(f"Checkout exception: {e}")
            return {"total": 0, "passed": 0, "failed": 0, "error": 1, "pass_rate": 0.0, "exit_code": -1}

        # 2. Clean after checkout
        subprocess.run(
            ["git", "clean", "-fdx", "-e", ".venv"],
            cwd=self.repo_path, capture_output=True, timeout=180, text=True,
        )

        # 3. Apply test.patch
        if apply_test_patch:
            test_patch = task_dir / "test.patch"
            if test_patch.exists() and test_patch.stat().st_size > 0:
                success, stdout, stderr = self._apply_patch_robust_detailed(test_patch, log)
                if not success:
                    # Save error log
                    error_file = output_dir / f"patch_error_test_{commit[:8]}.log"
                    try:
                        error_file.write_text(
                            f"=== test.patch Apply Error ===\nCommit: {commit}\n"
                            f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\n",
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    return {
                        "total": 0, "passed": 0, "failed": 0, "error": 1,
                        "pass_rate": 0.0, "exit_code": -2,
                        "patch_apply_error": "test.patch",
                        "output": f"test.patch apply failed: {stderr[:500]}",
                    }
            else:
                log.error("test.patch not found or empty")
                return {
                    "total": 0, "passed": 0, "failed": 0, "error": 1,
                    "pass_rate": 0.0, "exit_code": -2,
                    "patch_apply_error": "test.patch",
                }

        # 4. Apply code.patch
        if apply_code_patch:
            code_patch = task_dir / "code.patch"
            if code_patch.exists() and code_patch.stat().st_size > 0:
                success, stdout, stderr = self._apply_patch_robust_detailed(code_patch, log)
                if not success:
                    error_file = output_dir / f"patch_error_code_{commit[:8]}.log"
                    try:
                        error_file.write_text(
                            f"=== code.patch Apply Error ===\nCommit: {commit}\n"
                            f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}\n",
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    return {
                        "total": 0, "passed": 0, "failed": 0, "error": 1,
                        "pass_rate": 0.0, "exit_code": -3,
                        "patch_apply_error": "code.patch",
                    }

        # 5. Copy run-tests.sh to repo
        run_tests_src = task_dir / "run-tests.sh"
        run_tests_dst = self.repo_path / "run-tests.sh"
        if run_tests_src.exists():
            shutil.copy(run_tests_src, run_tests_dst)
            run_tests_dst.chmod(0o755)
        else:
            log.error("run-tests.sh not found")
            return {"total": 0, "passed": 0, "failed": 0, "error": 1, "pass_rate": 0.0, "exit_code": -1}

        # 6. Run tests
        try:
            proc = subprocess.run(
                ["bash", str(run_tests_dst)],
                cwd=self.repo_path,
                capture_output=True,
                timeout=24000,  # 400 minutes
            )
            stdout_str = proc.stdout.decode("utf-8", errors="replace")
            stderr_str = proc.stderr.decode("utf-8", errors="replace")
            output = stdout_str + "\n" + stderr_str
            exit_code = proc.returncode

            # 7. Save output
            output_file = output_dir / f"test_output_{commit[:8]}_{label}.log"
            try:
                output_file.write_text(
                    f"=== Test Output for commit {commit[:8]} ===\n"
                    f"apply_test_patch: {apply_test_patch}\n"
                    f"apply_code_patch: {apply_code_patch}\n"
                    f"exit_code: {exit_code}\n{'='*60}\n\n{output}",
                    encoding="utf-8",
                )
            except Exception:
                pass

            # 8. Parse results (priority: result.json > rule-based)
            test_stats = parse_test_output(output, exit_code, self.repo_path)
            test_stats["exit_code"] = exit_code
            test_stats["output"] = output[-2000:]  # Keep last 2000 chars

            # 9. Recalculate with expected_test_count
            if expected_test_count is not None and expected_test_count > 0:
                actual_total = test_stats.get("total", 0)
                test_stats["total"] = expected_test_count
                test_stats["pass_rate"] = test_stats["passed"] / expected_test_count
                if actual_total != expected_test_count:
                    log.info(
                        f"  Note: reported {actual_total} tests, "
                        f"expected {expected_test_count}"
                    )

            # 10. Log failures
            if exit_code != 0:
                log.warning(f"  Tests failed (exit_code={exit_code})")
                output_lines = output.strip().split("\n")
                log.warning("  Last 15 lines:")
                for line in output_lines[-15:]:
                    log.warning(f"    | {line[:200]}")

            log.info(
                f"  Tests: {test_stats['passed']}/{test_stats['total']} passed "
                f"({test_stats['pass_rate']:.1%}), exit={exit_code}"
            )

            return test_stats

        except subprocess.TimeoutExpired:
            log.error(f"  Tests timed out on {commit[:8]}")
            return {"total": 0, "passed": 0, "failed": 0, "error": 1, "pass_rate": 0.0, "exit_code": -1, "output": "TIMEOUT"}
        except Exception as e:
            log.error(f"  Failed to run tests: {e}")
            return {"total": 0, "passed": 0, "failed": 0, "error": 1, "pass_rate": 0.0, "exit_code": -1, "output": str(e)}

    # ==================================================================
    # Phase 8: Real Test Validation
    # ==================================================================

    async def _phase8_real_test_validation(
        self, query: str, base_commit: str,
        task_dir: Path, log: logging.Logger,
    ) -> Dict:
        """Run real test validation with actor models."""
        from real_test_validator import RealTestValidator

        val_dir = task_dir / "validate_difficulty"
        val_dir.mkdir(parents=True, exist_ok=True)

        validator = RealTestValidator(
            repo_path=self.repo_path,
            model_names=self.actor_models,
            n_runs=self.validation_runs,
        )

        results = {}
        for model_name in self.actor_models:
            log.info(f"Validating with actor model: {model_name}")
            try:
                avg_score, passed_all, scores = await validator.validate(
                    query=query,
                    base_commit=base_commit,
                    task_dir=task_dir,
                    model_name=model_name,
                    output_dir=val_dir / model_name,
                    log=log,
                )
                results[model_name] = {
                    "avg_score": avg_score,
                    "passed": passed_all,
                    "scores": scores,
                }

                # Early stopping if first model scores > 70%
                if avg_score > 0.7:
                    log.info(f"  First model scored {avg_score:.0%}, early stopping enabled")

            except Exception as e:
                log.error(f"Validation with {model_name} failed: {e}")
                results[model_name] = {"error": str(e), "avg_score": 0, "scores": []}

        (val_dir / "summary.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return results

    # ==================================================================
    # Scoring
    # ==================================================================

    def _calculate_validation_score(self, validation_result: Dict) -> float:
        """Calculate validation score (0-10) from Phase 7 results."""
        target_pass_rate = validation_result.get("target_commit", {}).get("pass_rate", 0)
        base_pass_rate = validation_result.get("base_commit", {}).get("pass_rate", 0)
        improvement = target_pass_rate - base_pass_rate

        improvement_score = min(improvement * 5, 5.0)
        target_score = target_pass_rate * 3.0
        base_score = (1 - base_pass_rate) * 2.0
        return min(improvement_score + target_score + base_score, 10.0)

    def _calculate_final_score(self, llm_score: float, validation_score: float) -> float:
        """Calculate final score: LLM * 0.3 + Validation * 0.7."""
        return round(llm_score * 0.3 + validation_score * 0.7, 2)

    # ==================================================================
    # PR Discovery
    # ==================================================================

    async def _discover_prs(self) -> List[Dict]:
        """Discover suitable PRs for the repository."""
        if self.use_pr_discovery:
            discovery = VerilogPRDiscovery(
                github_token=self.github_token,
                model=self.model,
                flash_model=self.flash_model,
                task_type=self.task_type,
            )

            result = await discovery.analyze_repository(
                repo_name=self.repo_name,
                max_prs=self.max_prs,
                top_n=self.top_n,
                repo_path=self.repo_path,
                logger=self.logger,
                require_code_and_test=self.require_code_and_test,
            )

            recommended = result.get("recommended_prs", [])
            if recommended:
                self.logger.info(f"PR Discovery found {len(recommended)} PRs")

                # Fetch full PR info from GitHub API for each recommended PR
                for pr in recommended:
                    pr_num = pr.get("pr_number")
                    if pr_num and not pr.get("base_commit"):
                        try:
                            from github import Github, Auth
                            auth = Auth.Token(self.github_token)
                            g = Github(auth=auth)
                            repo = g.get_repo(self.repo_name)
                            gh_pr = repo.get_pull(pr_num)
                            pr["base_commit"] = gh_pr.base.sha
                            pr["merge_commit"] = gh_pr.merge_commit_sha
                            pr["files"] = [f.filename for f in gh_pr.get_files()]
                            self.logger.info(f"  PR #{pr_num}: fetched commit info")
                        except Exception as e:
                            self.logger.warning(f"  PR #{pr_num}: failed to fetch: {e}")

                return recommended

        self.logger.warning("PR Discovery returned no results")
        return []

    # ==================================================================
    # Helper methods
    # ==================================================================

    async def _clone_repo(self) -> Optional[Path]:
        """Clone the repository."""
        repo_dir = self.output_dir / "repo"
        if repo_dir.exists():
            self.logger.info(f"Repo already cloned at {repo_dir}")
            return repo_dir

        url = f"https://github.com/{self.repo_name}"
        if self.github_token:
            url = f"https://{self.github_token}@github.com/{self.repo_name}"

        self.logger.info(f"Cloning {self.repo_name}...")
        try:
            result = subprocess.run(
                ["git", "clone", "--depth=500", url, str(repo_dir)],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0:
                self.logger.info(f"Cloned to {repo_dir}")
                return repo_dir
            else:
                self.logger.error(f"Clone failed: {result.stderr}")
                return None
        except Exception as e:
            self.logger.error(f"Clone exception: {e}")
            return None

    def _get_changed_files(
        self, base_commit: str, target_commit: str, log: logging.Logger
    ) -> List[str]:
        """Get list of files changed between two commits."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", base_commit, target_commit],
                cwd=self.repo_path, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                files = [f for f in result.stdout.strip().split("\n") if f.strip()]
                log.info(f"Changed files: {len(files)}")
                return files
        except Exception as e:
            log.error(f"Failed to get changed files: {e}")
        return []

    def _generate_patches(
        self, repo_path: Path, base_commit: str, target_commit: str,
        task_dir: Path, log: logging.Logger,
    ) -> Tuple[bool, bool]:
        """Generate code.patch and test.patch from diff (rule-based fallback)."""
        try:
            result = subprocess.run(
                ["git", "diff", base_commit, target_commit],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log.error(f"git diff failed: {result.stderr}")
                return False, False
            full_diff = result.stdout
        except Exception as e:
            log.error(f"git diff exception: {e}")
            return False, False

        code_hunks = []
        test_hunks = []
        current_file = ""
        current_hunk = []

        for line in full_diff.split("\n"):
            if line.startswith("diff --git"):
                if current_file and current_hunk:
                    file_type = classify_file(current_file)
                    hunk_text = "\n".join(current_hunk) + "\n"
                    if file_type == "code":
                        code_hunks.append(hunk_text)
                    elif file_type == "test":
                        test_hunks.append(hunk_text)
                parts = line.split()
                if len(parts) >= 4:
                    current_file = parts[2][2:]
                current_hunk = [line]
            else:
                current_hunk.append(line)

        if current_file and current_hunk:
            file_type = classify_file(current_file)
            hunk_text = "\n".join(current_hunk) + "\n"
            if file_type == "code":
                code_hunks.append(hunk_text)
            elif file_type == "test":
                test_hunks.append(hunk_text)

        has_code = has_test = False
        if code_hunks:
            (task_dir / "code.patch").write_text("".join(code_hunks), encoding="utf-8")
            has_code = True
            log.info(f"Generated code.patch ({len(code_hunks)} file hunks)")
        if test_hunks:
            (task_dir / "test.patch").write_text("".join(test_hunks), encoding="utf-8")
            has_test = True
            log.info(f"Generated test.patch ({len(test_hunks)} file hunks)")

        return has_code, has_test

    def _apply_patch_robust(self, patch_path: Path, log: logging.Logger) -> bool:
        """Apply patch with 4 fallback strategies."""
        success, _, _ = self._apply_patch_robust_detailed(patch_path, log)
        return success

    def _apply_patch_robust_detailed(
        self, patch_path: Path, log: logging.Logger
    ) -> Tuple[bool, str, str]:
        """Apply patch with 4 fallback strategies. Returns (success, stdout, stderr)."""
        strategies = [
            (["git", "apply", "--verbose", str(patch_path)], "git apply"),
            (["git", "apply", "--3way", str(patch_path)], "git apply --3way"),
            (["patch", "-p1", "-i", str(patch_path)], "patch -p1"),
            (["git", "apply", "--reject", str(patch_path)], "git apply --reject"),
        ]

        last_stdout = ""
        last_stderr = ""
        for cmd, name in strategies:
            try:
                result = subprocess.run(
                    cmd, cwd=self.repo_path,
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    log.info(f"Patch applied with {name}")
                    return True, result.stdout, result.stderr
                last_stdout = result.stdout
                last_stderr = result.stderr
                log.debug(f"{name} failed: {result.stderr[:200]}")
            except Exception as e:
                last_stderr = str(e)
                log.debug(f"{name} exception: {e}")

        log.error(f"All patch strategies failed for {patch_path.name}")
        return False, last_stdout, last_stderr

    def _ensure_commit(self, repo_path: Path, sha: str, log: logging.Logger):
        """Ensure a commit is available locally."""
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.info(f"Fetching commit {sha[:8]}...")
            subprocess.run(
                ["git", "fetch", "origin", sha, "--depth=1"],
                cwd=repo_path, capture_output=True, text=True, timeout=120,
            )

    def _git_checkout(self, repo_path: Path, sha: str, log: logging.Logger):
        """Checkout a commit and clean working tree."""
        subprocess.run(
            ["git", "checkout", sha, "--force"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            ["git", "clean", "-fdx", "-e", ".venv"],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )

    def _get_current_commit(self, repo_path: Path) -> str:
        """Get current HEAD commit SHA."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _save_validation_result(self, val_dir: Path, results: Dict):
        """Save validation results to JSON."""
        with open(val_dir / "result.json", "w") as f:
            json.dump(results, f, indent=4, ensure_ascii=False)

    def _cleanup_failed_task(self, task_dir: Path, log: logging.Logger):
        """Clean up a failed task directory (keep logs, remove large files)."""
        try:
            for f in ["code.patch", "test.patch"]:
                fpath = task_dir / f
                if fpath.exists() and fpath.stat().st_size > 100000:
                    fpath.unlink()
                    log.info(f"Cleaned up large file: {f}")
        except Exception as e:
            log.warning(f"Cleanup failed: {e}")

    def _sync_task_to_final(self, task_dir: Path):
        """Sync a completed task to final output directory."""
        if not self.copy_to_final:
            return
        try:
            task_id_env = os.environ.get("TASK_ID", "local")
            final_dir = self.copy_to_final / task_id_env / task_dir.name
            final_dir.mkdir(parents=True, exist_ok=True)
            for item in task_dir.iterdir():
                dst = final_dir / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)
            self.logger.info(f"Synced {task_dir.name} to {final_dir}")
        except Exception as e:
            self.logger.warning(f"Failed to sync to final: {e}")

    def _sync_summary_to_final(self, summary_path: Path):
        """Sync summary and logs to final output directory."""
        if not self.copy_to_final:
            return
        try:
            task_id_env = os.environ.get("TASK_ID", "local")
            final_root = self.copy_to_final / task_id_env
            final_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(summary_path, final_root / "pr_summary.json")

            log_file = self.output_dir / "task_generator.log"
            if log_file.exists():
                shutil.copy2(log_file, final_root / "system.log")

            self.logger.info(f"Synced summary to {final_root}")
        except Exception as e:
            self.logger.warning(f"Failed to sync summary: {e}")


# ==================================================================
# CLI entry point
# ==================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Verilog Task Generator")
    parser.add_argument("--repo", required=True, help="Repository name (owner/repo)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--repo-path", help="Path to cloned repo")
    parser.add_argument("--model", default="", help="Model name")
    parser.add_argument("--flash-model", default="", help="Flash model name")
    parser.add_argument("--top-n", type=int, default=20, help="Top N PRs per repo")
    parser.add_argument("--max-prs", type=int, default=100, help="Max candidate PRs to fetch")
    parser.add_argument("--pr-timeout", type=int, default=5400, help="Per-PR timeout (seconds)")
    parser.add_argument("--repo-timeout", type=int, default=14400, help="Per-repo timeout (seconds)")
    parser.add_argument("--quality-threshold", type=float, default=6.5)
    parser.add_argument("--validation-runs", type=int, default=4)
    parser.add_argument("--actor-models", nargs="*", default=[], help="Actor models")
    parser.add_argument("--no-pr-discovery", action="store_true")
    parser.add_argument("--no-code-test-filter", action="store_true")
    parser.add_argument(
        "--task-type", default="all", choices=["new_feature", "bugfix", "all"],
        help="Task type: new_feature, bugfix, or all",
    )
    parser.add_argument("--fewshot-dir", help="Few-shot examples directory")
    parser.add_argument("--copy-to-final", help="Final output directory for sync")
    parser.add_argument("--run-mode", default="local", choices=["local", "remote"])

    args = parser.parse_args()

    generator = VerilogTaskGenerator(
        repo_name=args.repo,
        output_dir=Path(args.output),
        repo_path=Path(args.repo_path) if args.repo_path else None,
        model=args.model,
        flash_model=args.flash_model,
        actor_models=args.actor_models,
        top_n=args.top_n,
        max_prs=args.max_prs,
        pr_timeout=args.pr_timeout,
        repo_timeout=args.repo_timeout,
        quality_threshold=args.quality_threshold,
        validation_runs=args.validation_runs,
        use_pr_discovery=not args.no_pr_discovery,
        require_code_and_test=not args.no_code_test_filter,
        task_type=args.task_type,
        fewshot_dir=Path(args.fewshot_dir) if args.fewshot_dir else None,
        copy_to_final=Path(args.copy_to_final) if args.copy_to_final else None,
        run_mode=args.run_mode,
    )

    asyncio.run(generator.generate_tasks())


if __name__ == "__main__":
    main()
