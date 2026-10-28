#!/usr/bin/env bash
# Chạy workflow Facebook đúng 1 lần bằng n8n CLI (GitHub Actions gọi file này; chạy thử trên máy cũng được).
# Cần: docker, jq, biến môi trường GEMINI_API_KEY và FB_PAGE_TOKEN.
# Danh sách bài đã đăng (static data của n8n) được nạp từ state/static-data.json và ghi lại sau khi chạy.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GEMINI_API_KEY:?Thiếu GEMINI_API_KEY}"
: "${FB_PAGE_TOKEN:?Thiếu FB_PAGE_TOKEN}"
N8N_IMAGE="${N8N_IMAGE:-n8nio/n8n:2.44.6}"
WF_ID=veganFbDaily0001

rm -rf run
mkdir -p run/n8n run/output
chmod -R 777 run

# điền key + id cố định + danh sách bài đã đăng vào bản workflow tạm (run/ không bao giờ được commit)
jq --arg id "$WF_ID" --arg gk "$GEMINI_API_KEY" --arg fb "$FB_PAGE_TOKEN" --slurpfile sd state/static-data.json '
  .id = $id | .active = false | .staticData = $sd[0]
  | (.nodes[] | select(.name == "Config") | .parameters.assignments.assignments[]) |= (
      if .name == "geminiApiKey" then .value = $gk
      elif .name == "fbPageToken" then .value = $fb
      else . end)
' n8n-vegan-facebook-workflow.json > run/workflow.json

n8n() {
  docker run --rm --entrypoint n8n \
    -v "$PWD/run/n8n:/home/node/.n8n" \
    -v "$PWD/run:/run-files" \
    -v "$PWD/run/output:/files/output" \
    -e N8N_RESTRICT_FILE_ACCESS_TO=/files/output \
    -e GENERIC_TIMEZONE=Asia/Ho_Chi_Minh \
    -e TZ=Asia/Ho_Chi_Minh \
    "$N8N_IMAGE" "$@"
}

n8n import:workflow --input=/run-files/workflow.json
status=0
n8n execute --id="$WF_ID" --rawOutput > run/result.json || status=$?

# lưu lại danh sách bài đã đăng, kể cả khi lỗi giữa chừng (để lần sau không đăng trùng bài đã hẹn giờ)
n8n export:workflow --id="$WF_ID" --output=/run-files/exported.json
jq '(if type == "array" then .[0] else . end).staticData // {}' run/exported.json > state/static-data.json

echo "Ảnh/caption đã tạo: $(find run/output -type f | wc -l | tr -d ' ') file"
exit "$status"
