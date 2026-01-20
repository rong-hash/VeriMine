"""File classification for Verilog/SystemVerilog code and tests."""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import List, Tuple

from .models import FilePatch

# Verilog/SystemVerilog source extensions
VERILOG_EXTENSIONS = {".v", ".vh", ".sv", ".svh"}

# Test file patterns (filename-based)
# Focus on explicit testbench naming conventions
TEST_FILE_PATTERNS = [
    "*_tb.sv",      # module_tb.sv (most common)
    "*_tb.v",       # module_tb.v
    "tb_*.sv",      # tb_module.sv
    "tb_*.v",       # tb_module.v
    "*_test.sv",    # module_test.sv
    "*_test.v",     # module_test.v
    "*_tb_*.sv",    # module_tb_top.sv
    "*_tb_*.v",     # module_tb_top.v
    "*testbench*.sv",
    "*testbench*.v",
]

# Test directory patterns
# NOTE: Excluded 'sim', 'simulation' - these often contain simulation
# platform/infrastructure code, not actual testbenches
TEST_DIR_PATTERNS = [
    "tb",
    "test",
    "tests",
    "testbench",
    "testbenches",
    "verif",
    "verification",
    "bench",
    "dv",           # design verification
    "uvm",          # UVM testbenches
    "cocotb",       # cocotb tests
]


def is_verilog_file(path: str) -> bool:
    """Check if a file is a Verilog/SystemVerilog source file."""
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in VERILOG_EXTENSIONS


def is_test_file(path: str) -> bool:
    """Check if a file is a test file based on name and path patterns."""
    p = PurePosixPath(path)
    filename = p.name.lower()

    # Check filename patterns
    for pattern in TEST_FILE_PATTERNS:
        if fnmatch.fnmatch(filename, pattern):
            return True

    # Check directory patterns
    parts = [part.lower() for part in p.parts[:-1]]
    for part in parts:
        if part in TEST_DIR_PATTERNS:
            return True

    return False


def classify_file(path: str) -> str:
    """
    Classify a file as 'code', 'test', or 'other'.

    Returns:
        'test' - Verilog/SV test file
        'code' - Verilog/SV source file (non-test)
        'other' - Non-Verilog file
    """
    if not is_verilog_file(path):
        return "other"

    if is_test_file(path):
        return "test"

    return "code"


def classify_files(
    files: List[dict],
) -> Tuple[List[FilePatch], List[FilePatch], List[FilePatch]]:
    """
    Classify a list of file changes from GitHub API.

    Args:
        files: List of file dicts from GitHub API (with 'filename', 'additions',
               'deletions', 'patch' keys)

    Returns:
        Tuple of (code_patches, test_patches, other_patches)
    """
    code_patches: List[FilePatch] = []
    test_patches: List[FilePatch] = []
    other_patches: List[FilePatch] = []

    for f in files:
        path = f.get("filename") or f.get("path", "")
        patch_type = classify_file(path)

        fp = FilePatch(
            path=path,
            patch_type=patch_type,
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            patch=f.get("patch"),
        )

        if patch_type == "code":
            code_patches.append(fp)
        elif patch_type == "test":
            test_patches.append(fp)
        else:
            other_patches.append(fp)

    return code_patches, test_patches, other_patches


def has_valid_patches(files: List[dict], min_code: int = 1, min_test: int = 1) -> bool:
    """
    Quick check if files contain both code and test changes.

    This is a fast filter to avoid expensive operations on PRs/commits
    that clearly don't meet the criteria.
    """
    code_count = 0
    test_count = 0

    for f in files:
        path = f.get("filename") or f.get("path", "")
        patch_type = classify_file(path)

        if patch_type == "code":
            code_count += f.get("additions", 0) + f.get("deletions", 0)
        elif patch_type == "test":
            test_count += f.get("additions", 0) + f.get("deletions", 0)

        # Early exit if both thresholds are met
        if code_count >= min_code and test_count >= min_test:
            return True

    return code_count >= min_code and test_count >= min_test


def extract_issue_refs(message: str) -> List[str]:
    """
    Extract issue references from a commit message.

    Patterns matched:
    - #123
    - fixes #123
    - closes #123
    - resolves #123
    - GH-123
    - owner/repo#123
    """
    refs: List[str] = []

    # Match various issue reference patterns
    patterns = [
        r"(?:fixes|closes|resolves|fix|close|resolve)\s+#(\d+)",
        r"(?:fixes|closes|resolves|fix|close|resolve)\s+(\w+/\w+#\d+)",
        r"(?<![\w/])#(\d+)",
        r"GH-(\d+)",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        refs.extend(matches)

    return list(set(refs))


def compute_file_overlap(files1: List[str], files2: List[str]) -> float:
    """
    Compute the overlap ratio between two file lists.

    Returns a value between 0 and 1.
    """
    if not files1 or not files2:
        return 0.0

    set1 = set(files1)
    set2 = set(files2)

    intersection = len(set1 & set2)
    union = len(set1 | set2)

    return intersection / union if union > 0 else 0.0


def merge_patches(patches_list: List[List[FilePatch]]) -> Tuple[List[FilePatch], List[FilePatch]]:
    """
    Merge patches from multiple commits into unified code and test patches.

    For each file, we keep the latest patch (by order in the list).
    This assumes patches_list is ordered chronologically.

    Args:
        patches_list: List of (code_patches, test_patches) tuples from multiple commits

    Returns:
        Tuple of (merged_code_patches, merged_test_patches)
    """
    # Track latest patch for each file path
    code_by_path: dict[str, FilePatch] = {}
    test_by_path: dict[str, FilePatch] = {}

    for patches in patches_list:
        for patch in patches:
            if patch.patch_type == "code":
                if patch.path in code_by_path:
                    # Merge: accumulate additions/deletions, keep latest patch content
                    existing = code_by_path[patch.path]
                    code_by_path[patch.path] = FilePatch(
                        path=patch.path,
                        patch_type="code",
                        additions=existing.additions + patch.additions,
                        deletions=existing.deletions + patch.deletions,
                        patch=patch.patch,  # keep latest
                    )
                else:
                    code_by_path[patch.path] = patch
            elif patch.patch_type == "test":
                if patch.path in test_by_path:
                    existing = test_by_path[patch.path]
                    test_by_path[patch.path] = FilePatch(
                        path=patch.path,
                        patch_type="test",
                        additions=existing.additions + patch.additions,
                        deletions=existing.deletions + patch.deletions,
                        patch=patch.patch,
                    )
                else:
                    test_by_path[patch.path] = patch

    return list(code_by_path.values()), list(test_by_path.values())
