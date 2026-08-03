# 既知の不具合と修正案

最終更新: 2026-08-03 / 対象: `workflow_v2`

2026-08-02〜03 の実Runway API実行（run-dc798306a8 / run-16a5d19e18 / run-6e72f3fe8e）で
判明した問題をまとめる。**すべて実行ログ・生成物から再現確認済み**であり、推測は
「未確定」と明記している。

優先度の基準:

- **P0** … 実行が完走しない / 課金が無駄になる。8/7提出前に直す
- **P1** … 完走はするが、判断を誤らせる or 手戻りが発生する
- **P2** … 締切後の改善。設計上の負債

---

## P0-1. 巨大画像でRunwayアップロードが失敗する

### 症状

該当カットの動画が生成されず、QAが「動画生成結果のファイルが作成されなかった」
と報告する。**課金は発生しない**（アップロード段階で落ちるため）。

### 再現データ

| run | cut | 素材 | サイズ | 画素 | 結果 |
| --- | --- | --- | --- | --- | --- |
| dc798306a8 | 1 | asset-020 皿倉山夕景 | 1.4MB | 5814×3876 | ✅ |
| 16a5d19e18 | 1 | **asset-001 皿倉山夜景03** | **13.2MB** | **8136×5424** | ❌ |
| 16a5d19e18 | 2 | asset-006 皿倉山夜景05 | 0.9MB | 2560×1707 | ✅ |
| 6e72f3fe8e | 1 | asset-020 皿倉山夕景 | 1.4MB | 5814×3876 | ✅ |
| 6e72f3fe8e | 2 | **asset-001 皿倉山夜景03** | **13.2MB** | **8136×5424** | ❌ |

**asset-001 だけが2回とも失敗、他は全て成功。** 再現性あり。

### 原因（未確定 / 有力な候補2つ）

例外本文がどこにも記録されていないため断定できない（→ P0-2）。ただし:

1. **アップロードのタイムアウトが60秒**。ダウンロードは900秒で、非対称。

   ```python
   # agewec_v2/backends/runway.py:73
   self._client = client or httpx.Client(timeout=60.0)   # ← uploadはこれ

   # agewec_v2/backends/runway.py:209
   self._client.stream("GET", url, timeout=self.timeout) # ← downloadは900秒
   ```

2. **Runway公式は「4Kを超える参照画像は非推奨」**。8136×5424 は4K幅の約2倍。
   （ephemeral upload のサイズ上限は200MBなので、13.2MB自体は問題ない）

### 修正案

- **即効**: `_upload_image` に `timeout=self.timeout` を明示して非対称を解消する
- **本命**: 送信前に長辺 4096px 程度へ縮小してから渡す。素材285枚に同様の巨大
  ファイルが他にもあるため、いずれ必須になる
- **暫定回避**: Asset Curator の代替候補から小さいものを `[s]` で指定する
  （asset-010 高塔山公園 12.6MB も地雷なので注意）

### 検証コマンド（無課金・アップロードのみ）

```bash
cd /Users/koshiro/Downloads/Agewec
PYTHONPATH=workflow_v2 .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
import yaml, pathlib, time
from agewec_v2.backends.runway import build_runway_backend
cfg=yaml.safe_load(open('workflow_v2/config_llm.yaml',encoding='utf-8'))
b=build_runway_backend(cfg['runway'],model='gen4.5',output_path_for=lambda c,a:'/tmp/x.mp4')
t=time.time()
print(b._upload_image(pathlib.Path('assets_dl/asset-001_夜景_皿倉_皿倉山夜景03.jpg')), f'{time.time()-t:.1f}秒')
"
```

---

## P0-2. 生成失敗の理由が画面にもディスクにも残らない

### 症状

Runway呼び出しが例外で落ちても、利用者が見るのは下流QAの
「動画生成結果のファイルが作成されなかった」だけ。**原因が一切わからない。**

### 原因

例外は捕捉され `blocking_issues` に入るが、`image_video_production` の
review policy が `never` のためゲートJSONが書かれず、そのまま破棄される。

```python
# agewec_v2/pipeline_runtime.py（生成部）
except Exception as exc:
    issues.append(f"{type(exc).__name__}: {exc}")   # ← ここには入る
```

```yaml
# config_llm.yaml:15
image_video_production: never   # ← ゲートJSONが出ない
```

run配下の全JSONを検索しても `RunwayError` / `Timeout` / `ConnectError` は
一件も見つからなかった。

### 修正案

review policy とは独立に、生成の失敗を必ずディスクへ残す。

- `cuts/cut_XX/attempt_NN_error.json` に例外の型・本文・スタックを書く
- あわせて `cut_visual_qa` の表示に上流の `blocking_issues` を転記する

**P0-1の原因特定はこれが入るまでできない。** 先にこちらを直すべき。

---

## P0-3. provenance が空パスでクラッシュする

### 症状

```
IsADirectoryError: [Errno 21] Is a directory: '.'
  pipeline_runtime.py:2573  shutil.copy2(source_video, final_video)
```

最終動画が無い状態で提出Package作成に進むと、プロセスごと落ちる。
提出パッケージもレポートも作られない。

### 原因

ガードは存在するが、**空文字が `Path('.')` になり `.exists()` を通過する**。

```python
source_video = Path(str(state.get("final_output") or ""))   # → Path('.')
if not source_video.exists():                               # → False（'.'は実在する）
    return ...エラー...
shutil.copy2(source_video, final_video)                     # → ディレクトリをコピーして例外
```

確認:

```
Path("")     → PosixPath('.')
.exists()    → True    ← ガードを素通り
.is_file()   → False   ← これなら弾ける
```

### 修正案

```python
raw = str(state.get("final_output") or "")
source_video = Path(raw)
if not raw or not source_video.is_file():
    return ...エラー...
```

1行で直る。`exists()` → `is_file()` かつ空文字チェックを追加。

---

## P0-4. CLIの入力ミスで実行が全消滅する

### 症状

```
対象カットID（全体修正はEnter）: Cut2
ValueError → プロセス終了 → 承認作業がすべて消える
```

### 原因

入力を例外処理なしで数値変換している。

```python
# agewec_v2/run.py:255
decision["target_cut_id"] = int(cut_id)      # バリデーションなし
# 同様に float(duration) も
```

さらに checkpointer が `MemorySaver`（メモリのみ）なので、
プロセスが落ちると**全状態が失われ、再開できない**。

2026-08-02〜03 に3回発生（Ctrl-C相当 / provenanceクラッシュ / 入力ミス）。

### 修正案

1. **入力の再試行ループ**（10行程度）

   ```python
   while True:
       raw = input("  対象カットID（全体修正はEnter）: ").strip()
       if not raw: break
       if raw.isdigit(): decision["target_cut_id"] = int(raw); break
       print("  数字だけを入力してください（例: 2）")
   ```

2. **`MemorySaver` → `SqliteSaver`**（数行）
   プロセスが落ちても途中から再開でき、承認のやり直しも再課金も不要になる。

### 暫定運用（修正前）

| プロンプト | 正しい入力 |
| --- | --- |
| 対象カットID | `2` または Enter |
| 目標尺 | `10` または Enter |
| 修正種別 | `direction` / `asset` / `storyboard` または Enter |
| カットQAの選択 | `y` `d` `s` `g` `n` `a` |

---

## P1-1. Sequence QA が成果物の無いカットを合格させる

### 症状

Cut 1 の動画が存在しないのに Phase 07B が

> 判定: 合格 / 全カットが承認済みで、予定尺とも一致しています

と出力。直後の Post Production が「cut 1: 成果物がない」で失敗した
（run-16a5d19e18）。

### 影響

失敗が1フェーズ遅れて発覚する。ここで止まっていれば Post Production /
provenance の無駄な実行とクラッシュ（P0-3）は起きなかった。

### 修正案

`visual_qa` で「全カットIDについて `production_artifacts` にファイルが実在するか」
を検査し、欠けていれば `revise` を返す。

---

## P1-2. カット単位の差し戻しが全カットを再実行する

### 症状

Cut 2 の QA で `[s]`（元画像を変更）を選ぶと、Asset Curator と Director が
**全カット分**再実行される（run-6e72f3fe8e で対象カット `[1, 2]` を確認）。

### 実害

今回は Cut 1 が同じ素材（asset-020）のまま出たので問題なかったが、
**LLMが別の素材を選び直す可能性があった**。運が良かっただけ。

なお生成済み・承認済みカットの動画は `approved_cut_ids` により保護されており、
再生成・再課金は起きない（`test_approved_cuts_are_not_broken_by_revision` あり）。
壊れうるのは「選定結果」であって「生成物」ではない。

### 原因

`cut_visual_qa` の差し戻しルート（`s` / `d` / `g` / `n`）で
`target_cut_id` が設定されていない。`support_video_creator` には
`target_cut_id` による絞り込みが実装済みなので、渡すだけで効く。

```python
# agewec_v2/pipeline_runtime.py（support_video_creator）
if target_cut_id is not None and cut_id != target_cut_id:
    continue        # ← 仕組みはある。値が来ていない
```

### 修正案

`commit_cut_qa` が差し戻しルートを組む際に
`review_context[target_phase]["target_cut_id"] = current` を設定する。
Asset Curator / Director 側も同じ絞り込みに対応させる。

---

## P1-3. config の定型文が全フェーズへ再注入され、承認を迂回する

### 症状

Executive Producer に「昼にこだわらなくていい」と修正指示して承認しても、
Creative Director と Writer Storyboard で「昼の喧騒」「昼の活気」が復活する。
同じ指摘を4回繰り返す羽目になった（run-16a5d19e18）。

### 原因

`config_llm.yaml` の固定文言が、承認ゲートを通らずに下流へ直接流れている。

```yaml
project:
  theme: 北九州の昼の活動から荘厳な夜景へ移り変わる魅力を、国内外の旅行者へ伝える観光プロモーション動画
```

```python
# agewec_v2/nodes_llm.py  creative_director / writer_storyboard
upstream={"project": state.get("project", {}),   # ← 未承認の生config
          "project_brief": brief, ...}           # ← 人間が承認した成果物
```

```
config.theme ──────────────┐ (承認ゲートを通らない)
                           ├──→ 下流フェーズ
executive_producer → brief ┘ (人間が承認したもの)
```

フィードバックで書き換わるのは `brief` だけで、`theme` 本体は不変。

### 修正案（推奨: 2番目）

1. config の theme を書き換える … 即効性はあるが、新しい定型文が同じように
   再注入されるだけで根治しない
2. **下流の upstream から `"project"` を外し、`project_brief` だけ渡す**
   下流が `project` から読んでいるのは `target_duration_seconds` と
   `target_award` の2つだけで、**両方とも `ProjectBrief` に含まれる**。
   さらに `executive_producer` の transform が両者の一致を検証して例外を
   投げるため、値のズレも起きない
3. CLI の Executive Producer フィードバックで theme も更新可能にする
   （`project_updates` は既に `theme` を許可済み。入力欄を足すだけ）

---

## P2-1. 素材選定がファイルサイズ・解像度を考慮しない

Asset Curator のスコアリングに素材のバイト数・画素数が入っていない。
P0-1 の地雷（asset-001 13.2MB / asset-010 12.6MB）を自力で回避できない。

**修正案**: 候補生成時に `file_size_bytes` と画素数でペナルティを付ける。
または P0-1 の送信前リサイズを入れて、そもそも問題にしない。

---

## P2-2. text-to-video に非対応

現状 image-to-video 専用。制約は4箇所。

```python
runway.py:150            f"{self.base_url}/v1/image_to_video"   # エンドポイント固定
backends/base.py:33      image_path: str                        # 必須フィールド
pipeline_runtime.py:221  画像が無ければ blocking                 # 存在チェック
config_llm.yaml          assets.require_primary_for_every_cut: true
```

gen4.5 は t2v 対応（1280:720 / 720:1280）なのでAPI側の障壁はない。

**ただし選択肢は3つあり、純粋なt2vが最善とは限らない:**

| | 内容 | provenance | 費用 |
| --- | --- | --- | --- |
| A. 純粋なt2v | 画像なし・文章のみ | 実在の北九州から離れる | $0.12/秒 |
| B. 参照画像つきt2v | 公式写真を参照しつつ新構図 | 素材の出典は残る | $0.36/秒（seedance2） |
| C. 現状のi2v | 写真を動かす | 最強 | $0.12/秒 |

「小倉城を歩くカップル」のような、人物を含む情緒的なカットが欲しい場合は
**A ではなく B** が該当する。Runway公式の入力仕様表でも
Seedance 2 Text-to-Video は Reference image に対応している。

---

## P2-3. VLM による意味的QAが未接続

`semantic_visual_qa: not_evaluated` のまま。技術QA（尺・解像度・破損）は
動いているが、「北九州に見えるか」「破綻していないか」は人間しか判断できない。

---

## 修正済み（記録用）

2026-08-02〜03 に対応し、テストで保護済みのもの。

| 内容 | 検証 |
| --- | --- |
| LTXのフレーム制約・draft解像度がRunwayへ漏れていた（5.042秒→6秒切り上げ、$0.60→$0.72） | `test_backend_request_contracts.py` |
| QAがLTX基準で判定し、必ず失敗していた | 同上 |
| 同一条件での再課金リトライを課金前に停止 | 同上（変異テストで失敗を確認） |
| `y`（問題を承知で承認）がAI判定に無視されていた | `test_human_cut_review.py`（変異テストで失敗を確認） |
| `commit_cut_qa` が旧 `WorkflowState` を受けており state が欠落しうる | 同上 |
| 人間上書き時の `issue_class` が空 → `human_override` を記録 | 同上 |
| QAの自動提案が「あなたの修正指示」と誤表示 | `feedback_origin` で切替 |
| LLM出力が英語混じり → 日本語化（生成プロンプトのみ英語、機械語彙は不変） | `test_llm_integration.py::LanguagePolicyTest` |

---

## 推奨対応順（8/7まで）

1. **P0-2**（エラーを残す）… P0-1の原因特定がこれ無しには進まない
2. **P0-3**（provenanceの1行修正）… 完走を阻む最後の壁
3. **P0-4**（入力バリデーション + SqliteSaver）… 事故で全部消えるのを止める
4. **P0-1**（アップロードtimeout / 送信前リサイズ）
5. **P1-3**（theme再注入）… 承認作業のストレスが大きく減る
6. P1-1 / P1-2 … 余力があれば
7. P2系 … 締切後
