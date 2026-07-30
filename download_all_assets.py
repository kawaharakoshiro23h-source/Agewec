"""北九州パレット公式フォトギャラリーの全写真を取得する一括ツール。

やること:
  1. ギャラリーを全ページ巡回して写真メタデータを収集
  2. 各画像を assets_dl/ へダウンロード（既にあればスキップ＝再開可能）
  3. sha256・ファイルサイズ・取得時刻・状態を記録
  4. enriched な asset_catalog.json を書き出す（v1/v2 が読む同じファイル）

使い方（ネット接続のある自分のMacで実行）:
  cd ~/Downloads/Agewec
  uv run python download_all_assets.py                 # 全ページ巡回＋全画像DL
  uv run python download_all_assets.py --metadata-only # DLせずカタログだけ
  uv run python download_all_assets.py --pages 3       # 先頭3ページだけ（お試し）
  uv run python download_all_assets.py --limit 10      # 先頭10件だけDL（お試し）

依存: httpx, beautifulsoup4（pyproject に既に含まれる）

注意（利用規約）:
  - 北九州市のPR・観光振興目的での利用は無料・申請不要。制作素材としての
    ローカル保存は目的に沿う。
  - 画像の二次配布は禁止。assets_dl/ は .gitignore 済みで、GitHub等へ
    コミット・再配布しないこと。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
BASE = "https://kitakyushucity.guide"
GALLERY = BASE + "/galleries"
CATALOG_PATH = ROOT / "asset_catalog.json"
DL_DIR = ROOT / "assets_dl"
UA = {"User-Agent": "Mozilla/5.0 (AGEWEC asset fetcher)"}
RIGHTS_STATUS = "approved_for_tourism_promotion"
USAGE_SCOPE = "agewec_submission"


def _abs(url: str) -> str:
    return BASE + url if url.startswith("/") else url


def parse_gallery(html: str) -> list[dict]:
    """1ページのHTMLから写真エントリを抽出。"""
    soup = BeautifulSoup(html, "html.parser")
    photos: list[dict] = []
    for a in soup.select('a[href*="/galleries/detail/"]'):
        title = a.get_text(strip=True)
        if not title:
            continue
        img = a.find_previous("img", src=lambda s: s and "/files/Photos/" in s)
        image_url = _abs(img["src"]) if img else ""
        genres, areas = [], []
        for tag in a.find_all_next("a", href=True, limit=6):
            href = tag["href"]
            if "genre%5B%5D=" in href or "genre[]=" in href:
                genres.append(tag.get_text(strip=True))
            elif "areas%5B%5D=" in href or "areas[]=" in href:
                areas.append(tag.get_text(strip=True))
            elif "/galleries/detail/" in href:
                break
        photos.append({
            "title": title,
            "detail_url": _abs(a["href"]),
            "image_url": image_url,
            "genres": genres,
            "areas": areas,
        })
    return photos


def collect_metadata(client: httpx.Client, max_pages: int) -> list[dict]:
    """全ページを巡回して写真メタデータ一覧を返す（重複除去）。"""
    seen: set[str] = set()
    photos: list[dict] = []
    for page in range(1, max_pages + 1):
        url = f"{GALLERY}?page={page}&sort=Photos.published"
        r = client.get(url, timeout=30.0, headers=UA)
        r.raise_for_status()
        page_photos = parse_gallery(r.text)
        new = [p for p in page_photos if p["detail_url"] not in seen]
        for p in new:
            seen.add(p["detail_url"])
        photos.extend(new)
        print(f"  page {page:>2}: +{len(new)}件 (累計 {len(photos)})")
        if not new:
            break
    return photos


_UNSAFE = re.compile(r'[\s　・/\\:*?"<>|~〜～]+')

# 長いジャンル名の短縮
_GENRE_SHORT = {
    "イルミネーション・夜景": "夜景",
    "祭り・イベント": "祭り",
}
_MAX_TITLE = 24  # 写真名が長すぎる場合の上限（文字数）


def _sanitize(text: str) -> str:
    """ファイル名に使えない文字（・ 空白 / \\ ~ 等）を除去する。"""
    return _UNSAFE.sub("", text) or "x"


def _short_genre(genres: list[str]) -> str:
    if not genres:
        return "notag"
    g = genres[0]
    return _sanitize(_GENRE_SHORT.get(g, g))


def _short_area(areas: list[str]) -> str:
    if not areas:
        return "noarea"
    a = areas[0].replace("エリア", "").split("・")[0]  # 「エリア」除去＋先頭区画
    return _sanitize(a)


def _short_title(title: str) -> str:
    t = _sanitize(title)
    return t[:_MAX_TITLE] if len(t) > _MAX_TITLE else t


def _local_name(asset_id: str, genres: list[str], areas: list[str],
                title: str, image_url: str) -> str:
    """ID＋主ジャンル＋エリア＋写真名でファイル名を作る。

    例: asset-001_夜景_皿倉_皿倉山夜景03.jpg
    asset_id が一意なので衝突は起きない。
    """
    ext = Path(urlparse(image_url).path).suffix or ".jpg"
    return (f"{asset_id}_{_short_genre(genres)}_{_short_area(areas)}"
            f"_{_short_title(title)}{ext}")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_all(photos: list[dict], client: httpx.Client, *,
                 metadata_only: bool, limit: int | None) -> list[dict]:
    """各写真をDLし、enrichedなレコード一覧を返す。"""
    DL_DIR.mkdir(parents=True, exist_ok=True)
    enriched: list[dict] = []
    ok = skipped = failed = 0

    for i, p in enumerate(photos, start=1):
        asset_id = f"asset-{i:03d}"
        rec = {
            "asset_id": asset_id,
            "title": p["title"],
            "detail_url": p["detail_url"],
            "image_url": p["image_url"],
            "genres": p["genres"],
            "areas": p["areas"],
            "usage_scope": USAGE_SCOPE,
            "rights_status": RIGHTS_STATUS,
            "local_path": None,
            "file_size_bytes": None,
            "sha256": None,
            "acquired_at": None,
            "download_status": "metadata_only",
        }

        if not metadata_only and p["image_url"]:
            name = _local_name(
                asset_id, p["genres"], p["areas"], p["title"], p["image_url"])
            dest = DL_DIR / name
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    status = "skipped_exists"
                    skipped += 1
                else:
                    resp = client.get(p["image_url"], timeout=60.0, headers=UA)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                    status = "success"
                    ok += 1
                rec["local_path"] = f"assets_dl/{name}"
                rec["file_size_bytes"] = dest.stat().st_size
                rec["sha256"] = _sha256_of(dest)
                rec["acquired_at"] = datetime.now(timezone.utc).isoformat()
                rec["download_status"] = status
            except Exception as exc:  # noqa: BLE001
                rec["download_status"] = "failed"
                rec["error"] = f"{type(exc).__name__}: {exc}"
                failed += 1

        enriched.append(rec)
        if i % 20 == 0 or i == len(photos):
            print(f"  {i}/{len(photos)} (成功{ok}/既存{skipped}/失敗{failed})")
        if limit and i >= limit:
            print(f"  --limit {limit} に達したため停止")
            break

    print(f"\nDL結果: 成功 {ok} / 既存スキップ {skipped} / 失敗 {failed}")
    return enriched


def main() -> None:
    ap = argparse.ArgumentParser(description="北九州パレット 全写真ダウンローダ")
    ap.add_argument("--pages", type=int, default=15, help="巡回する最大ページ数")
    ap.add_argument("--metadata-only", action="store_true",
                    help="画像本体はDLせずカタログのみ作成")
    ap.add_argument("--limit", type=int, default=None,
                    help="先頭N件だけDL（お試し用）")
    args = ap.parse_args()

    print(f"ギャラリー巡回中（最大{args.pages}ページ）...")
    with httpx.Client(follow_redirects=True) as client:
        photos = collect_metadata(client, args.pages)
        print(f"メタデータ収集完了: {len(photos)}件\n")
        if args.metadata_only:
            print("画像DLはスキップ（--metadata-only）")
        else:
            print("画像をダウンロード中...")
        enriched = download_all(
            photos, client,
            metadata_only=args.metadata_only, limit=args.limit,
        )

    catalog = {
        "source": GALLERY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(enriched),
        "usage_scope": USAGE_SCOPE,
        "rights_note": "北九州市のPR・観光振興目的で利用可。二次配布は禁止。",
        "photos": enriched,
    }
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nカタログ書き出し: {CATALOG_PATH}  ({len(enriched)}件)")
    downloaded = sum(1 for r in enriched
                     if r["download_status"] in {"success", "skipped_exists"})
    if not args.metadata_only:
        print(f"画像ローカル保存: {downloaded}件 → {DL_DIR}")

    # ジャンル別件数
    from collections import Counter
    g = Counter(x for r in enriched for x in r["genres"])
    print("\nジャンル別:")
    for name, n in g.most_common():
        print(f"  {name}: {n}件")


if __name__ == "__main__":
    sys.exit(main())
