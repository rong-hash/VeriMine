# Hardware Repo Pipeline (Verilog/SystemVerilog)

A GitHub API-based repo filtering pipeline for Verilog/SystemVerilog projects. It applies hard gates and outputs RepoCard JSONL for accepted repositories.

## Quick Start

1. Install dependencies:
   ```bash
   pip install -e .
   ```
2. Set a GitHub token (strongly recommended):
   ```bash
   export GITHUB_TOKEN=YOUR_TOKEN
   ```
3. Copy and edit config:
   ```bash
   cp config.example.json config.json
   ```
4. Run:
   ```bash
   python -m hwrepo_pipeline --config config.json
   ```

Default outputs:
- `output/repo_cards.jsonl`
- `output/rejects.jsonl`

## Filtering Overview

- Search: `language` + `stars` + `fork:false archived:false`
- Activity: `pushed_at` within N days
- Language ratio: Verilog + SystemVerilog bytes >= threshold
- Verilog/SV size: file count >= threshold or line count >= threshold
- Toolchain evidence: allowlist hit and no denylist hit in key files
- Community: Issue totals
- Commit activity: last 12m or 6m commit counts
- Release/Tags: at least one release or enough tags

## Config Notes

Config is JSON; see `config.example.json`. Common knobs:
- `min_stars` / `pushed_within_days`
- `min_sv_ratio` / `min_sv_files` / `min_sv_lines`
- `min_pr_total` / `min_issue_total`
- `min_commit_last_12m` / `min_commit_last_6m`
- `min_releases` / `min_tags`
- `allowlist_terms` / `denylist_terms`

## Practical Notes

- GitHub Search API caps results at 1000; tune `max_repos_per_language` accordingly.
- `min_sv_lines` triggers per-file content pulls; set it to 0 for faster pilots.
- The VCS deny rule is conservative to avoid false positives on "version control system".
- `sv_line_count` is set to `-1` when line counting is skipped because the file-count gate already passed.

