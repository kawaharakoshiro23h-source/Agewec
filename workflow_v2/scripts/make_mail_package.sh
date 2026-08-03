#!/usr/bin/env bash
# メール添付用の成果物を作る。
#
# 課題:
#   提出パッケージは 80MB 前後あり、Gmail / Outlook の上限（20〜25MB、
#   しかも転送時に約1.33倍へ膨張）を超える。さらにレポートは相対パスで
#   メディアを参照するため、zipを展開せずプレビューで開くと画像も動画も
#   表示されない。受け手の操作に依存する形は避けたい。
#
# 方針:
#   画像・動画をすべて data URI としてHTMLに埋め込み、1ファイルで完結させる。
#   展開もフォルダ構成も不要で、ダブルクリックすればそのまま見られる。
#
# 出力:
#   submissions/agewec_<run_id>_report.html  … 単体で開けるレポート
#   submissions/agewec_<run_id>_video.mp4    … 視聴用の最終動画
#
# 原本（submissions/<run_id>/）は変更しない。最終提出には原本を使うこと。
#
# 使い方:
#   ./scripts/make_mail_package.sh run-6e4ff40c1b
set -euo pipefail

RUN_ID="${1:?使い方: $0 <run_id>   例: $0 run-6e4ff40c1b}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/submissions/$RUN_ID"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

OUT_HTML="$ROOT/submissions/agewec_${RUN_ID}_report.html"
OUT_VIDEO="$ROOT/submissions/agewec_${RUN_ID}_video.mp4"

[ -d "$SRC" ] || { echo "提出パッケージが見つかりません: $SRC" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg が必要です" >&2; exit 1; }

echo "メディアを圧縮しています..."

# レポート内に埋め込む用（容量優先。表示サイズに合わせて縮小する）
ffmpeg -y -v error -i "$SRC/final_video.mp4" \
  -vf scale=960:-2 -c:v libx264 -crf 28 -preset slow "$WORK/final_video.mp4"
for f in "$SRC"/artifacts/cuts/*.mp4; do
  ffmpeg -y -v error -i "$f" -vf scale=640:-2 \
    -c:v libx264 -crf 30 -preset medium "$WORK/$(basename "$f")"
done
for f in "$SRC"/artifacts/sources/*.jpg; do
  ffmpeg -y -v error -i "$f" -vf scale=960:-2 -q:v 5 "$WORK/$(basename "$f")"
done

# 添付する視聴用の動画（レポート埋め込み版より高画質）
ffmpeg -y -v error -i "$SRC/final_video.mp4" \
  -c:v libx264 -crf 26 -preset slow "$OUT_VIDEO"

echo "HTMLへ埋め込んでいます..."
python3 - "$SRC/process_report.html" "$WORK" "$OUT_HTML" <<'PY'
import base64, mimetypes, os, re, sys

source_html, work_dir, out_path = sys.argv[1:4]
html = open(source_html, encoding="utf-8").read()

# src="..." と src='...' の両方を拾う（HTMLは箇所によって引用符が異なる）
pattern = re.compile(r"""src=(['"])([^'"#:]+\.(?:jpg|jpeg|png|mp4))\1""")


def data_uri(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    payload = base64.b64encode(open(path, "rb").read()).decode()
    return f"data:{mime};base64,{payload}"


cache: dict[str, str] = {}


def replace(match: re.Match) -> str:
    quote, ref = match.group(1), match.group(2)
    small = os.path.join(work_dir, os.path.basename(ref))
    if not os.path.exists(small):
        return match.group(0)
    if small not in cache:
        cache[small] = data_uri(small)
    return f"src={quote}{cache[small]}{quote}"


embedded = html
count = len(pattern.findall(html))
embedded = pattern.sub(replace, html)
remaining = [m.group(2) for m in pattern.finditer(embedded)
             if not m.group(2).startswith("data:")]
open(out_path, "w", encoding="utf-8").write(embedded)

print(f"  参照 {count}件 / 埋め込み {count - len(remaining)}件 / 未処理 {len(remaining)}件")
for name in remaining:
    print("   ★未埋め込み:", name)
sys.exit(1 if remaining else 0)
PY

echo
echo "作成しました:"
du -h "$OUT_HTML" "$OUT_VIDEO"
echo
python3 - "$OUT_HTML" "$OUT_VIDEO" <<'PY'
import os, sys
total = sum(os.path.getsize(p) for p in sys.argv[1:3]) / 1024 / 1024
print(f"  添付合計 {total:.1f}MB（転送時 約{total * 1.33:.0f}MB / 上限は20〜25MB）")
print("  " + ("送信できます" if total * 1.33 < 20 else "上限に近いので注意してください"))
PY
