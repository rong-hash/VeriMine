#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import sys
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def setup_logger(log_file: Path, name: str = "") -> logging.Logger:
    logger_name = f"cc_{name}" if name else "cc"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    log_file.parent.mkdir(parents=True, exist_ok=True)
    # Enable line buffering for immediate flush
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    # Force flush after each log record
    file_handler.stream.reconfigure(line_buffering=True)

    # 添加 [name] 前缀到格式中
    prefix = f"[{name}] " if name else ""
    file_formatter = logging.Formatter(
        f"%(asctime)s - {prefix}%(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        f"%(asctime)s - {prefix}%(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


class SessionManager:
    """
    Manages Claude Code session persistence

    Claude Code automatically saves sessions to:
    ~/.claude/projects/<encoded-work-dir>/<session-id>.jsonl

    This manager helps:
    1. Save session files to custom locations
    2. Restore sessions from backups
    3. Track session IDs across operations
    """

    def __init__(self, work_dir: Path, logger: logging.Logger | None = None):
        """
        Initialize SessionManager

        Args:
            work_dir: Working directory where Claude Code is executing
            logger: Optional logger for output
        """
        self.work_dir = work_dir
        self.logger = logger or logging.getLogger(__name__)
        # Claude projects directory
        self.projects_dir = Path.home() / ".claude" / "projects"
        # Calculate encoded path for this work_dir
        self.session_dir = self.projects_dir / self._encode_path(work_dir)

    def _encode_path(self, path: Path) -> str:
        """
        Encode path to Claude's format
        Claude replaces / with -, e.g., /home/user -> -home-user
        """
        return str(path).replace("/", "-")

    def save_session(
        self,
        session_id: str,
        backup_dir: Path,
        metadata: Optional[dict] = None
    ) -> Optional[Path]:
        """
        Save session file to backup directory

        Args:
            session_id: Claude session ID (UUID)
            backup_dir: Directory to save session backup
            metadata: Optional metadata to save alongside session

        Returns:
            Path to saved session file, or None if failed
        """
        # Find session file in Claude's projects directory
        session_file = self.session_dir / f"{session_id}.jsonl"

        if not session_file.exists():
            self.logger.warning(f"Session file not found: {session_file}")
            self.logger.warning(f"Session may not have been created yet")
            return None

        # Create backup directory
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Copy session file
        backup_file = backup_dir / f"{session_id}.jsonl"
        try:
            shutil.copy2(session_file, backup_file)
            self.logger.info(f"✓ Session saved to: {backup_file}")

            # Save metadata if provided
            if metadata:
                metadata_file = backup_dir / f"{session_id}_metadata.json"
                with open(metadata_file, "w") as f:
                    json.dump(metadata, f, indent=2, ensure_ascii=False)
                self.logger.info(f"✓ Session metadata saved")

            # Save session info summary
            session_info = {
                "session_id": session_id,
                "work_dir": str(self.work_dir),
                "session_file": str(session_file),
                "backup_file": str(backup_file),
                "file_size": session_file.stat().st_size,
                "line_count": self._count_lines(session_file),
            }
            if metadata:
                session_info["metadata"] = metadata

            info_file = backup_dir / "session_info.json"
            with open(info_file, "w") as f:
                json.dump(session_info, f, indent=2, ensure_ascii=False)

            return backup_file

        except Exception as e:
            self.logger.error(f"✗ Failed to save session: {e}")
            return None

    def restore_session(self, session_id: str, backup_dir: Path) -> bool:
        """
        Restore session from backup directory

        Args:
            session_id: Session ID to restore
            backup_dir: Directory containing session backup

        Returns:
            True if successful, False otherwise
        """
        backup_file = backup_dir / f"{session_id}.jsonl"

        if not backup_file.exists():
            self.logger.error(f"Backup file not found: {backup_file}")
            return False

        # Create session directory if needed
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Copy session file back
        session_file = self.session_dir / f"{session_id}.jsonl"
        try:
            shutil.copy2(backup_file, session_file)
            self.logger.info(f"✓ Session restored from: {backup_file}")
            return True
        except Exception as e:
            self.logger.error(f"✗ Failed to restore session: {e}")
            return False

    def _count_lines(self, file_path: Path) -> int:
        """Count lines in a file"""
        try:
            with open(file_path, "r") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def get_session_info(self, session_id: str) -> Optional[dict]:
        """
        Get information about a session

        Args:
            session_id: Session ID to query

        Returns:
            Dictionary with session info, or None if not found
        """
        session_file = self.session_dir / f"{session_id}.jsonl"

        if not session_file.exists():
            return None

        return {
            "session_id": session_id,
            "work_dir": str(self.work_dir),
            "session_file": str(session_file),
            "file_size": session_file.stat().st_size,
            "line_count": self._count_lines(session_file),
            "modified_time": session_file.stat().st_mtime,
        }


class ClaudeCodeExecutor:
    def __init__(
        self,
        work_dir: Path,
        output_dir: Path | None = None,
        task_name: str = "execution",
        logger: logging.Logger | None = None,
        model: str | None = None,
        fork_session: bool = False,
        resume: str | None = None,
    ):
        self.work_dir = work_dir
        self.task_name = task_name
        self.output_dir = output_dir if output_dir is not None else work_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Get run_mode from environment variable
        run_mode = os.environ.get("CLAUDE_CODE_RUN_MODE", "local")
        if run_mode not in ("local", "remote"):
            raise ValueError(
                f"CLAUDE_CODE_RUN_MODE must be 'local' or 'remote', got: {run_mode}"
            )

        self.model = model or os.environ.get("ANTHROPIC_MODEL", "")
        assert self.model, "ANTHROPIC_MODEL environment variable is required"
        # Configure model environment
        env = self._get_model_env(self.model, run_mode)

        options = ClaudeAgentOptions(
            cwd=work_dir,
            setting_sources=["project"],
            max_budget_usd=100.0,
            max_turns=200,
            permission_mode="bypassPermissions",
            env=env,
            fork_session=fork_session,
            resume=resume,
        )

        self.client = ClaudeSDKClient(options=options)
        self.connected = False
        self.session_id = None

        # Initialize session manager for session persistence
        self.session_manager = None  # Will be initialized after logger is set

        if logger:
            self.logger = logger
            # Add model prefix to existing logger's formatters (only if not already present)
            for handler in self.logger.handlers:
                if handler.formatter:
                    old_fmt = handler.formatter._fmt
                    # Check if model prefix already exists to avoid duplication
                    if not old_fmt.startswith(f"[{self.model}]"):
                        new_fmt = f"[{self.model}] {old_fmt}"
                        handler.setFormatter(
                            logging.Formatter(
                                new_fmt, datefmt=handler.formatter.datefmt
                            )
                        )
        else:
            self.logger = setup_logger(
                self.output_dir / f"{task_name}.log", f"{self.model}-{task_name}"
            )

        # Initialize session manager after logger is set
        self.session_manager = SessionManager(work_dir, self.logger)

    def _get_model_env(self, model: str, run_mode: str = "local") -> dict:
        """Get environment variables for model configuration"""
        env = {
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
        }

        # Configure base URL based on run_mode
        import os

        is_claude_model = (
            "sonnet" in model.lower()
            or "opus" in model.lower()
            or "haiku" in model.lower()
        )
        if run_mode == "remote":
            # Remote: Claude models use remote URL directly, non-Claude use local proxy
            if is_claude_model:
                # Use remote URL directly (no local proxy needed)
                env["ANTHROPIC_BASE_URL"] = "https://openai.app.msh.team/raw/msh-claude-code--chenzhirong/"
                env["ANTHROPIC_API_KEY"] = os.environ["QIANXUN_API_KEY"]
            else:
                env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8080/anthropic"
                # For non-Claude models, use sk-empty (matches anthropic-openai-compatible-server config in entrypoint.sh)
                env["ANTHROPIC_API_KEY"] = "sk-empty"
        else:  # local
            if is_claude_model:
                env[
                    "ANTHROPIC_BASE_URL"
                ] = f"https://openai.app.msh.team/raw/msh-claude-code--{os.environ.get('USER', 'unknown')}/"
                env["ANTHROPIC_API_KEY"] = os.environ.get("QIANXUN_API_KEY", "")
            else:
                # env["ANTHROPIC_CUSTOM_HEADERS"] = "x-msh-internal-anthropic-model-context-window:200000 x-msh-internal-anthropic-model-temperature:1.0"
                # env["ANTHROPIC_BASE_URL"] = "https://api-staff.msh.team/anthropic/"
                # env["MOONSHOT_STAFF_KEY"] = os.environ["MOONSHOT_STAFF_KEY"]
                # 需要先用anthropic-openai-compatible-server起好服务
                # env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8080/anthropic"
                env["ANTHROPIC_BASE_URL"] = "https://openai.app.msh.team/"
                env["ANTHROPIC_API_KEY"] = os.environ.get("QIANXUN_API_KEY", "")

        # Inherit Python environment variables for Bash commands
        for key in ["PATH", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"]:
            if key in os.environ:
                env[key] = os.environ[key]

        return env

    async def _ensure_connected(self):
        if not self.connected:
            await self.client.connect()
            self.connected = True

    async def execute(
        self,
        query: str,
        continue_conversation: bool = False,
        timeout: float = 36000,
        task_name: str | None = None,
    ) -> dict:
        await self._ensure_connected()

        # Use custom logger if task_name is provided
        logger = self.logger
        if task_name:
            logger = setup_logger(
                self.output_dir / f"{task_name}.log", f"{self.model}-{task_name}"
            )

        logger.info(f"Query: {query[:200]}... (continue: {continue_conversation})")

        async def _execute_with_timeout():
            self.client.options.continue_conversation = continue_conversation
            await self.client.query(query)

            messages = []
            final_result = ""

            async for message in self.client.receive_response():
                if isinstance(message, SystemMessage):
                    message_data = {
                        "role": "system",
                        "content": vars(message)
                        if hasattr(message, "__dict__")
                        else str(message),
                    }
                    messages.append(message_data)
                    messages.append({"role": "user", "content": query})
                elif isinstance(message, ResultMessage):
                    final_result = message.result
                    self.session_id = message.session_id  # Capture session ID
                    message_data = {
                        "role": "result",
                        "content": vars(message)
                        if hasattr(message, "__dict__")
                        else str(message),
                    }
                    messages.append(message_data)
                else:
                    content_blocks = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            logger.info(f"[Assistant] {block.text[:200]}...")
                            block_data = {"type": "text", "text": block.text}
                        elif isinstance(block, ThinkingBlock):
                            logger.info(f"[Thinking] {block.thinking[:200]}...")
                            block_data = {
                                "type": "thinking",
                                "thinking": block.thinking,
                            }
                        elif isinstance(block, ToolUseBlock):
                            logger.info(
                                f"[Tool: {block.name}] {str(block.input)[:100]}..."
                            )
                            block_data = {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        elif isinstance(block, ToolResultBlock):
                            logger.info(f"[Tool Result]: {str(block.content)[:100]}...")
                            block_data = {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id,
                                "content": str(block.content),
                                "is_error": block.is_error,
                            }
                        content_blocks.append(block_data)

                    parent_tool_use_id = message.parent_tool_use_id
                    if isinstance(message, UserMessage):
                        message_data = {
                            "role": "user",
                            "parent_tool_use_id": parent_tool_use_id,
                            "content": content_blocks,
                        }
                        if (
                            len(content_blocks) == 1
                            and content_blocks[0]["type"] == "tool_result"
                        ):
                            message_data["role"] = "assistant"
                    elif isinstance(message, AssistantMessage):
                        message_data = {
                            "role": "assistant",
                            "parent_tool_use_id": parent_tool_use_id,
                            "model": message.model,
                            "content": content_blocks,
                        }
                    messages.append(message_data)

            # Group messages by parent_tool_use_id
            agent_messages = []
            subagent_groups = {}

            for msg in messages:
                parent_id = msg.get("parent_tool_use_id")
                if parent_id is None:
                    agent_messages.append(msg)
                else:
                    if parent_id not in subagent_groups:
                        subagent_groups[parent_id] = []
                    subagent_groups[parent_id].append(msg)

            trajectory = {
                "result": final_result,
                "agent": {"messages": agent_messages},
            }

            for parent_id, subagent_msgs in subagent_groups.items():
                trajectory[f"subagent-{parent_id}"] = {"messages": subagent_msgs}

            # Save trajectory and result
            self.output_dir.mkdir(parents=True, exist_ok=True)

            trajectory_file = self.output_dir / "trajectory.json"
            with open(trajectory_file, "w", encoding="utf-8") as f:
                json.dump(trajectory, f, indent=2, ensure_ascii=False)

            result_file = self.output_dir / "result.md"
            with open(result_file, "w", encoding="utf-8") as f:
                f.write(final_result)

            return trajectory

        # Execute with timeout
        try:
            return await asyncio.wait_for(_execute_with_timeout(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Execution timed out after {timeout} seconds")
            raise

    def _parse_json_from_text(
        self, result_text: str
    ) -> tuple[Optional[Dict], Optional[str]]:
        """
        从文本中解析JSON

        Args:
            result_text: 需要解析的文本

        Returns:
            (解析后的字典, 错误信息)，如果成功返回(dict, None)，失败返回(None, error_msg)
        """
        try:
            # 尝试从markdown代码块中提取JSON
            json_match = re.search(r"```json\n([\s\S]*?)\n```", result_text)
            if not json_match:
                # 如果没有代码块，尝试提取整个JSON对象
                json_match = re.search(r"\{[\s\S]*\}", result_text)

            if not json_match:
                return None, "No JSON content found in output"

            json_str = (
                json_match.group(1) if json_match.lastindex else json_match.group(0)
            )
            parsed = json.loads(json_str)
            return parsed, None

        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e.msg} at line {e.lineno} col {e.colno}."
        except Exception as e:
            return None, f"Unexpected error: {str(e)}"

    async def execute_with_json_retry(
        self,
        query: str,
        continue_conversation: bool = False,
        timeout: float = 36000,
        max_retries: int = 5,
        must_include_keys: Optional[List[str]] = None,
        task_name: str | None = None,
    ) -> Optional[Dict]:
        """
        执行query并解析JSON，支持自动重试

        Args:
            query: 要执行的查询
            continue_conversation: 是否继续对话
            timeout: 执行超时时间
            max_retries: 最大重试次数
            must_include_keys: 必须包含的键列表
            task_name: 可选的任务名称，用于创建自定义logger

        Returns:
            解析后的字典，如果失败返回None
        """
        import time

        # Use custom logger if task_name is provided
        logger = self.logger
        if task_name:
            logger = setup_logger(
                self.output_dir / f"{task_name}.log", f"{self.model}-{task_name}"
            )

        original_query = query

        for attempt in range(max_retries):
            continue_conversation = True if attempt > 0 else continue_conversation
            try:
                logger.info(f"Execution attempt {attempt + 1}/{max_retries}")
                result = await self.execute(
                    query, continue_conversation, timeout, task_name
                )
                result_text = result.get("result", "")

                # 尝试解析JSON
                data, error_msg = self._parse_json_from_text(result_text)
                if data is None:
                    logger.warning(
                        f"Attempt {attempt + 1}: Failed to parse JSON - {error_msg}"
                    )
                    if attempt < max_retries - 1:
                        logger.info(
                            "Retrying with error details and format reminder..."
                        )
                        query = f"Previous output failed to parse: {error_msg}\n\nYou MUST output the required valid JSON in the format:\n```json\n{{...}}\n```"
                    continue

                # 验证必需的键
                if must_include_keys:
                    missing_keys = [key for key in must_include_keys if key not in data]
                    if missing_keys:
                        logger.warning(
                            f"Attempt {attempt + 1}: Missing required keys: {missing_keys}"
                        )
                        if attempt < max_retries - 1:
                            logger.info("Retrying with reminder about required keys...")
                            keys_str = ", ".join(must_include_keys)
                            # query = f"{original_query}\n\n⚠️ CRITICAL: You MUST include the required valid JSON with these keys: {keys_str}."
                            query = f"You MUST output the required valid JSON in the format:\n```json\n{{...}}\n```\nMake sure to include all required keys: {keys_str}"
                        continue

                logger.info(
                    f"✓ Successfully parsed and validated JSON on attempt {attempt + 1}"
                )
                return data

            except Exception as e:
                error_msg = str(e)
                logger.error(f"Attempt {attempt + 1}: Execution error: {e}")

                # 🆕 判断是否是临时性错误（网络、超时、服务不可用）
                is_temporary_error = any([
                    "timed out" in error_msg.lower(),
                    "timeout" in error_msg.lower(),
                    "connection" in error_msg.lower(),
                    "503" in error_msg,  # Service Unavailable
                    "502" in error_msg,  # Bad Gateway
                    "504" in error_msg,  # Gateway Timeout
                    "overloaded" in error_msg.lower(),
                    "rate limit" in error_msg.lower()
                ])

                # 🆕 如果是临时性错误，使用指数退避重试
                if is_temporary_error and attempt < max_retries - 1:
                    # 指数退避：2秒、4秒、8秒、16秒
                    wait_time = min(2 ** attempt, 30)  # 最多等待30秒
                    logger.warning(f"⚠️ 检测到临时性错误，{wait_time}秒后重试...")
                    logger.warning(f"错误详情: {error_msg}")
                    time.sleep(wait_time)
                    # 重置query为原始query（不要继续使用之前的错误提示）
                    query = original_query
                    continue

                # 🆕 如果是最后一次重试，记录详细错误信息但不抛出异常
                if attempt >= max_retries - 1:
                    logger.error(f"❌ 所有重试均失败 ({max_retries} 次)")
                    logger.error(f"最后一次错误: {error_msg}")
                    # 不抛出异常，返回None让调用者决定如何处理
                    return None

        logger.error(f"Failed to get valid JSON after {max_retries} attempts")
        return None

    async def disconnect(self):
        if self.connected:
            await self.client.disconnect()
            self.connected = False

    # =========================================================================
    # Session Management Methods
    # =========================================================================

    def save_session_to(self, backup_dir: Path, metadata: Optional[dict] = None, retry_count: int = 3, retry_delay: float = 0.5) -> Optional[Path]:
        """
        Save current session to specified directory

        Args:
            backup_dir: Directory to save session backup
            metadata: Optional metadata to include (e.g., turn info, query, etc.)
            retry_count: Number of retries if session file not found (default: 3)
            retry_delay: Delay between retries in seconds (default: 0.5)

        Returns:
            Path to saved session file, or None if failed

        Example:
            ```python
            executor = ClaudeCodeExecutor(work_dir=repo_path)
            result = await executor.execute("your query")

            # Save session with metadata
            session_file = executor.save_session_to(
                backup_dir=Path("./turn_1"),
                metadata={"turn": 1, "query": "your query"}
            )
            ```
        """
        if not self.session_id:
            self.logger.warning("No session ID available, cannot save session")
            return None

        import time

        # Retry mechanism for session file availability
        for attempt in range(retry_count):
            result = self.session_manager.save_session(
                session_id=self.session_id,
                backup_dir=backup_dir,
                metadata=metadata
            )

            if result is not None:
                return result

            # If failed and not last attempt, wait and retry
            if attempt < retry_count - 1:
                self.logger.info(f"Session file not ready, retrying in {retry_delay}s... (attempt {attempt + 1}/{retry_count})")
                time.sleep(retry_delay)

        self.logger.warning(f"Failed to save session after {retry_count} attempts")
        return None

    def restore_session_from(self, backup_dir: Path, session_id: str) -> bool:
        """
        Restore session from backup directory

        Note: This should be called BEFORE creating the executor with resume parameter.
        The typical workflow is:
        1. Call this static method to restore session file to Claude's directory
        2. Create executor with resume=session_id

        Args:
            backup_dir: Directory containing session backup
            session_id: Session ID to restore

        Returns:
            True if successful, False otherwise

        Example:
            ```python
            # Restore session from previous turn
            SessionManager(work_dir).restore_session(
                session_id="previous-session-id",
                backup_dir=Path("./turn_1")
            )

            # Create executor with restored session
            executor = ClaudeCodeExecutor(
                work_dir=repo_path,
                resume="previous-session-id"
            )
            ```
        """
        return self.session_manager.restore_session(
            session_id=session_id,
            backup_dir=backup_dir
        )

    def get_session_info(self) -> Optional[dict]:
        """
        Get information about current session

        Returns:
            Dictionary with session info, or None if no session

        Example:
            ```python
            info = executor.get_session_info()
            if info:
                print(f"Session: {info['session_id']}")
                print(f"Size: {info['file_size']} bytes")
                print(f"Lines: {info['line_count']}")
            ```
        """
        if not self.session_id:
            return None

        return self.session_manager.get_session_info(self.session_id)


# =============================================================================
# Usage Examples
# =============================================================================


async def example_simple_execution():
    """Simple example: execute a single query"""
    # Setup
    work_dir = Path("/tmp/example_task")
    work_dir.mkdir(exist_ok=True)

    # Create executor (logger created automatically)
    executor = ClaudeCodeExecutor(work_dir, task_name="demo")

    try:
        # Execute query
        query = "List all Python files in the current directory"
        result = await executor.execute(query)

        # Access result
        print(f"Result: {result['result']}")
        print(f"Messages: {len(result['agent']['messages'])} agent messages")

    finally:
        await executor.disconnect()


async def example_continued_conversation():
    """Example: execute multiple queries in the same conversation"""
    work_dir = Path("/tmp/example_task")
    work_dir.mkdir(exist_ok=True)

    executor = ClaudeCodeExecutor(work_dir, task_name="conversation")

    try:
        # First query
        result1 = await executor.execute(
            "Create a file called test.txt with 'Hello World'",
            continue_conversation=False,
        )

        # Continue the conversation
        result2 = await executor.execute(
            "Now append 'Second line' to test.txt", continue_conversation=True
        )

        print(f"Task 1 result: {result1['result']}")
        print(f"Task 2 result: {result2['result']}")

    finally:
        await executor.disconnect()


async def example_with_custom_logger():
    """Example: use a custom logger"""
    work_dir = Path("/tmp/example_task")
    work_dir.mkdir(exist_ok=True)

    # Custom logger
    custom_logger = logging.getLogger("custom")
    custom_logger.setLevel(logging.INFO)

    executor = ClaudeCodeExecutor(work_dir, task_name="custom", logger=custom_logger)

    try:
        result = await executor.execute("Write a simple Python hello world script")
        print(f"Result: {result['result']}")

    finally:
        await executor.disconnect()


async def example_with_session_management():
    """Example: Save and restore sessions across multiple executions"""
    work_dir = Path("/tmp/example_task")
    work_dir.mkdir(exist_ok=True)

    # === First execution - create and save session ===
    print("=== First Execution ===")
    executor1 = ClaudeCodeExecutor(work_dir, task_name="session_demo")

    try:
        # Execute first query
        result1 = await executor1.execute("Create a file called data.txt with some content")
        print(f"Result 1: {result1['result'][:100]}...")

        # Save session to turn_1 directory
        session_id = executor1.session_id
        print(f"Session ID: {session_id}")

        session_file = executor1.save_session_to(
            backup_dir=work_dir / "turn_1",
            metadata={
                "turn": 1,
                "query": "Create a file called data.txt",
                "timestamp": str(Path(work_dir).stat().st_mtime)
            }
        )
        print(f"Session saved to: {session_file}")

    finally:
        await executor1.disconnect()

    # === Second execution - restore and continue ===
    print("\n=== Second Execution (Restoring Session) ===")

    # Restore session from turn_1
    session_mgr = SessionManager(work_dir)
    restored = session_mgr.restore_session(
        session_id=session_id,
        backup_dir=work_dir / "turn_1"
    )
    print(f"Session restored: {restored}")

    # Create new executor with restored session
    executor2 = ClaudeCodeExecutor(
        work_dir=work_dir,
        task_name="session_demo_continued",
        resume=session_id  # Resume from previous session
    )

    try:
        # Execute query that builds on previous context
        result2 = await executor2.execute("What file did we just create? Add more content to it.")
        print(f"Result 2: {result2['result'][:100]}...")

        # Save session from turn_2
        session_file2 = executor2.save_session_to(
            backup_dir=work_dir / "turn_2",
            metadata={
                "turn": 2,
                "query": "Add more content to file",
            }
        )
        print(f"Session saved to: {session_file2}")

        # Get session info
        info = executor2.get_session_info()
        if info:
            print(f"\nSession Info:")
            print(f"  Size: {info['file_size']} bytes")
            print(f"  Lines: {info['line_count']}")

    finally:
        await executor2.disconnect()

    print("\n=== Session Management Complete ===")
    print(f"Turn 1 session: {work_dir / 'turn_1'}")
    print(f"Turn 2 session: {work_dir / 'turn_2'}")


if __name__ == "__main__":
    print("Example 1: Simple execution")
    asyncio.run(example_simple_execution())

    print("\nExample 2: Continued conversation")
    asyncio.run(example_continued_conversation())

    print("\nExample 3: Custom logger")
    asyncio.run(example_with_custom_logger())

    print("\nExample 4: Session management")
    asyncio.run(example_with_session_management())
