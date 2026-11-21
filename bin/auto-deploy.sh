#!/bin/bash
# 自动部署 parcel-tracker：本地检查 GitHub 有没有新提交，有就部署到阿里云 FC。
# 由 Hermes 定时任务每 10 分钟调用；只在真的部署时输出（无输出 = 无事发生）。
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")/.." || exit 1

state=".git/parcel-deployed-sha"

# 有远端更新就拉下来（失败不阻塞，仍按本地 HEAD 判断）
git fetch origin main --quiet 2>/dev/null
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main 2>/dev/null)" ]; then
  git pull --ff-only --quiet || echo "⚠️ 本地有未合并改动，按本地 HEAD 部署"
fi

# 判断依据是「上次部署的提交」，不是本地/远端是否相同——
# 在本地 commit 后直接 push 时，两者相等，远端比对会漏部署。
target=$(git rev-parse HEAD)
if [ -f "$state" ] && [ "$(cat "$state")" = "$target" ]; then
  exit 0
fi

env_file=$(mktemp /tmp/parcel-deploy-env.XXXXXX)
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
rm -f "$env_file"

if out=$(uv run --quiet deploy.py --role "$PARCEL_ROLE" 2>&1); then
  echo "$target" > "$state"
  echo "✅ 已自动部署 ${target:0:7}"
  echo "$out" | tail -1
else
  echo "❌ 自动部署失败（${target:0:7}）"
  echo "$out" | tail -15
  exit 1
fi