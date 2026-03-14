"""
Craft Orchestrator - Main pipeline for new_feature_craft.

Per-repo pipeline:
  Phase 1: Repo Setup & Test Analysis
  Phase 2: Test Setup & Validation
  Phase 3: Module Mining
  Phase 4: Query Generation (3 levels)
  Phase 5: Actor Validation

Adapted from verilog_mining/verilog_task_generator.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.claude_code_executor import setup_logger
from config import CraftConfig
from models import ActorResult, CraftTask, ModuleMining, QueryResult, RepoResult
from test_setup import TestSetup
from module_miner import ModuleMiner
from query_crafter import QueryCrafter
from actor_validator import ActorValidator

logger = logging.getLogger(__name__)


class CraftOrchestrator:
    """
    Main orchestrator for the new_feature_craft pipeline.

    Usage:
        orchestrator = CraftOrchestrator(
            repo_name="owner/repo",
            config=CraftConfig(...),
        )
        result = await orchestrator.run()
    """

    def __init__(
        self,
        repo_name: str,
        config: CraftConfig,
        repo_path: Optional[Path] = None,
    ):
        self.repo_name = repo_name
        self.config = config
        self.repo_path = Path(repo_path) if repo_path else None

        # Derive output directory
        safe_name = repo_name.replace("/", "_")
        self.output_dir = Path(config.output_dir) / safe_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Set run mode
        os.environ["CLAUDE_CODE_RUN_MODE"] = config.run_mode

        # Logger
        self.logger = setup_logger(
            self.output_dir / "orchestrator.log", f"craft-{safe_name}"
        )

        # Initialize components
        self.test_setup = TestSetup(
            model=config.model,
            flash_model=config.flash_model,
            test_timeout=config.test_timeout,
        )
        self.module_miner = ModuleMiner(
            model=config.model,
            flash_model=config.flash_model,
            test_timeout=config.test_timeout,
            max_modules=config.modules_per_repo,
        )
        self.query_crafter = QueryCrafter(
            model=config.model,
            quality_threshold=config.quality_threshold,
        )
        self.actor_validator = ActorValidator(
            actor_models=config.actor_models,
            validation_runs=config.actor_validation_runs,
            test_timeout=config.test_timeout,
        )

        # Progress tracking
        self.progress_file = self.output_dir / "progress.json"

    # ------------------------------------------------------------------
    # Progress management
    # ------------------------------------------------------------------

    def _load_progress(self) -> Dict:
        if self.progress_file.exists():
            try:
                return json.loads(self.progress_file.read_text())
            except Exception:
                pass
        return {"phase": 0, "completed_modules": [], "status": "pending"}

    def _save_progress(self, progress: Dict):
        self.progress_file.write_text(
            json.dumps(progress, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Repo cloning
    # ------------------------------------------------------------------

    def _clone_repo(self) -> Optional[Path]:
        """Clone the repo if not already provided."""
        if self.repo_path and self.repo_path.exists():
            self.logger.info(f"Using existing repo path: {self.repo_path}")
            return self.repo_path

        repo_dir = self.output_dir / "repo"
        if repo_dir.exists():
            self.logger.info(f"Repo already cloned: {repo_dir}")
            return repo_dir

        self.logger.info(f"Cloning {self.repo_name}...")
        clone_url = f"https://github.com/{self.repo_name}"
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            clone_url = f"https://{token}@github.com/{self.repo_name}"

        try:
            subprocess.run(
                ["git", "clone", "--recursive", "--depth=500", clone_url, str(repo_dir)],
                capture_output=True, text=True, timeout=900, check=True,
            )
            self.logger.info(f"Cloned to {repo_dir}")
            return repo_dir
        except Exception as e:
            self.logger.error(f"Clone failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    async def run(self) -> RepoResult:
        """
        Run the full per-repo pipeline.

        Returns RepoResult with all tasks (or reject reason).
        """
        self.logger.info("=" * 80)
        self.logger.info(f"Starting craft pipeline for {self.repo_name}")
        self.logger.info(f"Output: {self.output_dir}")
        self.logger.info(f"Model: {self.config.model}")
        self.logger.info(f"Actor models: {self.config.actor_models}")
        self.logger.info(f"Modules per repo: {self.config.modules_per_repo}")
        self.logger.info("=" * 80)

        start_time = time.time()
        repo_result = RepoResult(repo_name=self.repo_name)
        progress = self._load_progress()

        try:
            # === Clone repo ===
            repo_path = self._clone_repo()
            if not repo_path:
                repo_result.status = "failed"
                repo_result.reject_reason = "Clone failed"
                return repo_result
            self.repo_path = repo_path

            # === Phase 1: Repo Test Analysis ===
            self.logger.info("=" * 40 + " PHASE 1: Test Analysis " + "=" * 40)
            if progress.get("phase", 0) < 1:
                repo_analysis = await self.test_setup.analyze_repo(
                    repo_path=repo_path,
                    output_dir=self.output_dir,
                    log=self.logger,
                )
                if not repo_analysis:
                    repo_result.status = "failed"
                    repo_result.reject_reason = "Test analysis failed"
                    return repo_result

                if not repo_analysis.get("is_testable", False):
                    reason = repo_analysis.get("reject_reason", "Not testable")
                    self.logger.warning(f"Repo not testable: {reason}")
                    repo_result.status = "skipped"
                    repo_result.reject_reason = reason
                    return repo_result

                progress["phase"] = 1
                progress["repo_analysis"] = repo_analysis
                self._save_progress(progress)
            else:
                repo_analysis = progress.get("repo_analysis", {})
                self.logger.info("Phase 1 already completed, skipping")

            # === Phase 2: Test Setup & Validation ===
            self.logger.info("=" * 40 + " PHASE 2: Test Setup " + "=" * 40)
            if progress.get("phase", 0) < 2:
                run_tests_path = await self.test_setup.generate_and_validate_tests(
                    repo_path=repo_path,
                    repo_analysis=repo_analysis,
                    output_dir=self.output_dir,
                    log=self.logger,
                )
                if not run_tests_path:
                    repo_result.status = "failed"
                    repo_result.reject_reason = "Test setup failed"
                    return repo_result

                progress["phase"] = 2
                progress["run_tests_path"] = str(run_tests_path)
                self._save_progress(progress)
            else:
                run_tests_path = Path(progress.get("run_tests_path", ""))
                self.logger.info("Phase 2 already completed, skipping")

            # Ensure run-tests.sh is in repo
            repo_run_tests = repo_path / "run-tests.sh"
            if not repo_run_tests.exists() and run_tests_path.exists():
                shutil.copy2(run_tests_path, repo_run_tests)
                repo_run_tests.chmod(0o755)

            # === Phase 3: Module Mining ===
            self.logger.info("=" * 40 + " PHASE 3: Module Mining " + "=" * 40)
            if progress.get("phase", 0) < 3:
                minings = await self.module_miner.mine_modules(
                    repo_path=repo_path,
                    repo_analysis=repo_analysis,
                    run_tests_sh=repo_run_tests,
                    output_dir=self.output_dir,
                    log=self.logger,
                )
                if not minings:
                    repo_result.status = "failed"
                    repo_result.reject_reason = "No valid modules mined"
                    return repo_result

                progress["phase"] = 3
                progress["mined_modules"] = [m.module_info.module_name for m in minings]
                self._save_progress(progress)
            else:
                self.logger.info("Phase 3 already completed, loading from disk")
                minings = []  # Will be reconstructed from files if needed

            # === Phase 4 & 5: Per-module Query Generation + Actor Validation ===
            completed_modules = set(progress.get("completed_modules", []))

            for mining in minings:
                module_name = mining.module_info.module_name
                if module_name in completed_modules:
                    self.logger.info(f"Module {module_name} already processed, skipping")
                    continue

                self.logger.info("=" * 40 + f" MODULE: {module_name} " + "=" * 40)

                try:
                    task = await self._process_module(
                        mining=mining,
                        repo_path=repo_path,
                        run_tests_sh=repo_run_tests,
                        repo_analysis=repo_analysis,
                    )
                    repo_result.tasks.append(task)

                    # Update progress
                    completed_modules.add(module_name)
                    progress["completed_modules"] = list(completed_modules)
                    self._save_progress(progress)

                except asyncio.TimeoutError:
                    self.logger.error(f"Module {module_name} timed out")
                    repo_result.tasks.append(CraftTask(
                        task_id=f"{self.repo_name}:{module_name}",
                        repo_name=self.repo_name,
                        status="failed",
                    ))
                except Exception as e:
                    self.logger.error(f"Module {module_name} failed: {e}\n{traceback.format_exc()}")
                    repo_result.tasks.append(CraftTask(
                        task_id=f"{self.repo_name}:{module_name}",
                        repo_name=self.repo_name,
                        status="failed",
                    ))

            repo_result.status = "completed"

        except Exception as e:
            self.logger.error(f"Pipeline failed: {e}\n{traceback.format_exc()}")
            repo_result.status = "failed"
            repo_result.reject_reason = str(e)

        finally:
            elapsed = time.time() - start_time
            self.logger.info(f"Pipeline finished in {elapsed:.0f}s")
            self.logger.info(f"Status: {repo_result.status}")
            self.logger.info(f"Tasks: {len(repo_result.tasks)}")

            # Save repo summary
            self._save_summary(repo_result, elapsed)

            # Copy to final output if configured
            if self.config.copy_to_final:
                self._copy_to_final(Path(self.config.copy_to_final))

        return repo_result

    # ------------------------------------------------------------------
    # Per-module processing (Phase 4 + 5)
    # ------------------------------------------------------------------

    async def _process_module(
        self,
        mining: ModuleMining,
        repo_path: Path,
        run_tests_sh: Path,
        repo_analysis: Dict,
    ) -> CraftTask:
        """Process a single mined module through Phase 4 and 5."""
        module_name = mining.module_info.module_name
        task = CraftTask(
            task_id=f"{self.repo_name}:{module_name}",
            repo_name=self.repo_name,
            module_mining=mining,
            run_tests_sh=str(run_tests_sh),
        )

        # Phase 4: Query generation
        self.logger.info(f"Phase 4: Generating query for {module_name}")
        query_result = await asyncio.wait_for(
            self.query_crafter.craft_query(
                mining=mining,
                repo_path=repo_path,
                output_dir=self.output_dir,
                log=self.logger,
            ),
            timeout=self.config.module_timeout,
        )

        if not query_result:
            task.status = "failed"
            return task
        task.query_result = query_result

        # Phase 5: Actor validation (if actor models configured)
        if self.config.actor_models:
            self.logger.info(f"Phase 5: Actor validation for {module_name}")
            actor_results = await asyncio.wait_for(
                self.actor_validator.validate_module(
                    mining=mining,
                    query_result=query_result,
                    repo_path=repo_path,
                    run_tests_sh=run_tests_sh,
                    output_dir=self.output_dir,
                    log=self.logger,
                ),
                timeout=self.config.module_timeout,
            )
            task.actor_validations = [
                {
                    "model_name": r.model_name,
                    "run_index": r.run_index,
                    "pass_rate": r.pass_rate,
                    "tests_passed": r.tests_passed,
                    "tests_total": r.tests_total,
                    "exit_code": r.exit_code,
                }
                for r in actor_results
            ]

        # Package task folder
        self._package_task(task, mining, repo_path, run_tests_sh)

        task.status = "completed"
        return task

    # ------------------------------------------------------------------
    # Package task output folder
    # ------------------------------------------------------------------

    def _package_task(
        self,
        task: CraftTask,
        mining: ModuleMining,
        repo_path: Path,
        run_tests_sh: Path,
    ):
        """
        Package a completed task into a clean deliverable folder:
          task_{module_name}/
          ├── repo/              # base state repo (code removed)
          ├── code.patch         # unified diff to restore code (gold patch)
          ├── run-tests.sh       # test script
          └── query.txt          # task query
        """
        from module_miner import ModuleMiner

        module_name = mining.module_info.module_name
        task_dir = self.output_dir / f"task_{module_name}"
        task_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"Packaging task: {task_dir}")

        # 1. Copy repo and apply removals to create base state
        base_repo = task_dir / "repo"
        if base_repo.exists():
            shutil.rmtree(base_repo)
        try:
            shutil.copytree(repo_path, base_repo, symlinks=True)
            # Apply the same removal ranges to the copy
            ModuleMiner._remove_ranges(
                base_repo, mining.module_info.removal_ranges, self.logger
            )
            # Clean up .git to save space
            git_dir = base_repo / ".git"
            if git_dir.exists():
                for subdir in ["objects/pack"]:
                    pack_dir = git_dir / subdir
                    if pack_dir.exists():
                        shutil.rmtree(pack_dir)
                        pack_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.error(f"Failed to create base repo: {e}")

        # 2. Generate code.patch from removed_content
        patch_path = task_dir / "code.patch"
        try:
            patch_lines = []
            for entry in mining.removed_content:
                fpath = entry["file"]
                start = entry["start_line"]
                end = entry["end_line"]
                content = entry["content"]
                content_lines = content.split("\n")
                # Remove trailing empty line
                if content_lines and content_lines[-1] == "":
                    content_lines = content_lines[:-1]
                n_lines = len(content_lines)

                if entry.get("was_whole_file", False):
                    # New file patch
                    patch_lines.append(f"--- /dev/null")
                    patch_lines.append(f"+++ b/{fpath}")
                    patch_lines.append(f"@@ -0,0 +1,{n_lines} @@")
                else:
                    # Insert lines at specific position
                    patch_lines.append(f"--- a/{fpath}")
                    patch_lines.append(f"+++ b/{fpath}")
                    patch_lines.append(f"@@ -{start},0 +{start},{n_lines} @@")

                for line in content_lines:
                    patch_lines.append(f"+{line}")
                patch_lines.append("")

            patch_path.write_text("\n".join(patch_lines), encoding="utf-8")
            self.logger.info(f"Generated code.patch ({len(mining.removed_content)} ranges)")
        except Exception as e:
            self.logger.error(f"Failed to generate code.patch: {e}")

        # 3. Copy run-tests.sh
        task_run_tests = task_dir / "run-tests.sh"
        try:
            shutil.copy2(run_tests_sh, task_run_tests)
            task_run_tests.chmod(0o755)
        except Exception as e:
            self.logger.error(f"Failed to copy run-tests.sh: {e}")

        # 4. Write query.txt
        query_path = task_dir / "query.txt"
        try:
            if task.query_result and task.query_result.query:
                query_path.write_text(task.query_result.query, encoding="utf-8")
            else:
                query_path.write_text("", encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed to write query.txt: {e}")

        # 5. Write task metadata
        meta_path = task_dir / "task_meta.json"
        try:
            meta = {
                "task_id": task.task_id,
                "repo_name": task.repo_name,
                "module_name": module_name,
                "removal_ranges": [
                    {"file": e["file"], "start_line": e["start_line"],
                     "end_line": e["end_line"], "was_whole_file": e.get("was_whole_file", False)}
                    for e in mining.removed_content
                ],
                "query_score": task.query_result.score if task.query_result else 0,
                "base_state_valid": mining.base_state_valid,
                "target_state_valid": mining.target_state_valid,
            }
            if task.actor_validations:
                meta["actor_validations"] = task.actor_validations
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.logger.error(f"Failed to write task_meta.json: {e}")

        self.logger.info(f"Task packaged: {task_dir}")

    # ------------------------------------------------------------------
    # Summary and output
    # ------------------------------------------------------------------

    def _save_summary(self, result: RepoResult, elapsed: float):
        """Save repo summary to disk."""
        summary = {
            "repo_name": result.repo_name,
            "status": result.status,
            "reject_reason": result.reject_reason,
            "total_tasks": len(result.tasks),
            "completed_tasks": sum(1 for t in result.tasks if t.status == "completed"),
            "failed_tasks": sum(1 for t in result.tasks if t.status == "failed"),
            "elapsed_seconds": round(elapsed, 1),
            "tasks": [],
        }

        for task in result.tasks:
            task_info: Dict[str, Any] = {
                "task_id": task.task_id,
                "status": task.status,
            }
            if task.module_mining:
                task_info["module_name"] = task.module_mining.module_info.module_name
                task_info["base_valid"] = task.module_mining.base_state_valid
                task_info["target_valid"] = task.module_mining.target_state_valid
            if task.query_result:
                task_info["query_score"] = task.query_result.score
            if task.actor_validations:
                task_info["actor_validation_count"] = len(task.actor_validations)
            summary["tasks"].append(task_info)

        summary_file = self.output_dir / "repo_summary.json"
        summary_file.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Also append to global summary
        global_summary = Path(self.config.output_dir) / "craft_summary.jsonl"
        with open(global_summary, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")

        # Append rejects
        if result.status in ("skipped", "failed") and result.reject_reason:
            rejects_file = Path(self.config.output_dir) / "craft_rejects.jsonl"
            reject_record = {
                "repo_name": result.repo_name,
                "status": result.status,
                "reason": result.reject_reason,
            }
            with open(rejects_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(reject_record, ensure_ascii=False) + "\n")

    def _copy_to_final(self, final_dir: Path):
        """Copy output to final directory (e.g., mounted workspace)."""
        try:
            final_dir.mkdir(parents=True, exist_ok=True)
            safe_name = self.repo_name.replace("/", "_")
            dest = final_dir / safe_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(self.output_dir, dest)
            self.logger.info(f"Copied output to {dest}")
        except Exception as e:
            self.logger.error(f"Failed to copy to final: {e}")
