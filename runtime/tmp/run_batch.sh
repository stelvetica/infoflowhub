#!/usr/bin/env bash
cd /c/Users/ETT/Playground/infoflowhub || exit 1
LOG=runtime/tmp/digest_batch2.log
{
  echo "[start-psy] $(date -Is)"
  BSK_AUTO_START=0 .venv/Scripts/python -m apps.laterhub.video_digest \
    --source douyin_favorite \
    --tag "心理_认知/学习方法,心理_自我/情绪分析,心理_他人/关系,心理_职场" \
    --limit 300 --keep-temp --retry-failed
  echo "[end-psy] $(date -Is)"
  PAUSE=$((RANDOM % 180 + 120))
  echo "[pause] ${PAUSE}s"
  sleep "$PAUSE"
  echo "[start-social] $(date -Is)"
  BSK_AUTO_START=0 .venv/Scripts/python -m apps.laterhub.video_digest \
    --source douyin_favorite \
    --tag "社科_历史,社科_哲学/社会/经济/金融" \
    --limit 100 --keep-temp
  echo "[end-social] $(date -Is)"
} >> "$LOG" 2>&1
