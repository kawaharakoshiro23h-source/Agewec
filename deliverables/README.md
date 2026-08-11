# Deliverables

提出済み成果物と、その派生版をソースコード・実行生成物から分離して保管する場所です。大容量ファイルと提出資料本体はGit追跡外です。

```text
deliverables/
├── AGEWEC2026/   AGEWEC 2026へ提出した確定パッケージ
└── variants/     字幕のみ・BGMのみ等の派生動画
```

## 取り扱い

- `AGEWEC2026/`は確定提出物です。内容を編集・再生成・上書きしません。
- `variants/`は比較・保存用であり、確定提出物とは区別します。
- 新しいパイプライン実行結果は`runtime/submissions/`へ出力します。確認・採用後にだけ、明示的にこちらへ移します。
- 提出関連の文章は`docs/submission/`へ置きます。

## Stage 7固定ハッシュ

| ファイル | SHA-256 |
| --- | --- |
| `AGEWEC2026/01_作品/agewec_48s_final.mp4` | `60527b5762c63820c12272141a12f8afa1cda265ef4ff48a86d4c8c39d3b42a8` |
| `AGEWEC2026/02_制作過程/process_report.html` | `dde6b523e0848c263838d6c5c048b8a8fbc1186bf04faf7015161aba588457e6` |
| `AGEWEC2026/02_制作過程/process_report.pdf` | `536718842f011f631218e9e49e25e6c64455825af9cf1998b14957443019d979` |
| `AGEWEC2026/03_証跡/provenance.json` | `e69ce200c612865069b033885f972bf1d52635cb9db6023217af5aaf7cc49598` |
| `AGEWEC2026/03_証跡/manifest.json` | `4ef43045831eb80d4ad3cd43554f9e713eaf8ec815d5d64c077c2c14a34d2de7` |

Stage 7では確定パッケージ全75ファイルについても移動前後のハッシュ一覧を比較し、差分がないことを確認しています。
