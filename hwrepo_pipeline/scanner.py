from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from .models import MatchEvidence


def compile_allowlist(terms: Iterable[str]) -> re.Pattern:
    escaped = [re.escape(term) for term in terms]
    pattern = r"\b(" + "|".join(escaped) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def compile_denylist(terms: Iterable[str]) -> re.Pattern:
    escaped = [re.escape(term) for term in terms]
    pattern = r"\b(" + "|".join(escaped) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def _is_false_positive_vcs(line_lower: str) -> bool:
    if "version control" in line_lower or "version-control" in line_lower:
        return True
    return False


def _is_vcs_tool_usage(line_lower: str) -> bool:
    if "synopsys" in line_lower and "vcs" in line_lower:
        return True
    if re.search(r"\bvcs\b\s+[-+]", line_lower):
        return True
    if "vlogan" in line_lower or "vcs" in line_lower and "-full64" in line_lower:
        return True
    return False


def scan_text(
    path: str,
    content: str,
    allow_re: re.Pattern,
    deny_re: re.Pattern,
) -> Tuple[List[MatchEvidence], List[MatchEvidence]]:
    allow_hits: List[MatchEvidence] = []
    deny_hits: List[MatchEvidence] = []

    for idx, line in enumerate(content.splitlines(), start=1):
        if allow_re.search(line):
            allow_hits.append(
                MatchEvidence(path=path, line_number=idx, line=line.strip(), pattern=allow_re.pattern)
            )
        if deny_re.search(line):
            line_lower = line.lower()
            if re.search(r"\bvcs\b", line_lower):
                if _is_false_positive_vcs(line_lower):
                    continue
                if not _is_vcs_tool_usage(line_lower):
                    continue
            deny_hits.append(
                MatchEvidence(path=path, line_number=idx, line=line.strip(), pattern=deny_re.pattern)
            )
    return allow_hits, deny_hits


def extract_candidate_cmds(path: str, content: str) -> Tuple[List[str], List[str]]:
    build_cmds: List[str] = []
    test_cmds: List[str] = []

    if path.lower().endswith((".yml", ".yaml")):
        for line in content.splitlines():
            if "run:" in line:
                _, cmd = line.split("run:", 1)
                cmd = cmd.strip()
                if cmd in {"|", ">"}:
                    continue
                if cmd:
                    if re.search(r"\btest\b|\bcheck\b|pytest", cmd):
                        test_cmds.append(cmd)
                    else:
                        build_cmds.append(cmd)

    if path.endswith("Makefile"):
        for line in content.splitlines():
            match = re.match(r"^(test|check|build|all)\s*:\s*", line)
            if match:
                target = match.group(1)
                cmd = f"make {target}"
                if target in {"test", "check"}:
                    test_cmds.append(cmd)
                else:
                    build_cmds.append(cmd)

    return build_cmds, test_cmds
