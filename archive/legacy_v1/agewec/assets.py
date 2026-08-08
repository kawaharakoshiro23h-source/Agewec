"""[LEGACY v1] Asset ステージの素材カタログ生成と照合。

■ カタログ生成（ネット接続が要る。あなたのMacで実行する前提）
    uv run python -m agewec.assets            # フォトギャラリーを巡回して asset_catalog.json を作る
    uv run python -m agewec.assets --pages 3  # 先頭3ページだけ（お試し）

  出力 asset_catalog.json:
    {"photos": [
        {"title": "皿倉山夜景03",
         "detail_url": "https://.../galleries/detail/xxxx",
         "image_url": "https://.../files/Photos/xxxx/....jpg",
         "genres": ["イルミネーション・夜景"],
         "areas":  ["皿倉・河内・東田エリア"]}, ... ]}

■ 照合（パイプライン内で使う）
    load_catalog() でカタログを読み、pick_for_cut() でカットに合う実写を1枚選ぶ。
    ネット/カタログが無い環境では load_catalog() は None を返し、全カット生成に倒れる。

  ※ 取得コードは公式サイトのDOMに依存する。初回実行時に取れ方を確認し、必要なら
     parse_gallery() のセレクタを微調整すること。利用は「観光振興用途」の規約に従う。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[2] / "asset_catalog.json"

BASE = "https://kitakyushucity.guide"
GALLERY = BASE + "/galleries"

# 狙う賞 → 優先ジャンル（実写照合のヒント）
AWARD_GENRE = {
    "夜景賞": "イルミネーション・夜景",
    "観光賞": "観光スポット",
    "環境賞": "公園",
    "DX賞": None,   # 特定ジャンル無し → 生成寄り
}


# ---------------------------------------------------------------- カタログ生成
def _abs(url: str) -> str:
    """相対URL（/files/... 等）を絶対URLに直す。"""
    if url.startswith("/"):
        return BASE + url
    return url


def parse_gallery(html: str) -> list[dict]:
    """1ページのHTMLから写真エントリを抽出する。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    photos: list[dict] = []
    # 詳細リンクを起点に各写真を拾う
    for a in soup.select('a[href*="/galleries/detail/"]'):
        title = a.get_text(strip=True)
        if not title:
            continue
        detail_url = _abs(a["href"])
        # 直近の画像（/files/Photos/ を含む img）
        img = a.find_previous("img", src=lambda s: s and "/files/Photos/" in s)
        image_url = _abs(img["src"]) if img else ""
        # 後続のタグ（genre[] / areas[]）
        genres, areas = [], []
        for tag in a.find_all_next("a", href=True, limit=6):
            href = tag["href"]
            if "genre%5B%5D=" in href or "genre[]=" in href:
                genres.append(tag.get_text(strip=True))
            elif "areas%5B%5D=" in href or "areas[]=" in href:
                areas.append(tag.get_text(strip=True))
            elif "/galleries/detail/" in href:
                break  # 次の写真に到達
        photos.append({"title": title, "detail_url": detail_url,
                       "image_url": image_url, "genres": genres, "areas": areas})
    return photos


def build_catalog(max_pages: int = 15) -> dict:
    """ギャラリーを巡回してカタログを組み立てて保存する。"""
    import httpx

    all_photos: list[dict] = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = f"{GALLERY}?page={page}&sort=Photos.published"
        r = httpx.get(url, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        page_photos = parse_gallery(r.text)
        new = [p for p in page_photos if p["detail_url"] not in seen]
        for p in new:
            seen.add(p["detail_url"])
        all_photos.extend(new)
        if not new:  # これ以上増えなければ終端
            break
    catalog = {"source": GALLERY, "count": len(all_photos), "photos": all_photos}
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return catalog


# ---------------------------------------------------------------- ダウンロード
DL_DIR = Path(__file__).resolve().parents[2] / "assets_dl"


def download_image(url: str, dest_dir: Path = DL_DIR) -> str | None:
    """画像URLを実ファイルとして保存し、ローカルパスを返す。

    既に落としてあれば再取得しない（キャッシュ）。失敗時は None。
    """
    if not url:
        return None
    import httpx
    from urllib.parse import unquote, urlparse

    name = unquote(Path(urlparse(url).path).name) or "asset.jpg"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / name
    if out.exists():
        return str(out)
    try:
        r = httpx.get(url, timeout=60.0, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        out.write_bytes(r.content)
        return str(out)
    except Exception:
        return None


# ---------------------------------------------------------------- 照合（実行時）
def load_catalog() -> dict | None:
    if CATALOG_PATH.exists():
        try:
            return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def pick_for_cut(catalog: dict, target_genre: str | None,
                 used: set[str]) -> dict | None:
    """指定ジャンルに合う未使用の実写を1枚返す。無ければ None。"""
    if not catalog or not target_genre:
        return None
    for p in catalog.get("photos", []):
        if p["detail_url"] in used:
            continue
        if target_genre in p.get("genres", []):
            return p
    return None


if __name__ == "__main__":
    pages = 15
    if "--pages" in sys.argv:
        pages = int(sys.argv[sys.argv.index("--pages") + 1])
    print(f"ギャラリーを巡回中（最大{pages}ページ）...")
    cat = build_catalog(pages)
    print(f"カタログ生成: {cat['count']}件 → {CATALOG_PATH}")
    # ジャンル別の件数を軽く表示
    from collections import Counter
    g = Counter(g for p in cat["photos"] for g in p["genres"])
    for name, n in g.most_common():
        print(f"  {name}: {n}件")
