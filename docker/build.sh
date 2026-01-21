#!/bin/bash
# Build the hardware verification base image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building hw-base image..."
sudo docker build -t verimine/hw-base:latest -f "$SCRIPT_DIR/Dockerfile.hw-base" "$SCRIPT_DIR"

echo ""
echo "Done! Test with:"
echo "  sudo docker run --rm -it verimine/hw-base:latest"
echo ""
echo "Or mount a repo:"
echo "  sudo docker run --rm -it -v /path/to/repo:/workspace verimine/hw-base:latest"
