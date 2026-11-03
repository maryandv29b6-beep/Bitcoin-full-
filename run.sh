#!/usr/bin/env sh
# FlowLens: create a virtualenv, install dependencies and start the server.
#   ./run.sh          live public data from Binance/Bybit
#   ./run.sh --demo   synthetic DEMO data
set -e
cd "$(dirname "$0")/backend"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f ../config/flowlens.config.json ] && export FLOWLENS_CONFIG=../config/flowlens.config.json
python -m flowlens serve "$@"
