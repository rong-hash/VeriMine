"""
Query Crafter - Phase 4 of the new_feature_craft pipeline.

Generates a single query per mined module, validates alignment with
golden patch + tests, refines if needed, then evaluates quality.
All in 1 executor per module.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from agents.claude_code_executor import ClaudeCodeExecutor
from models import ModuleMining, QueryResult

logger = logging.getLogger(__name__)


class QueryCrafter:
    """Generates and evaluates a query for each mined module."""

    def __init__(
        self,
        model: str,
        quality_threshold: float = 6.5,
        max_query_refine: int = 2,
    ):
        self.model = model
        self.quality_threshold = quality_threshold
        self.max_query_refine = max_query_refine

        # Load prompts
        prompts_dir = Path(__file__).parent / "config" / "prompts"
        self.generate_prompt = self._load_prompt(prompts_dir / "generate_query.txt")
        self.alignment_prompt = self._load_prompt(prompts_dir / "validate_query_alignment.txt")
        self.quality_prompt = self._load_prompt(prompts_dir / "quality_evaluation.txt")

    @staticmethod
    def _load_prompt(path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning(f"Prompt not found: {path}")
        return ""

    # ------------------------------------------------------------------
    # Phase 4: Generate query (1 executor per module, all-in-one)
    # ------------------------------------------------------------------

    async def craft_query(
        self,
        mining: ModuleMining,
        repo_path: Path,
        output_dir: Path,
        log: logging.Logger,
    ) -> Optional[QueryResult]:
        """
        For one module: generate query → alignment check → refine → evaluate.
        Single executor, single conversation throughout.
        """
        module_dir = output_dir / f"module_{mining.module_info.module_name}"
        module_dir.mkdir(parents=True, exist_ok=True)

        qr = QueryResult(module_name=mining.module_info.module_name)

        golden_patch = self._build_golden_patch(mining)
        test_files_info = self._build_test_files_info(mining)

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=module_dir,
            model=self.model,
            task_name=f"query_{mining.module_info.module_name}",
            logger=log,
        )

        try:
            # Step 1: Generate query
            prompt = self.generate_prompt.format(
                module_name=mining.module_info.module_name,
                file_path=mining.module_info.file_path,
                repo_path=repo_path,
            )

            result = await executor.execute_with_json_retry(
                query=prompt,
                timeout=18000,
                max_retries=3,
                must_include_keys=["query"],
            )

            if not result or not result.get("query"):
                log.error("Failed to generate query")
                return None

            query_text = result["query"]
            log.info(f"Generated query: {len(query_text)} chars")

            # Step 2: Alignment check + refine
            query_text = await self._align_and_refine(
                executor=executor,
                query_text=query_text,
                mining=mining,
                golden_patch=golden_patch,
                test_files_info=test_files_info,
                output_dir=module_dir,
                log=log,
            )

            # Save final query
            query_file = module_dir / "query.md"
            query_file.write_text(query_text, encoding="utf-8")
            qr.query = query_text

            # Step 3: Evaluate quality (same executor, continue conversation)
            eval_prompt = self.quality_prompt.format(
                query_level="detailed",
                module_name=mining.module_info.module_name,
                repo_path=repo_path,
                query=query_text,
                quality_threshold=self.quality_threshold,
            )

            eval_result = await executor.execute_with_json_retry(
                query=eval_prompt,
                continue_conversation=True,
                timeout=18000,
                max_retries=3,
                must_include_keys=["overall_score"],
            )

            if eval_result:
                qr.score = float(eval_result.get("overall_score", 0))
                log.info(f"Quality score: {qr.score}/10")

            # Save score
            score_file = module_dir / "quality_score.json"
            score_file.write_text(json.dumps({
                "module_name": qr.module_name,
                "score": qr.score,
                "quality_threshold": self.quality_threshold,
            }, indent=2, ensure_ascii=False), encoding="utf-8")

            if qr.score < self.quality_threshold:
                log.warning(f"Query score {qr.score} below threshold {self.quality_threshold}")

        except Exception as e:
            log.error(f"Query crafting failed: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

        return qr

    # ------------------------------------------------------------------
    # Alignment check + refine (same executor session)
    # ------------------------------------------------------------------

    async def _align_and_refine(
        self,
        executor: ClaudeCodeExecutor,
        query_text: str,
        mining: ModuleMining,
        golden_patch: str,
        test_files_info: str,
        output_dir: Path,
        log: logging.Logger,
    ) -> str:
        """
        Check query alignment with golden patch + tests.
        If misaligned, refine in the same conversation.
        """
        for refine_round in range(1 + self.max_query_refine):
            label = "initial" if refine_round == 0 else f"refine_{refine_round}"

            alignment_prompt = self.alignment_prompt.format(
                query_level="detailed",
                module_name=mining.module_info.module_name,
                query=query_text,
                golden_patch=golden_patch,
                test_files_info=test_files_info,
            )

            alignment = await executor.execute_with_json_retry(
                query=alignment_prompt,
                continue_conversation=True,
                timeout=18000,
                max_retries=2,
                must_include_keys=["aligned"],
            )

            if alignment:
                alignment_file = output_dir / f"alignment_{label}.json"
                alignment_file.write_text(
                    json.dumps(alignment, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            if not alignment or alignment.get("aligned", False):
                if alignment and alignment.get("aligned"):
                    log.info(f"Query alignment OK ({label})")
                else:
                    log.warning(f"Alignment check failed ({label}), keeping query")
                break

            if refine_round >= self.max_query_refine:
                log.warning(f"Query still misaligned after {self.max_query_refine} refines")
                break

            log.warning(f"Query misaligned ({label}), refining... "
                        f"({refine_round + 1}/{self.max_query_refine})")

            missing = alignment.get("missing_features", [])
            missing_if = alignment.get("missing_interfaces", [])
            mismatches = alignment.get("test_mismatches", [])
            suggestion = alignment.get("suggestion", "")

            refine_prompt = "Query 和 golden patch / test 不完全吻合，需要修改。\n\n问题:\n"
            if missing:
                refine_prompt += f"- 遗漏的功能: {', '.join(missing)}\n"
            if missing_if:
                refine_prompt += f"- 遗漏的接口: {', '.join(missing_if)}\n"
            if mismatches:
                refine_prompt += f"- 测试不匹配: {', '.join(mismatches)}\n"
            if suggestion:
                refine_prompt += f"\n修改建议: {suggestion}\n"
            refine_prompt += (
                "\n请基于以上反馈修改 query，确保覆盖所有功能点和接口。\n"
                "注意：不要泄露内部实现细节，只描述规格/需求。\n\n"
                "输出修改后的 query：\n"
                "```json\n{\"query\": \"...\"}\n```"
            )

            refine_result = await executor.execute_with_json_retry(
                query=refine_prompt,
                continue_conversation=True,
                timeout=18000,
                max_retries=2,
                must_include_keys=["query"],
            )

            if refine_result and refine_result.get("query"):
                query_text = refine_result["query"]
                log.info(f"Refined query: {len(query_text)} chars")
            else:
                log.warning("Refine failed, keeping previous query")
                break

        return query_text

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_golden_patch(mining: ModuleMining) -> str:
        golden_patch = ""
        for fpath, content in mining.module_content.items():
            header = f"=== {fpath} ===\n"
            if len(golden_patch) + len(header) + len(content) > 8000:
                golden_patch += header + content[:2000] + "\n...(truncated)\n"
            else:
                golden_patch += header + content + "\n"
        return golden_patch

    @staticmethod
    def _build_test_files_info(mining: ModuleMining) -> str:
        if mining.module_info.test_files:
            return "Test files: " + ", ".join(mining.module_info.test_files)
        return "Test files: (see repo testbench directory)"
