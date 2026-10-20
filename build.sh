#!/usr/bin/env bash
set -e

# Install Deno
curl -fsSL https://deno.land/install.sh | sh

# Add Deno to PATH
export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

# Verify Deno installation
deno --version

# Install Python dependencies
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt
