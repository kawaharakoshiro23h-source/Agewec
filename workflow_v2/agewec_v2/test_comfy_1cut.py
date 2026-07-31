"""#3+#4: 実ComfyUI 1カット生成テスト＆尺検証（LLM/フルパイプライン不要）。

実写1枚を LTX(image-to-video) で動画化し、ffprobe で
「要求フレーム数 ＝ 実際のフレーム数/尺」を検証する。

前提（あなたのMacで実行）:
  - ComfyUI Desktop を起動し、LTX i2v ワークフローを開いた状態
  - workflows/ltx_i2v_api.json が API形式で書き出し済み
  - まず `python -m agewec_v2.comfy_check` が status: ready を返すこと

実行:
  cd ~/Downloads/Agewec/workflow_v2
  python -m agewec_v2.test_comfy_1cut                    # 既定: 8秒・draftプロファイル
  python -m agewec_v2.test_comfy_1cut --seconds 8 --profile final
  python -m agewec_v2.test_comfy_1cut --image assets_dl/asset-001_夜景_皿倉_....jpg

判定:
  #3 = 動画が生成・ダウンロードでき、デコード可能
  #4 = actual_frames == requested_frames かつ actual_seconds ≈ requested_seconds
       （短ければ LTX が要求フレームを守っていない＝要調整）
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml

from . import nodes as det
from .backends.comfy_runtime import ComfyClient, ComfyGenerationRequest
from .pipeline_runtime import _ltx_frame_count

ROOT = Path(__file__).resolve().parents[1]

SAMPLE_PROMPT = (
    "Cinematic footage of a real Kitakyushu night cityscape. Preserve the real "
    "architecture, terrain, harbor and roads. Only the city lights shimmer "
    "gently. Camera performs a slow, stable push-in. High detail, no distortion."
)
SAMPLE_NEGATIVE = "distorted architecture, flickering, motion smear, warped buildings"


def _pick_local_image() -> str | None:
    """asset_catalog.json からローカル実在の画像を1枚選ぶ（夜景優先）。"""
    catalog = det._load_catalog()
    photos = catalog.get("photos", [])
    night, other = None, None
    for p in photos:
        lp = det._local_asset_path(p)
        if not lp:
            continue
        other = other or lp
        title = str(p.get("title", ""))
        if any(t in title for t in ("夜", "ライトアップ", "イルミネーション")):
            night = lp
            break
    return night or other


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries",
         "stream=nb_read_frames,r_frame_rate,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    num, den = (stream.get("r_frame_rate", "0/1").split("/") + ["1"])[:2]
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "frames": int(stream.get("nb_read_frames", 0)),
        "fps": round(fps, 3),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": round(float(data.get("format", {}).get("duration", 0)), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="実ComfyUI 1カット生成＆尺検証")
    ap.add_argument("--config", type=Path, default=ROOT / "config_llm.yaml")
    ap.add_argument("--image", type=str, default=None, help="入力画像（相対/絶対）")
    ap.add_argument("--seconds", type=float, default=8.0, help="目標尺（秒）")
    ap.add_argument("--profile", choices=["draft", "final"], default="draft")
    ap.add_argument("--prompt", type=str, default=SAMPLE_PROMPT)
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    comfy = config.get("comfy", {})
    prod = config.get("production", {})
    profile = prod.get("profiles", {}).get(args.profile, {})
    fps = int(profile.get("fps", 24))
    width = int(profile.get("width", 576))
    height = int(profile.get("height", 384))
    steps = int(profile.get("steps", 20))
    mc = prod.get("model_constraints", {})
    frames = _ltx_frame_count(
        args.seconds, fps,
        multiple=int(mc.get("frame_multiple", 8)),
        offset=int(mc.get("frame_offset", 1)),
    )
    max_frames = int(mc.get("max_frames", 257))
    if frames > max_frames:
        raise SystemExit(
            f"要求{frames}フレームがモデル上限{max_frames}を超過。--secondsを短く。"
        )
    expected_seconds = round(frames / fps, 3)

    image = args.image or _pick_local_image()
    if not image:
        raise SystemExit("ローカル画像が見つかりません。先にdownload_all_assets.pyを実行。")
    image_path = image if Path(image).is_absolute() else str(det.PROJECT_ROOT / image)
    if not Path(image_path).exists():
        raise SystemExit(f"画像が存在しません: {image_path}")

    print("=== 生成設定 ===")
    print(f" image   : {image_path}")
    print(f" profile : {args.profile} ({width}x{height}, {fps}fps, steps={steps})")
    print(f" seconds : {args.seconds}  -> frames={frames}  (期待尺≈{expected_seconds}s)")
    print()

    client = ComfyClient(
        base_url=comfy.get("base_url", "http://127.0.0.1:8188"),
        workflow_path=ROOT / comfy.get("workflow_api_json", "workflows/ltx_i2v_api.json"),
        input_mapping=comfy.get("inputs", {}),
        output_dir=ROOT / "work" / "production",
        poll_interval=float(comfy.get("poll_interval_seconds", 2)),
        timeout=float(comfy.get("timeout_seconds", 1800)),
    )
    request = ComfyGenerationRequest(
        image_path=image_path,
        positive_prompt=args.prompt,
        negative_prompt=SAMPLE_NEGATIVE,
        width=width, height=height, frames=frames, steps=steps, fps=fps,
        seed=12345, file_prefix="agewec_test_1cut",
    )

    print("=== ComfyUIへ生成投入中（時間がかかります）... ===")
    result = client.generate(request)
    out_path = Path(result["output_path"])
    print(f" 生成完了: {out_path}  ({result['elapsed_seconds']}s)")
    print()

    print("=== #3 生成 & #4 尺検証（ffprobe）===")
    probe = _ffprobe(out_path)
    print(f" 実測: frames={probe['frames']} / fps={probe['fps']} / "
          f"{probe['width']}x{probe['height']} / duration={probe['duration']}s")
    frame_ok = probe["frames"] == frames
    dur_ok = abs(probe["duration"] - expected_seconds) <= 0.25
    print()
    print(f" [#3] 生成・デコード : {'OK' if probe['frames'] > 0 else 'NG'}")
    print(f" [#4] フレーム一致   : {'OK' if frame_ok else 'NG'} "
          f"(要求{frames} / 実測{probe['frames']})")
    print(f" [#4] 尺一致(±0.25s) : {'OK' if dur_ok else 'NG'} "
          f"(期待{expected_seconds}s / 実測{probe['duration']}s)")
    if not (frame_ok and dur_ok):
        print("\n※ 不一致＝LTXが要求フレーム数を守っていない可能性。"
              "profile/steps/モデル版を見直す。Phase08は短尺をエラー扱いにする設計。")


if __name__ == "__main__":
    main()
