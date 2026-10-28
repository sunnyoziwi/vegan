#!/usr/bin/env bash
# Chạy 1 workflow n8n đúng 1 lần bằng n8n CLI (GitHub Actions gọi file này; chạy thử trên máy cũng được).
#   bash ci/run-workflow.sh facebook   → cần GEMINI_API_KEY + FB_PAGE_TOKEN
#   bash ci/run-workflow.sh tiktok     → cần GEMINI_API_KEY; muốn đăng TikTok thêm GH_PAGES_TOKEN,
#                                        TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN (thiếu thì chỉ tạo file)
# Danh sách bài đã làm (static data của n8n) được nạp từ state/<tên>.json và ghi lại sau khi chạy.
# Ảnh/caption tạo ra nằm ở run/output/.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:?Dùng: run-workflow.sh facebook|tiktok}"
case "$NAME" in
  facebook) WF_FILE=n8n-vegan-facebook-workflow.json; WF_ID=veganFbDaily0001; OUT_DIR=/files/output
            : "${FB_PAGE_TOKEN:?Thiếu FB_PAGE_TOKEN}" ;;
  tiktok)   WF_FILE=n8n-vegan-tiktok-workflow.json;   WF_ID=veganTtDaily0001; OUT_DIR=/files/tiktok_output ;;
  *) echo "Không có workflow '$NAME'" >&2; exit 2 ;;
esac
: "${GEMINI_API_KEY:?Thiếu GEMINI_API_KEY}"
STATE="state/$NAME.json"
N8N_IMAGE="${N8N_IMAGE:-n8nio/n8n:2.44.6}"

rm -rf run
mkdir -p run/n8n run/output
chmod -R 777 run

# điền key + id cố định + danh sách bài đã làm vào bản workflow tạm (run/ không bao giờ được commit)
jq --arg id "$WF_ID" --arg gk "$GEMINI_API_KEY" --arg fb "${FB_PAGE_TOKEN:-}" --slurpfile sd "$STATE" \
   --arg gh "${GH_PAGES_TOKEN:-}" --arg tk "${TIKTOK_CLIENT_KEY:-}" --arg ts "${TIKTOK_CLIENT_SECRET:-}" --arg tr "${TIKTOK_REFRESH_TOKEN:-}" '
  .id = $id | .active = false | .staticData = $sd[0]
  | (.nodes[] | select(.name == "Config") | .parameters.assignments.assignments[]) |= (
      if .name == "geminiApiKey" then .value = $gk
      elif .name == "fbPageToken" then .value = $fb
      elif .name == "githubToken" then .value = $gh
      elif .name == "tiktokClientKey" then .value = $tk
      elif .name == "tiktokClientSecret" then .value = $ts
      elif .name == "tiktokRefreshToken" then .value = $tr
      else . end)
' "$WF_FILE" > run/workflow.json

n8n() {
  docker run --rm --entrypoint n8n \
    -v "$PWD/run/n8n:/home/node/.n8n" \
    -v "$PWD/run:/run-files" \
    -v "$PWD/run/output:$OUT_DIR" \
    -e N8N_RESTRICT_FILE_ACCESS_TO="$OUT_DIR" \
    -e NODES_EXCLUDE='["n8n-nodes-base.localFileTrigger"]' \
    -e GENERIC_TIMEZONE=Asia/Ho_Chi_Minh \
    -e N8N_RUNNERS_TASK_TIMEOUT=900 \
    -e TZ=Asia/Ho_Chi_Minh \
    "$N8N_IMAGE" "$@"
}

n8n import:workflow --input=/run-files/workflow.json
status=0
n8n execute --id="$WF_ID" --rawOutput > run/result.json || status=$?

# lưu lại danh sách bài đã làm, kể cả khi lỗi giữa chừng (để lần sau không làm trùng)
n8n export:workflow --id="$WF_ID" --output=/run-files/exported.json
jq '(if type == "array" then .[0] else . end).staticData // {}' run/exported.json > "$STATE"

echo "File đã tạo: $(find run/output -type f | wc -l | tr -d ' ')"
exit "$status"
