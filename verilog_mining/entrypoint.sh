#!/bin/bash
# Verilog Task Mining - Sandbox Entrypoint
# Runs inside the eda-sandbox:agent container
#
# Key difference from C++ entrypoint:
# - Clones the repo (instead of expecting a pre-built Docker image)
# - Uses eda-sandbox tools (iverilog, verilator, cocotb)

set -e
set -x

# Validate required environment variables
if [ -z "$CODE_DIR" ]; then
    echo "ERROR: CODE_DIR not set"
    exit 1
fi
if [ -z "$OUTPUT_DIR" ]; then
    echo "ERROR: OUTPUT_DIR not set"
    exit 1
fi
if [ -z "$USER_CMD" ]; then
    echo "ERROR: USER_CMD not set"
    exit 1
fi

echo "========================="
echo "Verilog Task Mining Entrypoint"
echo "CODE_DIR: $CODE_DIR"
echo "OUTPUT_DIR: $OUTPUT_DIR"
echo "REPO_NAME: ${REPO_NAME:-not set}"
echo "USER_CMD: $USER_CMD"
echo "========================="

mkdir -p "$OUTPUT_DIR"

# Create workspace and copy code
WORKSPACE=/hyy_workspace
mkdir -p "$WORKSPACE"

echo "Copying files from $CODE_DIR to $WORKSPACE..."
MAX_RETRIES=5
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if cp -r "$CODE_DIR"/* "$WORKSPACE/"; then
        echo "Copy successful (attempt $((RETRY_COUNT + 1)))"
        break
    else
        RETRY_COUNT=$((RETRY_COUNT + 1))
        if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
            rm -rf "$WORKSPACE"/*
            sleep 2
        else
            echo "ERROR: Copy failed after $MAX_RETRIES attempts"
            exit 1
        fi
    fi
done

cd "$WORKSPACE"

# Save task metadata
cat > task.json <<EOF
{
  "task_image": "${TASK_IMAGE:-}",
  "task_id": "${TASK_ID:-0}",
  "repo_name": "${REPO_NAME:-}",
  "task_source": "${TASK_SOURCE:-}"
}
EOF

# Run environment setup
if [ -f setup_env.sh ]; then
    echo "Running setup_env.sh..."
    max_retries=3
    retry_count=0
    while [ $retry_count -lt $max_retries ]; do
        if bash setup_env.sh > "$OUTPUT_DIR/setup_env_$retry_count.log" 2>&1; then
            echo "setup_env.sh completed"
            break
        else
            retry_count=$((retry_count + 1))
            if [ $retry_count -lt $max_retries ]; then
                echo "setup_env.sh failed, retrying ($retry_count/$max_retries)..."
                sleep 5
            else
                echo "ERROR: setup_env.sh failed after $max_retries attempts"
                tail -50 "$OUTPUT_DIR/setup_env_$((retry_count - 1)).log" 2>/dev/null || true
                exit 1
            fi
        fi
    done
fi

# Clone the repository (key difference from C++ pipeline)
REPO_DIR="$WORKSPACE/repo"
if [ -n "$REPO_NAME" ]; then
    echo "========================="
    echo "Cloning repository: $REPO_NAME"
    echo "========================="

    CLONE_URL="https://github.com/${REPO_NAME}"
    if [ -n "$GITHUB_TOKEN" ]; then
        CLONE_URL="https://${GITHUB_TOKEN}@github.com/${REPO_NAME}"
    fi

    MAX_RETRIES=3
    RETRY_COUNT=0
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        if git clone --depth=500 "$CLONE_URL" "$REPO_DIR" 2>"$OUTPUT_DIR/git_clone.log"; then
            echo "Clone successful"
            break
        else
            RETRY_COUNT=$((RETRY_COUNT + 1))
            if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
                echo "Clone failed, retrying ($RETRY_COUNT/$MAX_RETRIES)..."
                rm -rf "$REPO_DIR"
                sleep 5
            else
                echo "ERROR: Clone failed after $MAX_RETRIES attempts"
                cat "$OUTPUT_DIR/git_clone.log" 2>/dev/null || true
                exit 1
            fi
        fi
    done

    export REPO_PATH="$REPO_DIR"
else
    echo "WARNING: REPO_NAME not set, skipping clone"
fi

# Environment variables
ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-"sonnet"}
FLASH_MODEL=${FLASH_MODEL:-""}
MAX_THINKING_TOKENS=${MAX_THINKING_TOKENS:-"32768"}

export ANTHROPIC_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_SMALL_FAST_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$ANTHROPIC_MODEL"
export DISABLE_TELEMETRY=1
export IS_SANDBOX=1
export MAX_THINKING_TOKENS="$MAX_THINKING_TOKENS"
export PATH="/root/.local/bin:/nix/var/nix/profiles/default/bin:$PATH"

# Detect model type and set up proxy if needed
is_claude_model() {
    echo "$1" | grep -iqE 'sonnet|opus|haiku'
}

setup_proxy() {
    local model="$1"
    local port=8080
    echo "=== Starting anthropic-openai-compatible-server for: $model ==="

    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true

    if [[ "$model" == internal/* ]] || [[ "$model" == kimi-* ]] || [[ "$model" == infinigence/* ]]; then
        echo "Using qianxun: https://openai.app.msh.team/v1"
        OPENAI_MODEL_CONTEXT_LENGTH=262144 \
        ANTHROPIC_MIDDLEWARE_TEMPERATURE_MULTIPLIER=1.0 \
        OPENAI_BASE_URL=https://openai.app.msh.team/v1 \
        OPENAI_API_KEY="${QIANXUN_API_KEY:-}" \
        PORT=$port \
        LOG_LEVEL=info \
        anthropic-openai-compatible-server > "$OUTPUT_DIR/proxy_server.log" 2>&1 &
    else
        echo "Using self-deployed: https://${model}.app.msh.team/v1"
        OPENAI_MODEL_CONTEXT_LENGTH=262144 \
        ANTHROPIC_MIDDLEWARE_TEMPERATURE_MULTIPLIER=1.0 \
        OPENAI_BASE_URL="https://${model}.app.msh.team/v1" \
        OPENAI_API_KEY=sk-empty \
        PORT=$port \
        LOG_LEVEL=info \
        anthropic-openai-compatible-server > "$OUTPUT_DIR/proxy_server.log" 2>&1 &
    fi

    local pid=$!
    echo "Proxy PID: $pid, waiting 3s..."
    sleep 3
    if kill -0 $pid 2>/dev/null; then
        echo "Proxy ready at http://127.0.0.1:$port"
    else
        echo "WARNING: Proxy failed. Log:"
        cat "$OUTPUT_DIR/proxy_server.log" 2>/dev/null | tail -20
    fi
}

# Check generation model
NEED_PROXY=false
if ! is_claude_model "$ANTHROPIC_MODEL"; then
    NEED_PROXY=true
    PROXY_MODEL="$ANTHROPIC_MODEL"
fi

# Check actor models (extract from USER_CMD)
ACTOR_MODELS=$(echo "$USER_CMD" | grep -oP '(?<=--actor-models )[\w\-\./ ]+(?= --|$)' || true)
for m in $ACTOR_MODELS; do
    if ! is_claude_model "$m"; then
        NEED_PROXY=true
        PROXY_MODEL="${PROXY_MODEL:-$m}"
        break
    fi
done

if [ "$NEED_PROXY" = true ]; then
    echo "Non-Claude model detected: $PROXY_MODEL"
    if command -v anthropic-openai-compatible-server &>/dev/null; then
        setup_proxy "$PROXY_MODEL"
        export CLAUDE_CODE_RUN_MODE=remote
    else
        echo "ERROR: anthropic-openai-compatible-server not found!"
        exit 1
    fi
else
    echo "All models are Claude — no proxy needed"
    export CLAUDE_CODE_RUN_MODE=remote
fi

# Activate virtual environment
if [ -d /root/.venv ]; then
    source /root/.venv/bin/activate
fi

echo "========================="
echo "Running user command..."
echo "CMD: $USER_CMD"
echo "========================="

# Replace $REPO_NAME in the command with actual value
FINAL_CMD=$(echo "$USER_CMD" | sed "s|\\\$REPO_NAME|${REPO_NAME}|g")

# Execute
eval "$FINAL_CMD" 2>&1 | tee "$OUTPUT_DIR/execution.log"
EXIT_CODE=${PIPESTATUS[0]}

echo "========================="
echo "Execution complete. Exit code: $EXIT_CODE"
echo "Output: $OUTPUT_DIR"
echo "========================="

# Copy results to final output
if [ -d "$LOCAL_OUTPUT_DIR" ] && [ -n "$FINAL_OUTPUT_DIR" ]; then
    echo "Syncing results to $FINAL_OUTPUT_DIR..."
    mkdir -p "$FINAL_OUTPUT_DIR"
    cp -r "$LOCAL_OUTPUT_DIR"/* "$FINAL_OUTPUT_DIR/" 2>/dev/null || true
fi

exit $EXIT_CODE
