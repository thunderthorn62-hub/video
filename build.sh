#!/usr/bin/env bash
set -e

curl -fsSL https://deno.land/install.sh | sh

export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

deno --version

python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt
