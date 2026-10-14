#!/bin/bash
# Optional ANUGA environment. Needs conda.
set -e
if ! command -v conda >/dev/null; then
  echo "conda was not found. ANUGA is optional."
  exit 1
fi
conda create -y -n anuga python=3.12
conda run -n anuga pip install anuga
echo "ANUGA env: $(conda run -n anuga which python)"
