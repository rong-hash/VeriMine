"""
Actor Validator - Phase 5 of the new_feature_craft pipeline.

For each (actor_model x run):
  1. Copy repo, remove module files (base state)
  2. Give actor model the query + repo context
  3. Actor generates module code
  4. Run run-tests.sh
  5. Record pass rate
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from agents.claude_code_executor import ClaudeCodeExecutor, setup_logger
from models import ActorResult, ModuleMining, QueryResult
from test_setup import run_test_script

logger = logging.getLogger(__name__)

ACTOR_QUERY_TEMPLATE = """You are an expert RTL/Verilog engineer. A hardware project needs a new module implemented.

## Repository

The project is located at: {repo_path}

Please browse the repository to understand the existing codebase, design patterns, and coding style.

## Task

{query}

## Requirements

1. Implement the module described above
2. Write the implementation to the correct file path(s) in the repository
3. Make sure the implementation compiles and integrates with the existing codebase
4. Follow the existing coding style and conventions
5. The implementation should pass the existing tests

Please implement the module now. Write the file(s) directly.
"""


class ActorValidator:
    """Validates mined modules by having actor models implement them."""

    def __init__(
        self,
        actor_models: List[str],
        validation_runs: int = 4,
        test_timeout: int = 300,
    ):
        self.actor_models = actor_models
        self.validation_runs = validation_runs
        self.test_timeout = test_timeout

    # ------------------------------------------------------------------
    # Single validation run
    # ------------------------------------------------------------------

    async def _run_single_validation(
        self,
        actor_model: str,
        query_text: str,
        run_index: int,
        mining: ModuleMining,
        repo_path: Path,
        run_tests_sh: Path,
        output_dir: Path,
        log: logging.Logger,
    ) -> ActorResult:
        """
        Run a single actor validation:
        1. Create a working copy of the repo with module removed
        2. Let actor implement the module from query
        3. Run tests and record results
        """
        run_dir = output_dir / actor_model.replace("/", "_") / f"run_{run_index}"
        run_dir.mkdir(parents=True, exist_ok=True)

        result = ActorResult(
            model_name=actor_model,
            run_index=run_index,
        )

        # Create working copy
        work_repo = run_dir / "repo"
        if work_repo.exists():
            shutil.rmtree(work_repo)

        try:
            shutil.copytree(repo_path, work_repo, symlinks=True)
        except Exception as e:
            log.error(f"Failed to copy repo: {e}")
            return result

        # Remove module files (create base state)
        for fpath in mining.removed_files:
            target = work_repo / fpath
            if target.exists():
                target.unlink()
                log.info(f"Removed from working copy: {fpath}")

        # Copy run-tests.sh to working copy
        work_run_tests = work_repo / "run-tests.sh"
        shutil.copy2(run_tests_sh, work_run_tests)
        work_run_tests.chmod(0o755)

        # Run actor model
        prompt = ACTOR_QUERY_TEMPLATE.format(
            repo_path=work_repo,
            query=query_text,
        )

        executor = ClaudeCodeExecutor(
            work_dir=work_repo,
            output_dir=run_dir,
            model=actor_model,
            task_name=f"actor_run{run_index}",
            logger=log,
        )

        try:
            await executor.execute(
                query=prompt,
                timeout=25200,  # 7 hours per actor run
            )
        except Exception as e:
            log.error(f"Actor execution failed: {e}")
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

        # Run tests
        log.info(f"Running tests after actor implementation...")
        test_result = await run_test_script(
            repo_path=work_repo,
            run_tests_sh=work_run_tests,
            timeout=self.test_timeout,
            log=log,
        )

        # Save test result
        test_result_file = run_dir / "test_result.json"
        test_result_file.write_text(
            json.dumps(test_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        result.exit_code = test_result["exit_code"]
        result.tests_passed = test_result.get("passed", 0)
        result.tests_total = test_result.get("total", 0)
        result.pass_rate = test_result.get("pass_rate", 0.0)

        log.info(f"Actor {actor_model} | run {run_index}: "
                 f"pass_rate={result.pass_rate:.2f} "
                 f"({result.tests_passed}/{result.tests_total})")

        # Clean up working copy to save disk space
        try:
            shutil.rmtree(work_repo)
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # Validate all runs for one module
    # ------------------------------------------------------------------

    async def validate_module(
        self,
        mining: ModuleMining,
        query_result: QueryResult,
        repo_path: Path,
        run_tests_sh: Path,
        output_dir: Path,
        log: logging.Logger,
    ) -> List[ActorResult]:
        """
        Run actor validation for all (model x run) combinations.
        """
        module_dir = output_dir / f"module_{mining.module_info.module_name}"
        validation_dir = module_dir / "actor_validation"
        validation_dir.mkdir(parents=True, exist_ok=True)

        all_results: List[ActorResult] = []
        query_text = query_result.query

        if not query_text:
            log.warning("No query text, skipping actor validation")
            return all_results

        for actor_model in self.actor_models:
            for run_idx in range(self.validation_runs):
                log.info(f"Validation: {actor_model} | run {run_idx}")
                try:
                    result = await self._run_single_validation(
                        actor_model=actor_model,
                        query_text=query_text,
                        run_index=run_idx,
                        mining=mining,
                        repo_path=repo_path,
                        run_tests_sh=run_tests_sh,
                        output_dir=validation_dir,
                        log=log,
                    )
                    all_results.append(result)
                except Exception as e:
                    log.error(f"Validation failed: {actor_model}/run{run_idx}: {e}")
                    all_results.append(ActorResult(
                        model_name=actor_model,
                        run_index=run_idx,
                    ))

        # Generate summary
        summary = self._summarize_results(all_results)
        summary_file = validation_dir / "validation_summary.json"
        summary_file.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        log.info(f"Actor validation complete: {len(all_results)} runs")
        return all_results

    @staticmethod
    def _summarize_results(results: List[ActorResult]) -> Dict:
        """Aggregate actor validation results by model."""
        summary: Dict = {"by_model": {}, "overall": {}}

        if not results:
            return summary

        for r in results:
            key = r.model_name
            if key not in summary["by_model"]:
                summary["by_model"][key] = {"runs": 0, "total_pass_rate": 0.0}
            summary["by_model"][key]["runs"] += 1
            summary["by_model"][key]["total_pass_rate"] += r.pass_rate

        for key, val in summary["by_model"].items():
            if val["runs"] > 0:
                val["avg_pass_rate"] = val["total_pass_rate"] / val["runs"]

        total_runs = len(results)
        total_pass = sum(r.pass_rate for r in results)
        summary["overall"] = {
            "total_runs": total_runs,
            "avg_pass_rate": total_pass / total_runs if total_runs > 0 else 0.0,
        }

        return summary
