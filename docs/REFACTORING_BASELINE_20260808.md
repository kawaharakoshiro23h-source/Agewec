# リファクタリング開始時ベースライン

記録日: 2026-08-08  
原本: `/Users/koshiro/Downloads/Agewec`  
作業版: `/Users/koshiro/Downloads/Agewecのコピー`

## Git

- 開始コミット: `c5cd4ed4440425d423bb8e1a15b278612d56b931`
- 開始ブランチ: `main`
- 原本の作業ツリー: clean
- 作業版の開始差分: `docs/REFACTORING_EXECUTION_GUIDE.md`のみ新規

## 容量と構成

- プロジェクト全体: 2.7GB
- `workflow_v2`: 1.6GB
- `workflow_v2/work`: 1.1GB
- `workflow_v2/submissions`: 479MB
- `assets_dl`: 337MB
- `AGEWEC2026_提出資料`: 520MB
- テストファイル: 25

## テスト

実行コマンド:

```bash
PYTHONPATH=workflow_v2 /Users/koshiro/Downloads/Agewec/.venv/bin/python \
  -m unittest discover -s workflow_v2/tests -v
```

結果:

```text
Ran 240 tests in 19.369s
239 passed / 1 environment error
```

既知の開始時エラー:

```text
test_monitor.ServerTest.test_serves_a_downscaled_asset_photo
ModuleNotFoundError: No module named 'PIL'
```

原因はテスト対象の機能が`Pillow`を利用する一方、`pyproject.toml`の依存関係に
`Pillow`が登録されていないこと。リファクタリングによる回帰ではなく、開始時点から
存在する環境再現性の欠陥として第1段階で修正する。

`pytest`は開始環境にインストールされていないため、正式な基準コマンドには
標準ライブラリの`unittest discover`を使用する。

## 提出物SHA-256

原本と作業版で、以下のハッシュが一致することを確認済み。

| ファイル | SHA-256 |
|---|---|
| `01_作品/agewec_48s_final.mp4` | `60527b5762c63820c12272141a12f8afa1cda265ef4ff48a86d4c8c39d3b42a8` |
| `02_制作過程/process_report.html` | `dde6b523e0848c263838d6c5c048b8a8fbc1186bf04faf7015161aba588457e6` |
| `02_制作過程/process_report.pdf` | `536718842f011f631218e9e49e25e6c64455825af9cf1998b14957443019d979` |
| `03_証跡/provenance.json` | `e69ce200c612865069b033885f972bf1d52635cb9db6023217af5aaf7cc49598` |
| `03_証跡/manifest.json` | `4ef43045831eb80d4ad3cd43554f9e713eaf8ec815d5d64c077c2c14a34d2de7` |

提出物については、リファクタリング後も上記のバイト列一致を要求する。
パイプラインで再生成するmock動画については、コンテナメタデータ差を考慮し、
SHA-256ではなく尺・解像度・ストリーム・代表フレーム等で同等性を確認する。

## 外部API

- ベースライン測定ではRunway APIを呼び出していない
- 今後も明示的な個別承認がない限り、リファクタリング検証で有料APIを使用しない

