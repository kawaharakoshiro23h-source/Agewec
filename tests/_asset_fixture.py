"""Small, generated asset fixture for tests that exercise asset selection."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image


_ASSETS = (
    ("小倉城昼景", "観光スポット", "小倉"),
    ("門司港駅昼景", "観光スポット", "門司港"),
    ("皿倉山夜景", "イルミネーション・夜景", "皿倉"),
    ("若戸大橋ライトアップ", "イルミネーション・夜景", "若戸"),
    ("小倉夕景", "観光スポット", "小倉"),
    ("門司港夕暮", "観光スポット", "門司港"),
    ("工場昼景", "観光スポット", "若松"),
    ("港夜景", "イルミネーション・夜景", "門司港"),
)


def configure_test_assets(config: dict[str, Any], root: Path) -> None:
    """Point a config at generated metadata and images under ``root``."""
    assets_dir = root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    photos = []
    for index, (title, genre, area) in enumerate(_ASSETS, start=1):
        image_path = assets_dir / f"fixture-{index:02d}.jpg"
        Image.new(
            "RGB",
            (16, 16),
            color=((index * 29) % 256, (index * 53) % 256, 96),
        ).save(image_path, format="JPEG")
        photos.append(
            {
                "title": title,
                "image_url": f"https://example.invalid/{image_path.name}",
                "detail_url": "https://example.invalid/asset",
                "genres": [genre],
                "areas": [area],
                "local_path": str(image_path),
            }
        )

    catalog_path = root / "asset_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {"source": "generated-test-fixture", "photos": photos},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paths = config.setdefault("paths", {})
    paths["assets_dir"] = str(assets_dir)
    paths["asset_catalog"] = str(catalog_path)
