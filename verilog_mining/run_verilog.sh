#!/bin/bash
# Verilog Task Mining - Rollout Launch Script
# Generates Verilog coding tasks from hardware design repositories
#
# Usage:
#   bash run_verilog.sh [EXPERIMENT_NAME] [START] [END] [MODEL] [FLASH_MODEL] \
#                       [CONCURRENCY] [TOP_N] [REQUIRE_CODE_TEST] [PR_TIMEOUT] \
#                       [REPO_TIMEOUT] [ACTOR_MODELS] [VALIDATION_RUNS]

set -e
set -x

# Validate API keys
export GITHUB_TOKEN="${GITHUB_TOKEN:?Error: GITHUB_TOKEN not set}"
if [ -z "$QIANXUN_API_KEY" ]; then
    echo "Error: QIANXUN_API_KEY not set. Please run: source ~/.bashrc"
    exit 1
fi

# Command-line arguments with defaults
EXPERIMENT_NAME=${1:-verilog-task-gen}
START_INDEX=${2:-0}
END_INDEX=${3:-50}
MODEL=${4:-my-x35-cc-xyz}
FLASH_MODEL=${5:-$MODEL}
CONCURRENCY=${6:-50}
PR_DISCOVERY_TOP_N=${7:-20}
REQUIRE_CODE_AND_TEST=${8:-true}
PR_TIMEOUT=${9:-300}
REPO_TIMEOUT=${10:-9999}
ACTOR_MODELS_INPUT=${11:-$MODEL}
ACTOR_MODELS=$(echo "$ACTOR_MODELS_INPUT" | tr ',' ' ')
VALIDATION_RUNS=${12:-4}

# Derived values
WORKSPACE_ID="chenzhirong-verilog-task/${EXPERIMENT_NAME}"
TASK="${EXPERIMENT_NAME}"

echo "========================================"
echo "Verilog Task Mining - Rollout"
echo "========================================"
echo "Experiment: $EXPERIMENT_NAME"
echo "Workspace: $WORKSPACE_ID"
echo "Index range: [$START_INDEX, $END_INDEX)"
echo "Model: $MODEL"
echo "Flash Model: $FLASH_MODEL"
echo "Concurrency: $CONCURRENCY"
echo "PR Discovery Top N: $PR_DISCOVERY_TOP_N"
echo "Require Code+Test: $REQUIRE_CODE_AND_TEST"
echo "PR Timeout: ${PR_TIMEOUT}min"
echo "Repo Timeout: ${REPO_TIMEOUT}min"
echo "Actor Models: $ACTOR_MODELS"
echo "Validation Runs: $VALIDATION_RUNS"
echo "========================================"

# Paths inside sandbox
LOCAL_OUTPUT_DIR=/tmp/dataset
FINAL_OUTPUT_DIR=/mnt/workspace/$TASK/output

# Code+test filter flag
if [ "$REQUIRE_CODE_AND_TEST" = "true" ]; then
    CODE_TEST_FLAG=""  # default is require
else
    CODE_TEST_FLAG="--no-code-test-filter"
fi

# Build the task generator command (runs inside sandbox)
# Note: entrypoint.sh copies CODE_DIR to /hyy_workspace and cd's there
# entrypoint.sh also clones repo to /hyy_workspace/repo
# So we run directly (pyproject.toml is in workspace) and use --repo-path to avoid double clone
cmd="uv run python verilog_task_generator.py \
    --repo \$REPO_NAME \
    --repo-path \$REPO_PATH \
    --output $LOCAL_OUTPUT_DIR \
    --model $MODEL \
    --flash-model $FLASH_MODEL \
    --top-n $PR_DISCOVERY_TOP_N \
    --max-prs 100 \
    --pr-timeout $((PR_TIMEOUT * 60)) \
    --repo-timeout $((REPO_TIMEOUT * 60)) \
    --quality-threshold 6.5 \
    --validation-runs $VALIDATION_RUNS \
    --actor-models $ACTOR_MODELS \
    --run-mode remote \
    --copy-to-final $FINAL_OUTPUT_DIR \
    --task-type all \
    $CODE_TEST_FLAG"

# Rollout output dir (local)
ROLLOUT_OUTPUT_DIR="/tmp/rollouts/${EXPERIMENT_NAME}"

# Launch via cc_rollout.py
# Note: Uses eda-sandbox:agent image (single shared image, not per-repo)
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
    --upload "verilog_mining/*"

echo ""
echo "========================================"
echo "Verilog Task Mining Complete!"
echo "Output: $FINAL_OUTPUT_DIR"
echo "========================================"
