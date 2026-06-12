# pdf-metadata-filler

論文 PDF の **Title / Author / DOI** を多段フォールバックで自動補完する Claude Code Skill + Python ツール。

`DOI(ファイル名/本文) → OpenAlex → CrossRef → CiNii(和文紀要に強い) → OCR → LLM(Gemini/Groq)` の順に
書誌情報を解決し、`pypdf`（本体）/ `exiftool`（helper スクリプト）で PDF に書き戻します。
LLM・CiNii のキーが無くても OpenAlex/CrossRef + ヒューリスティック OCR で動作し、
LLM は「どうしても取れないとき」のフォールバックです。

## ステータス / 既知の未検証事項（TODO）
- [ ] **OCR テキストの end-to-end 一致は未検証**: PDF ラスタライズを PyMuPDF(AGPL) → `pypdfium2`(Apache/BSD)
  へ移植済み。ラスタライズ自体（300 DPI グレースケール化）は実 PDF で検証済みだが、**スキャン PDF を
  `tesseract` 込みで通した OCR テキストの一致は未確認**（PDFium と MuPDF は画素レベルで非同一）。
  本番運用前に、実スキャン PDF を 1 本 end-to-end で通して OCR 結果の同等性を確認すること。

## これは何
- 散らかった論文 PDF ライブラリ（メタデータ欠損）をまとめて補完する。
- **CiNii 対応**で日本語紀要にも強い（数少ない特徴）。
- title 類似度ゲート(0.6)で誤一致を抑止。本文に無い DOI は捏造しない（空のまま）。

## 動作要件
- Python 3.10+
- `pip install pypdf requests pypdfium2 pytesseract Pillow`
  （LLM は requests による REST 直叩きに統一済み。`groq` / `google-generativeai` SDK は**不要** —
  google-generativeai は 2025-11-30 にサポート終了したため依存から外しました）
- `tesseract`（eng+jpn 言語データ。OCR 段で使用）
- `exiftool`（helper スクリプト `fuzzy_fill.py` / `upgrade_matched.py` の書込で使用。
  本体 `fill_pdf_metadata.py` は pypdf のみで書き込む）

helper スクリプトは外部バイナリを**起動時に確認**し、無ければ LLM トークンを消費する前に
インストール手順付きで終了します。

## インストール（Claude Code Skill として）
```sh
git clone https://github.com/eUmeda/pdf-metadata-filler.git
mkdir -p ~/.claude/skills
ln -s "$PWD/pdf-metadata-filler" ~/.claude/skills/pdf-metadata-filler
```
Claude Code で `/pdf-metadata-filler`（または「PDF のメタデータを埋めて」）で起動。スクリプト単体でも実行可。

## 使い方
```sh
cp api_keys.example.json api_keys.json    # 使う鍵だけ記入（リポジトリ直下に置く）
python scripts/fill_pdf_metadata.py <dir> --dry-run        # 件数確認（書込なし）
python scripts/fill_pdf_metadata.py <dir>                  # 本実行（原本は <name>.pdf.bak 退避）
python scripts/verify_proposals.py <dir>                   # LLM 提案を OpenAlex/CrossRef/CiNii 照合（read-only）
python scripts/upgrade_matched.py <dir>                    # 照合済を実 DOI/著者に格上げ
python scripts/fuzzy_fill.py <dir>                         # 残りを OCR+ファイル名で LLM ファジー補完（LLM 必須）
```
- 対象ディレクトリは第1引数。helper 3 スクリプト（verify/upgrade/fuzzy）は未指定なら明示エラーで終了します（環境変数 `PAPERS_DIR` でも可）。`fill_pdf_metadata.py` は未指定だと**カレントディレクトリを走査**するので、`--dry-run` での事前確認を推奨します。
- **`--keys PATH`**: 全 4 スクリプト共通。api_keys.json の解決順は
  `--keys 指定 → リポジトリ直下（scripts/ の1つ上）→（fill_pdf_metadata.py のみ）走査対象ディレクトリ`。
- `fill_pdf_metadata.py --redo`: `done*` 済みファイルも再処理する（既定では再処理しない）。

書込フィールド: `/Title`・`/Author`（第一著者, 複数は "First et al."）・`/Subject`（DOI &#124; 誌名 &#124; 年）・`/Keywords`（DOI）。

信頼度 status: `done`（API 検証済）/ `done_ocr_fallback`・`done_groq_proposal`・`done_fuzzy`（未検証）/
`deferred_quota`（LLM 無料枠待ち・翌日再実行で継続）/ `deferred_api`（書誌 API の一時障害・再実行で継続）/ `failed`。

### バックアップと実行時ファイル
- **書込を行う全スクリプト**（fill_pdf_metadata / upgrade_matched / fuzzy_fill）は、変更前に
  `<name>.pdf.bak` を**未存在時のみ**作成します。既存の `.bak` は「原本」とみなし、
  再実行でも**決して上書きしません**。バックアップ作成に失敗したファイルは書き込みません。
- 本体の PDF 書込・status 書込はアトミック（同一ディレクトリの一時ファイル → `os.replace`）。
  途中で失敗しても元 PDF は無傷のまま残ります。
- 実行時 state はすべて**対象 papers ディレクトリ側**に置かれます（skill のインストール先は汚しません）:
  `pdf_metadata_status.json`（処理結果）・`_llm_usage.json`（LLM 日次使用量）・
  `_proposals_verified.csv`（verify→upgrade の受け渡し）。壊れた state ファイルは黙って捨てず
  `*.corrupt-<時刻>` に退避されます。

## API キー
| provider | 用途 | 必須? | 取得 |
|---|---|---|---|
| **OpenAlex** | メタ解決 第1候補 | **実質必須**（2026-02-13 以降、全リクエストでキー必須・無料） | https://openalex.org/settings/api |
| CrossRef | メタ解決 第2候補 | 不要（メール任意） | — |
| CiNii | 和文紀要 | appid 必須（無料・NII 開発者登録）。未設定なら CiNii 段をスキップ | https://support.nii.ac.jp/ja/cinii/api/developer |
| Gemini | LLM 第1候補 | 任意 | https://aistudio.google.com/app/apikey |
| Groq | LLM 第2候補 | 任意 | https://console.groq.com/keys |

- 鍵は `api_keys.json`（`.gitignore` 済）に記入。空の provider は自動スキップ。
- OpenAlex キー未設定時は 1 回だけ警告してテスト枠で試行し、401/403/409 が返ったら
  その実行中は OpenAlex 段をスキップして CrossRef 以降で継続します。
- 既定モデル: `gemini-2.5-flash` / `llama-3.3-70b-versatile`（廃止された gemini-1.5-flash /
  mixtral-8x7b-32768 は使いません）。無料枠の数値は変動するため各プロバイダの現行ドキュメントで確認してください。
- **プレースホルダメールは送信されません**: `openalex.email` が空、または `example.com` / `your-email`
  系のプレースホルダの場合、User-Agent に mailto を付けず匿名アクセスします（偽の連絡先を申告しない）。
- 無料枠ガード（provider 別日次トークン予算 + RPM スロットル + 429/503 の Retry-After 尊重バックオフ）で、
  超過分は失敗でなく `deferred_quota`（翌日 resume で継続）。書誌 API の一時障害は `deferred_api` で保留し、
  **未検証の LLM 提案へフォールバックしません**。

### データの取り扱い / Privacy
本ツールは LLM 段で PDF 冒頭の本文/OCR テキスト（最大約 1200 字）とファイル名を LLM に送信します。
**Gemini 無料枠では、送信したテキストを Google が製品・サービス改善のために利用することがあり、
人手によるレビューも含まれます**（Gemini API 利用規約の Unpaid Services 条項）。
未公開原稿・査読中の機密 PDF・エンバーゴ付きプレプリントを含むディレクトリを処理する場合は、
**Gemini 有料枠を使うか、Groq のみを設定する**（Groq の Services Agreement は入出力を学習に
使わないとしています）か、LLM キーを設定せず API+OCR のみで運用してください。

## Roadmap
- **ローカル LLM 対応**（例: Ollama）— クラウド API 不要のオフライン補完段を追加予定。
- dedup / quarantine スクリプトの同梱。

## 利用上の注意（Terms / Disclaimer）
本ツールは現状有姿（**AS IS**）で提供され、明示黙示を問わず**いかなる保証もありません**。
**利用は完全に自己責任**で行ってください。本ツールの利用により生じたいかなる損害（PDF の破損・
誤ったメタデータ書込み等を含む）についても、作者は一切の責任を負いません。
**重要な PDF は事前にバックアップ**してください（本ツールも書込前に `.pdf.bak` を退避しますが保証はしません）。

## 引用（Citation）
研究や作業に役立った場合、引用やリンクでの言及を歓迎します（必須ではありません）。
> Eisaku Umeda (2026). *pdf-metadata-filler*. https://github.com/eUmeda/pdf-metadata-filler

## Third-party licenses / 依存ライセンス
本ツールのコードは MIT ですが、以下のライブラリに依存します（利用者が各自インストール。本 repo には**同梱しません**）:

| ライブラリ | ライセンス | 備考 |
|---|---|---|
| pypdf | BSD-3-Clause | |
| requests | Apache-2.0 | 書誌 API・LLM REST 呼び出しすべてに使用 |
| pypdfium2 | Apache-2.0 / BSD-3-Clause | PDF ラスタライズ（OCR 用）。**コピーレフトなし** |
| pytesseract | Apache-2.0 | 外部 `tesseract` のラッパ |
| Pillow | HPND/MIT-CMU | OCR 画像ハンドリング |

各ライブラリはそれぞれのライセンス下にあり、いずれも permissive（コピーレフトなし）です。
本ツールは当初 PDF ラスタライズに PyMuPDF(AGPL) を使っていましたが、**copyleft を避けるため
`pypdfium2`(Apache/BSD) へ移行済み**です。LLM SDK（groq / google-generativeai）への依存も
撤廃し、requests のみで REST を直接呼び出します。

## License
MIT License — see [LICENSE](LICENSE).

---
🤖 Built and maintained with [Claude Code](https://claude.com/claude-code).
