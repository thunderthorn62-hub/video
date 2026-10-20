#!/usr/bin/env bash
set -e

echo "=== Installing Deno ==="

curl -fsSL https://deno.land/install.sh | sh

export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

echo "=== Checking Deno ==="
which deno
deno --version

echo "=== Installing Python dependencies ==="
python -m pip install --upgrade pip
python -m pip install --no-cache-dir --upgrade -r requirements.txt

echo "=== Build complete ==="
