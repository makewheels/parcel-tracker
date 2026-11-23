#!/bin/bash
# Hermes 定时任务发布 GitHub main 到腾讯云 SCF 和阿里云 FC。
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")/.." || exit 1

git fetch origin main --quiet
target=$(git rev-parse origin/main)
git merge --ff-only --quiet "$target"
if [ "$(git rev-parse HEAD)" != "$target" ] || [ -n "$(git status --porcelain --untracked-files=all -- fc deploy.py)" ]; then
  echo "部署目录和 GitHub main 不一致"
  exit 1
fi

scf_state=".git/parcel-scf-deployed-sha"
if [ ! -f "$scf_state" ] || [ "$(cat "$scf_state")" != "$target" ]; then
  uv run --quiet deploy.py --target tencent
  echo "$target" > "$scf_state"
  echo "腾讯云 SCF 已部署 ${target:0:7}"
fi

state=".git/parcel-deployed-sha"
if [ -f "$state" ] && [ "$(cat "$state")" = "$target" ]; then
  exit 0
fi

temp_dir=${TMPDIR:-$(python3 -c 'import tempfile; print(tempfile.gettempdir())')}
env_file=$(mktemp "${temp_dir%/}/parcel-deploy-env.XXXXXX")
trap 'rm -f "$env_file"' EXIT
python3 - <<'EOF' > "$env_file"
import json, pathlib, shlex, subprocess
fn = json.loads(subprocess.check_output(["aliyun", "fc", "get-function", "--function-name", "parcel-tracker"]))
for key, value in (fn.get("environmentVariables") or {}).items():
    print(f"export {key}={shlex.quote(value)}")
print("export PARCEL_ROLE=" + shlex.quote(fn["role"]))
# 优先用快递包裹专属 RAM 用户的凭据；没有才退回本机 CLI 配置
creds = pathlib.Path.home() / ".config/parcel-tracker/deploy-credentials.json"
if creds.exists():
    data = json.loads(creds.read_text())
    print("export ALIBABA_CLOUD_ACCESS_KEY_ID=" + shlex.quote(data["access_key_id"]))
    print("export ALIBABA_CLOUD_ACCESS_KEY_SECRET=" + shlex.quote(data["access_key_secret"]))
else:
    cfg = json.loads((pathlib.Path.home() / ".aliyun/config.json").read_text())
    profile = next((p for p in cfg.get("profiles", []) if p.get("name") == cfg.get("current")), cfg["profiles"][0])
    print("export ALIBABA_CLOUD_ACCESS_KEY_ID=" + shlex.quote(profile["access_key_id"]))
    print("export ALIBABA_CLOUD_ACCESS_KEY_SECRET=" + shlex.quote(profile["access_key_secret"]))
EOF
chmod 600 "$env_file"
# shellcheck disable=SC1090
source "$env_file"

if out=$(uv run --quiet deploy.py --role "$PARCEL_ROLE" 2>&1); then
  echo "$target" > "$state"
  echo "✅ 已自动部署 ${target:0:7}"
  echo "$out" | tail -1
else
  echo "❌ 自动部署失败（${target:0:7}）"
  echo "$out" | tail -15
  exit 1
fi
