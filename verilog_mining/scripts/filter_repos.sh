#!/bin/bash
# Convenience script to run VeriMine's repo filtering pipeline locally
# Produces data/repo_list.jsonl for the task mining pipeline

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VERIMINE_DIR="$(dirname "$PROJECT_DIR")"

# Configuration
CONFIG=${1:-"$VERIMINE_DIR/config.json"}
OUTPUT_DIR=${2:-"$PROJECT_DIR/data"}

echo "========================================"
echo "VeriMine Repo Filtering"
echo "========================================"
echo "Config: $CONFIG"
echo "Output: $OUTPUT_DIR"
echo "========================================"

# Ensure GitHub token is set
if [ -z "$GITHUB_TOKEN" ]; then
    echo "ERROR: GITHUB_TOKEN not set"
    echo "Usage: export GITHUB_TOKEN=your_token"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Run VeriMine pipeline
cd "$VERIMINE_DIR"
python -m hwrepo_pipeline --config "$CONFIG"

# Convert repo_cards.jsonl to the format needed by task mining
REPO_CARDS="$VERIMINE_DIR/output/repo_cards.jsonl"
REPO_LIST="$OUTPUT_DIR/repo_list.jsonl"

if [ -f "$REPO_CARDS" ]; then
    echo "Converting repo_cards.jsonl to repo_list.jsonl..."
    python3 -c "
import json
with open('$REPO_CARDS') as f_in, open('$REPO_LIST', 'w') as f_out:
    for line in f_in:
        card = json.loads(line)
        entry = {
            'repo': card.get('full_name', ''),
            'full_name': card.get('full_name', ''),
            'stars': card.get('stars', 0),
            'description': card.get('description', ''),
            'language': card.get('primary_language', 'Verilog'),
        }
        f_out.write(json.dumps(entry) + '\n')
    "
    REPO_COUNT=$(wc -l < "$REPO_LIST")
    echo "Generated $REPO_LIST with $REPO_COUNT repos"
else
    echo "ERROR: $REPO_CARDS not found"
    echo "Run the VeriMine pipeline first"
    exit 1
fi

echo "========================================"
echo "Filtering complete!"
echo "Repos: $REPO_LIST"
echo "========================================"
