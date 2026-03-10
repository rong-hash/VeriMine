#!/bin/bash
# New Feature Craft - Rollout Launch Script
# Mines core modules from chip design repos to construct new-feature tasks
#
# Usage:
#   bash run_craft.sh [EXPERIMENT_NAME] [START] [END] [MODEL] [FLASH_MODEL] \
#                     [CONCURRENCY] [MODULES_PER_REPO] [TEST_TIMEOUT] \
#                     [REPO_TIMEOUT] [ACTOR_MODELS] [VALIDATION_RUNS]

set -e
set -x

# Validate API keys
if [ -z "$QIANXUN_API_KEY" ]; then
    echo "Error: QIANXUN_API_KEY not set. Please run: source ~/.bashrc"
    exit 1
fi
# GITHUB_TOKEN is optional (helps with clone rate limits for public repos)
if [ -n "$GITHUB_TOKEN" ]; then
    export GITHUB_TOKEN
fi

# Command-line arguments with defaults
EXPERIMENT_NAME=${1:-craft-chip-repos}
START_INDEX=${2:-0}
END_INDEX=${3:-60}
MODEL=${4:-my-x35-cc-xyz}
FLASH_MODEL=${5:-$MODEL}
CONCURRENCY=${6:-50}
MODULES_PER_REPO=${7:-3}
TEST_TIMEOUT=${8:-300}
REPO_TIMEOUT=${9:-9999}
ACTOR_MODELS_INPUT=${10:-$MODEL}
ACTOR_MODELS=$(echo "$ACTOR_MODELS_INPUT" | tr ',' ' ')
VALIDATION_RUNS=${11:-4}
INPUT_FILE=${12:-/home/chenzhirong/VeriMine/output/repo_list_chip.jsonl}

# Derived values
WORKSPACE_ID="chenzhirong-verilog-task/${EXPERIMENT_NAME}"
TASK="${EXPERIMENT_NAME}"

echo "========================================"
echo "New Feature Craft - Rollout"
echo "========================================"
echo "Experiment: $EXPERIMENT_NAME"
echo "Workspace: $WORKSPACE_ID"
echo "Index range: [$START_INDEX, $END_INDEX)"
echo "Model: $MODEL"
echo "Flash Model: $FLASH_MODEL"
echo "Concurrency: $CONCURRENCY"
echo "Modules per repo: $MODULES_PER_REPO"
echo "Test Timeout: ${TEST_TIMEOUT}s"
echo "Repo Timeout: ${REPO_TIMEOUT}min"
echo "Actor Models: $ACTOR_MODELS"
echo "Validation Runs: $VALIDATION_RUNS"
echo "Input File: $INPUT_FILE"
echo "========================================"

# Paths inside sandbox
LOCAL_OUTPUT_DIR=/tmp/dataset
FINAL_OUTPUT_DIR=/mnt/workspace/$TASK/output

# Build the craft command (runs inside sandbox)
cmd="uv run python __main__.py \
    --repo \$REPO_NAME \
    --repo-path \$REPO_PATH \
    --output $LOCAL_OUTPUT_DIR \
    --model $MODEL \
    --flash-model $FLASH_MODEL \
    --modules-per-repo $MODULES_PER_REPO \
    --test-timeout $TEST_TIMEOUT \
    --repo-timeout $((REPO_TIMEOUT * 60)) \
    --quality-threshold 6.5 \
    --validation-runs $VALIDATION_RUNS \
    --actor-models $ACTOR_MODELS \
    --run-mode remote \
    --copy-to-final $FINAL_OUTPUT_DIR"

# Rollout output dir (local)
ROLLOUT_OUTPUT_DIR="/tmp/rollouts/${EXPERIMENT_NAME}"

# Launch via cc_rollout.py
cd /home/chenzhirong/VeriMine
python /home/chenzhirong/agent-task-craft/cc_env/cc_rollout.py \
    --cmd "$cmd" \
    --task "$TASK" \
    --env "eda-sandbox" \
    --workspace-id "$WORKSPACE_ID" \
    --start-index $START_INDEX \
    --end-index $END_INDEX \
    --model "$MODEL" \
    --flash-model "$FLASH_MODEL" \
    --output-dir "$ROLLOUT_OUTPUT_DIR" \
    -c $CONCURRENCY \
    --early-start-threshold 0.6 \
    --max-concurrent-batches 5 \
    --dedupe-by-repo \
    --input-file "$INPUT_FILE" \
    --upload "new_feature_craft/*"

echo ""
echo "========================================"
echo "New Feature Craft Complete!"
echo "Output: $FINAL_OUTPUT_DIR"
echo "========================================"
