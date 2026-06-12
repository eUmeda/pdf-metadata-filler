---
name: pdf-metadata-filler
description: 論文PDFの Title/Author/DOI を自動補完する。「PDFのメタデータを埋めたい」「論文PDFにメタデータがない」「PDFのメタデータ補完」「PDF走査して補完」「/pdf-meta」「/pdf-metadata」で使用。ファイル名DOI→本文DOI→OpenAlex→CrossRef→CiNii(和文紀要)→OCR→LLM(Gemini/Groq)の多段で書誌を取得し pypdf/exiftool で書込。
---

# PDF Metadata Filler

PDF ディレクトリの Title/Author/DOI 欠損を多段フォールバックで補完する。
エンジンは OpenAlex/CrossRef/CiNii + OCR + LLM(Gemini/Groq, REST 直叩き)。
（earlier private tooling を統合・置換した公開版, 2026-06）

## 取得チェーン（`fill_pdf_metadata.py`）
```
ファイル名DOI → 本文DOI → OpenAlex(DOI) → CrossRef(DOI)
            → OpenAlex(title) → CrossRef(title) → CiNii(title=和文紀要に強い)
            → (スキャンPDF) OCR(eng+jpn) → LLM テキスト提案(Groq→Gemini)
```
- 書込: 本体は pypdf（アトミック書込: `.pdf.tmp` → `os.replace`。元 PDF は失敗時も無傷）。
  helper の `upgrade_matched.py` / `fuzzy_fill.py` は exiftool 書込。
- **バックアップ契約（全書込スクリプト共通）**: 変更前に `<name>.pdf.bak` を**未存在時のみ**作成。
  既存 `.bak` は原本なので再実行でも決して上書きしない。バックアップ失敗時はそのファイルを書き込まない。
- DOI は `/Subject` に格納（dedup キー）。title 類似度ゲート(0.6) で誤一致抑止。
  LLM は本文に無ければ doi 空（捏造禁止）。

## スクリプト（`scripts/`）
| script | 役割 |
|---|---|
| `fill_pdf_metadata.py` | 本体。`<dir> [--dry-run] [--only-list FILE] [--redo] [--keys PATH]` |
| `verify_proposals.py` | LLM提案(done_groq_proposal)のタイトルを OpenAlex/CrossRef/CiNii 照合 → matched/unmatched/no_title/error（read-only。error=API一時障害、再実行で再照合） |
| `upgrade_matched.py` | matched を実 DOI/著者で格上げ(exiftool) → status `done` |
| `fuzzy_fill.py` | no-title を 強制OCR(eng+jpn)+ファイル名で LLM ファジー全埋め → `done_fuzzy`（LLM 必須・キー未設定は設定エラーで即終了） |

- helper 3 スクリプトは対象 dir を第1引数（または環境変数 `PAPERS_DIR`）で受け取る。未指定は明示エラー。
- `--keys PATH` は全スクリプト共通。既定の解決順:
  `--keys → リポジトリ直下（scripts/ の親）の api_keys.json →（fill_pdf_metadata.py のみ）走査対象 dir`。
- 書込対象パスは対象 dir 内に限定（containment チェック。`../` 等で外の PDF は書き換えない）。
- helper は `exiftool` / `tesseract` を起動時に確認し、無ければ LLM トークン消費前に終了する。

標準運用: ①`fill_pdf_metadata.py <dir> --dry-run` で件数確認 → ②本実行(in-place, `.bak`退避) → ③`verify`→`upgrade`→（必要なら）`fuzzy`。

## 無料枠ガード（オーバーフロー防止）
プロバイダ別**日次トークン予算** + RPM スロットル + プロンプト縮小 + 429/5xx の Retry-After 尊重バックオフ。
予算超過・走行中 429 は失敗でなく `deferred_quota`（翌日 resume で継続）。
書誌 API の一時障害(429/5xx/接続断)は `deferred_api` で保留し、**未検証の LLM 提案にフォールバックしない**。
モデル不存在(404)等の恒久エラーは当該プロバイダを無効化（誤って翌日に保留しない）。
LLM 優先 Groq→Gemini。使用量は対象 dir の `_llm_usage.json` に永続化。
`--only-list FILE` が空のときは「対象0件」（全走査にフォールバックしない安全策）。

## status 規約（信頼度）
`done`=API検証済（高信頼） / `done_ocr_fallback`=ヒューリスティックOCR（**未検証**） /
`done_groq_proposal`=LLM提案（**未検証**） / `done_fuzzy`=OCR+ファイル名のLLMファジー（**未検証**） /
`deferred_quota`=LLM無料枠待ち / `deferred_api`=書誌API一時障害（再実行で継続） / `failed`。

## 書込フィールド
`/Title`=論文タイトル, `/Author`=第一著者(複数は "First et al."), `/Subject`=`DOI: … | 誌名 | 年`, `/Keywords`=DOI。

## 鍵・依存
- `api_keys.json`（雛形 `api_keys.example.json`）: `openalex.email`・`openalex.api_key`・`cinii.appid`・
  `gemini.api_key/model`・`groq.api_key/model`。**実鍵は commit/同期しない**(gitignore)。
- **OpenAlex は 2026-02-13 以降 API キー必須**（無料）。未設定なら警告のうえテスト枠で試行、
  401/403/409 で OpenAlex 段をスキップ。**CiNii は appid 必須**（NII 開発者登録・無料）、未設定なら CiNii 段スキップ。
- メールが空/プレースホルダ（example.com / your-email 等）のとき mailto は**一切送信しない**（匿名 User-Agent）。
- Gemini 無料枠は送信テキストが Google の製品改善（人手レビュー含む）に使われうる。機密 PDF は有料枠か Groq を使う。
- 依存: `pypdf requests pypdfium2 pytesseract Pillow` + `tesseract(eng+jpn)` + `exiftool`(helper 書込用)。
  LLM SDK（groq / google-generativeai）は**不要**（requests による REST 直叩き）。
- 実行時 state（`pdf_metadata_status.json` / `_llm_usage.json` / `_proposals_verified.csv`）は
  すべて**対象 papers ディレクトリ側**に置く。壊れた state は `*.corrupt-<時刻>` に退避して保全。
- 自動再開(launchd/cron)・creds 管理・大規模運用は各自の環境に合わせて構成すること。

> **CiNii(和文紀要)** 対応を内蔵。OCR/LLM 経路で scan PDF にも対応。
> dedup / quarantine スクリプトは本 repo の範囲外（Roadmap 参照）。
