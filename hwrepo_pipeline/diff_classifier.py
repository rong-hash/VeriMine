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
TEST_FILE_PATTERNS = [
    "*tb*.sv",
    "*tb*.v",
    "*test*.sv",
    "*test*.v",
    "*_tb.sv",
    "*_tb.v",
    "tb_*.sv",
    "tb_*.v",
    "*_test.sv",
    "*_test.v",
    "test_*.sv",
    "test_*.v",
    "*testbench*.sv",
    "*testbench*.v",
]

# Test directory patterns
TEST_DIR_PATTERNS = [
    "tb",
    "test",
    "tests",
    "testbench",
    "testbenches",
    "sim",
    "simulation",
    "verif",
    "verification",
    "bench",
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
