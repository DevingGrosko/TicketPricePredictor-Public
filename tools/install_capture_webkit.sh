#!/usr/bin/env bash
# Run only on the disposable Ubuntu 24.04 Actions runner.
set -euo pipefail
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 && "${GITHUB_ACTIONS:-}" == true ]]

# The Azure HTTP mirror has taken 45+ minutes to serve this dependency set.
# Use Ubuntu's official HTTPS mirror; leave other configured repositories alone.
for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
  if [[ -f "$source_file" ]]; then
    sudo sed -i -E 's@https?://azure.archive.ubuntu.com/ubuntu@https://archive.ubuntu.com/ubuntu@g' "$source_file"
  fi
done
sudo tee /etc/apt/apt.conf.d/99-ticket-capture-timeouts >/dev/null <<'APT'
Acquire::Retries "1";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
APT
python -m pip install --timeout 30 --retries 1 -r requirements-webkit-collector.txt
PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=60000 python -m playwright install --with-deps webkit
