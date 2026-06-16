#!/bin/sh
set -eu

ENV_NAME="valley"
PYTHON_VERSION="3.13.5"

if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  . "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
  . "$HOME/anaconda3/etc/profile.d/conda.sh"
elif command -v conda >/dev/null 2>&1; then
  CONDA_BIN=$(command -v conda)
  CONDA_PREFIX=$(cd "$(dirname "$CONDA_BIN")/.." && pwd)
  if [ -f "$CONDA_PREFIX/etc/profile.d/conda.sh" ]; then
    . "$CONDA_PREFIX/etc/profile.d/conda.sh"
  fi
fi

if conda env list 2>/dev/null | awk '{print $1}' | grep -xq "$ENV_NAME"; then
  :
else
  conda create -n "$ENV_NAME" python="$PYTHON_VERSION" -y
fi

conda activate "$ENV_NAME"
# conda info --envs 2>/dev/null | awk '/\*/{print $1; exit}'

pip install -r requirements.txt

if [ ! -f .env ]; then
  python init_env.py
fi

python main.py
