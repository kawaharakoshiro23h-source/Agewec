# Phase 10: Provenance & Submission Package

## 状態

`implemented`

## 目的

H3承認後、提出動画だけでなく、全Agentの出力、判断、承認、修正履歴、
技術検査を一つのパッケージへまとめる。

## 出力

```text
runtime/submissions/<run_id>/
├── final_video.mp4
├── manifest.json
├── provenance.json
├── process_report.html
├── process_report.md
├── decision_log.jsonl
├── technical_report.json
├── review_summary.json
├── storyboard.json
├── direction_plan.json
└── qa_frames/
```

## 実装済みの内容

- 実行IDごとの独立ディレクトリ
- 最終MP4のコピー
- Project、Config、全Phase Result、Review、Event、Artifactの保存
- API Key、Token、Secretのマスク
- StoryboardとDirection Planの個別保存
- 人間が読めるMarkdown / HTML制作レポート
- 判断時系列のJSON Lines
- QA代表フレームの同梱
- ファイルサイズとSHA-256を持つManifest
- `final_output`を提出用`final_video.mp4`へ設定

保存するのは外部説明可能な判断、フィードバック、Evidenceであり、
モデル内部のChain-of-Thoughtは保存しない。

正常時は`provenance: on_exception`で自動完了し、パッケージ生成エラー時だけ
人間確認を出せる。
