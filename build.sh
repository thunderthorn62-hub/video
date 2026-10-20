#!/usr/bin/env bash
set -e

echo "=== Installing Deno ==="

mkdir -p "$PWD/.deno-bin"

curl -fsSL https://deno.land/install.sh | sh -s -- -y

if [ ! -x "$HOME/.deno/bin/deno" ]; then
    echo "ERROR: Deno installation failed"
    exit 1
fi

cp "$HOME/.deno/bin/deno" "$PWD/.deno-bin/deno"
chmod +x "$PWD/.deno-bin/deno"

echo "=== Checking Deno ==="
./.deno-bin/deno --version

echo "=== Installing Python dependencies ==="
python -m pip install --upgrade pip
python -m pip install --no-cache-dir --upgrade -r requirements.txt

echo "=== Build complete ==="
