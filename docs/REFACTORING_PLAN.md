# リファクタリング計画（締切前・低リスク版）

作成日: 2026-07-31 / 前提: 8/2 中間提出・8/7 最終提出

## 0. 大前提（調査で判明した事実）

当初「`nodes.py` / `nodes_llm.py` は旧実装の死にパス」と考えていたが、
依存関係を追ったところ **両方とも本番経路が現役で依存している**。

```
run.py → graph_safe.py → nodes_runtime.py ─┬→ nodes_llm.py ──→ nodes.py（deterministic fallback）
                                            └→ pipeline_runtime.py → nodes / nodes_llm
```

- `nodes.py` … 決定論フォールバック＋共有ヘルパ（`_load_catalog`, `_local_asset_path`, `_complete` 等）
- `nodes_llm.py` … 各役割のLLM実行（asset_curator / director / visual_qa …）
- `nodes_runtime.py` … 本番のノード束ね
- `pipeline_runtime.py` … 実処理（ComfyUI・FFmpeg・QA）

**したがってファイル単位の移動・削除は不可。** 死んでいるのは
「ファイル」ではなく「一部の関数（旧post_production 等の `ffmpeg_pending` 経路）」のみ。

## 1. 方針

**メイン実行に影響を与えない範囲に限定する。**

- 「削除」ではなく **「印をつける・整理する・文書化する」** を中心にする
- ふるまいを変える変更（ロジック改変・分割・リネーム）は **8/7以降**
- 各ステップ後に **テスト19件** を実行して緑を確認する

## 2. 実施する項目（低リスク順）

### A. 正式経路の明示（リスク: なし／効果: 大）

各モジュール冒頭のdocstringに「本番経路のどこか」「誰から呼ばれるか」を1〜3行で明記する。

- `graph_safe.py` … 本番グラフ（正）／`graph.py` があるなら旧版と明記
- `nodes_runtime.py` … 本番ノード（正）
- `nodes_llm.py` … 役割別LLM。`nodes.py` をフォールバックに使用
- `nodes.py` … 決定論フォールバック＋共有ヘルパ（**単独では使わない**）
- `pipeline_runtime.py` … 実処理（ComfyUI/FFmpeg/QA）

→ 人間もAIも「どれが正か」を最初の数行で判断でき、無駄な読み込みが減る。

### B. 未使用関数に LEGACY 印（リスク: なし／効果: 中）

`ffmpeg_pending` を返す旧 post_production 系など、**本番から呼ばれない関数**の
docstring 先頭に `[LEGACY 未使用]` と1行付ける。**削除はしない**（テストが参照する可能性）。

対象候補（実装前に呼び出し元を再確認）:
- `nodes.py` の post_production（`ffmpeg_pending`）
- `nodes_llm.py` の post_production（`ffmpeg_pending`）

### C. README に構成マップを追加（リスク: なし／効果: 大）

`workflow_v2/README.md` に「実行経路」と「ファイルの役割」の表を1つ足す。
新規参加者（および他のAIツール）が最初に読む地図になる。

### D. 未使用 import の除去（リスク: 低／効果: 小）

`unquote` / `urlparse` など、`_local_asset_path` 改修で使われなくなった可能性のある
import を、静的チェックで確認してから削除する。**1ファイルずつ、テストを回して確認。**

### E. 一時ファイル・生成物の整理（リスク: なし／効果: 小）

- `work/production/` の試行 mp4、`submissions/` の古い run を整理（残す run を1〜2個決める）
- **注意**: 画像・動画はAIのトークンを消費しない（メタデータのみ参照）。
  ディスク節約と見通しの改善が目的であり、AI負荷対策としては B・C の方が効く。

## 3. やらないこと（8/7以降に回す）

- `pipeline_runtime.py`（1907行）の分割
- `nodes.py` / `nodes_llm.py` / `nodes_runtime.py` の統廃合・リネーム
- ディレクトリ再編（`legacy/` への移動を含む）
- 型付け強化・共通化などの構造変更

理由: いずれも**動作中の安定点（1カット統合テスト 11チェック pass）を壊すリスク**があり、
締切前に得るものより失うものが大きい。

## 4. 手順

1. テストを実行し、**現状が緑であることを記録**（ベースライン）
2. A → B → C → D → E の順に実施
3. **各ステップ後にテスト19件を実行**
4. 1カット統合テスト（`test_pipeline_1cut`）が pass することを最後に確認
5. 変更点を README に追記

## 5. 完了条件

- テスト19件が緑
- `test_pipeline_1cut` が status: pass
- README を見れば「本番経路とファイルの役割」が分かる
- ふるまいの変更がゼロ（差分はdocstring・README・未使用import・生成物のみ）
