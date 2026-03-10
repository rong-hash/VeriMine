#!/bin/bash
# Verilog Task Mining - Environment Setup
# Installs Python dependencies and tools inside the sandbox container
#
# Adapted from agent-task-craft/new_feature/setup_env.sh
# Simplified: no nix or anthropic-openai-compatible-server needed for initial setup

set -e
set -x

echo "========================="
echo "Setting up environment..."
echo "Current directory: $PWD"
echo "========================="

# Ensure curl is available
if ! command -v curl &> /dev/null; then
    echo "Installing curl..."
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; elif command -v sudo &>/dev/null; then SUDO="sudo"; else SUDO=""; fi

    if command -v apt-get &>/dev/null; then
        $SUDO apt-get update -qq && $SUDO apt-get install -y -qq curl ca-certificates
    elif command -v yum &>/dev/null; then
        $SUDO yum install -y -q curl ca-certificates
    elif command -v apk &>/dev/null; then
        $SUDO apk add --no-cache curl ca-certificates
    fi
fi

# Check pyproject.toml
if [ ! -f "pyproject.toml" ]; then
    echo "ERROR: pyproject.toml not found in $PWD"
    ls -la
    exit 1
fi

# Install uv if not available
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    export PATH="$HOME/.local/bin:$PATH"

    if command -v curl &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh && echo "uv installed via curl"
    elif command -v pip &>/dev/null; then
        pip install uv --quiet && echo "uv installed via pip"
    else
        echo "ERROR: Cannot install uv"
        exit 1
    fi

    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

echo "uv version: $(uv --version)"

# Virtual environment
if [ -d "/root/.venv" ]; then
    echo "Activating existing venv at /root/.venv"
    source /root/.venv/bin/activate
    export UV_PYTHON=/root/.venv/bin/python3
else
    echo "Creating new venv at /root/.venv"
    uv venv /root/.venv
    source /root/.venv/bin/activate
    export UV_PYTHON=/root/.venv/bin/python3
fi

# Install dependencies
echo "========================="
echo "Installing dependencies..."
export UV_HTTP_TIMEOUT=600
export UV_LINK_MODE=copy
export UV_PROJECT_ENVIRONMENT=/root/.venv
uv sync --active

# Verify key packages
echo "========================="
echo "Verifying packages..."
python3 -c "import claude_agent_sdk; print(f'claude_agent_sdk {claude_agent_sdk.__version__}')" 2>&1 || echo "claude_agent_sdk FAILED"
python3 -c "import github; print('PyGithub OK')" 2>&1 || echo "PyGithub FAILED"
echo "========================="

# Verify EDA tools
echo "Checking EDA tools..."
command -v iverilog && echo "iverilog: $(iverilog -V 2>&1 | head -1)" || echo "iverilog: NOT FOUND"
command -v verilator && echo "verilator: $(verilator --version 2>&1 | head -1)" || echo "verilator: NOT FOUND"
command -v cocotb-config && echo "cocotb: $(cocotb-config --version 2>&1)" || echo "cocotb: NOT FOUND (install with pip)"

# Install nix and anthropic-openai-compatible-server
echo "========================="
echo "Installing nix..."
echo "========================="

MAX_RETRIES=20
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    echo "Installing nix (attempt $((RETRY_COUNT + 1))/$MAX_RETRIES)..."
    if curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix -o /tmp/nix-install.sh; then
        if sh /tmp/nix-install.sh install linux --extra-conf "sandbox = false" --init none --no-confirm; then
            echo "Nix installed"
            break
        fi
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
        sleep 5
    else
        echo "ERROR: Nix installation failed after $MAX_RETRIES attempts"
        exit 1
    fi
done

export PATH="${PATH}:/nix/var/nix/profiles/default/bin"
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
nix profile remove anthropic-openai-compatible-server || true
nix --option extra-substituters 'http://172.24.127.103:8080/moonshot' \
    --option extra-trusted-public-keys 'moonshot:Cajs/8Dgd7YdqSqzZeoWnHPGXQSoFwKJhgaHoE7JWRc=' \
    profile add 'git+https://dev.msh.team/coding/nix-agents.git?rev=1e3c85ccbe266b5c1cf5fab0379771a4e02fa683#anthropic-openai-compatible-server'

echo "========================="
echo "Setup complete!"
echo "========================="
