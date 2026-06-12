#!/usr/bin/env python3
"""
fuzzy_fill.py — title が空のまま(no-title)残った PDF を、強制OCR(eng+jpn)＋ファイル名を手がかりに
LLM が「最善推測(ファジー)」で必ず埋める。user 方針: とにかく全部埋める(DOIだけは捏造しない)。
無料枠ガード(F._provider_ok/_throttle/_record_usage)を尊重。LLM 呼出は SDK ではなく素の REST
(requests + 共通バックオフ)。書込は exiftool。書込前に <name>.pdf.bak を未存在時のみ作成する
（既存 .bak は原本なので決して上書きしない）。status → done_fuzzy（未検証フラグ）。
無料枠切れは元の done_groq_proposal レコードを壊さず "fuzzy": "deferred_quota" を追記して保留。

使い方: fuzzy_fill.py <papers_dir> [--keys PATH]   （または環境変数 PAPERS_DIR）
api_keys.json の既定位置はリポジトリ直下（scripts/ の親）。
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse
from pathlib import Path

import requests

TOOL = Path(__file__).resolve().parent
ROOT = TOOL.parent  # リポジトリ直下（api_keys.json の既定位置）
sys.path.insert(0, str(TOOL))
import fill_pdf_metadata as F
import pypdfium2 as pdfium
import pytesseract

# ---- 引数: 対象ディレクトリは明示必須 ----
# （旧実装は空文字 → Path('.') が常に is_dir() となりガードが死んでいた。
#   未指定のまま CWD に破壊的書込する事故を防ぐため、空なら即終了する）
_ap = argparse.ArgumentParser(description="no-title PDF を OCR+LLM でファジー補完する")
_ap.add_argument("papers_dir", nargs="?", default=os.environ.get("PAPERS_DIR", ""),
                 help="対象 papers ディレクトリ（または環境変数 PAPERS_DIR）")
_ap.add_argument("--keys", default=str(ROOT / "api_keys.json"),
                 help="api_keys.json のパス（既定: リポジトリ直下の api_keys.json）")
_args = _ap.parse_args()
if not _args.papers_dir:
    sys.exit("[設定エラー] 対象ディレクトリ未指定。PAPERS_DIR 環境変数を設定するか、"
             "第1引数に papers ディレクトリを渡してください。")
PAPERS = Path(_args.papers_dir).expanduser().resolve()
if not PAPERS.is_dir():
    sys.exit(f"[設定エラー] papers ディレクトリが存在しません: {PAPERS}")
STATUS = PAPERS / "pdf_metadata_status.json"
KEYS = Path(_args.keys).expanduser()

# ---- 外部バイナリは起動時に確認（LLM トークンを 1 つも消費する前に落とす） ----
for _bin, _hint in (("exiftool", "brew install exiftool / apt install libimage-exiftool-perl"),
                    ("tesseract", "brew install tesseract tesseract-lang / apt install tesseract-ocr")):
    if shutil.which(_bin) is None:
        sys.exit(f"[設定エラー] {_bin} が見つかりません。インストールしてください（{_hint}）。")

# ---- LLM 設定（REST 直叩き。SDK 不使用） ----
# キー未設定は「quota 切れ」ではなく設定エラー。誤って deferred_quota を量産しないよう即終了する。
try:
    with open(KEYS, encoding="utf-8") as _fh:
        _cfg = json.load(_fh)
except FileNotFoundError:
    sys.exit(f"[設定エラー] APIキーファイルが見つかりません: {KEYS}\n"
             "  fuzzy_fill は LLM 必須です（quota 切れではありません）。リポジトリ直下で\n"
             "  cp api_keys.example.json api_keys.json してキーを設定してください。")
except (json.JSONDecodeError, OSError) as _e:
    sys.exit(f"[設定エラー] APIキーファイルの読み込み失敗: {KEYS}: {_e}")

GROQ_KEY     = (_cfg.get("groq", {}).get("api_key") or "").strip()
GROQ_MODEL   = (_cfg.get("groq", {}).get("model") or "").strip() or "llama-3.3-70b-versatile"
GEMINI_KEY   = (_cfg.get("gemini", {}).get("api_key") or "").strip()
GEMINI_MODEL = (_cfg.get("gemini", {}).get("model") or "").strip() or "gemini-2.5-flash"
PROVS = [p for p, k in (("groq", GROQ_KEY), ("gemini", GEMINI_KEY)) if k]
if not PROVS:
    sys.exit(f"[設定エラー] LLM APIキー未設定（quota 切れではありません）。\n"
             f"  {KEYS} に groq.api_key または gemini.api_key を設定してください。")

FUZZY_PROMPT = """以下は学術PDFのOCRテキスト(冒頭・文字化けや空の可能性あり)とファイル名です。
書誌情報を「最善推測」で必ず埋めてください。テキストが読めない/空でも、ファイル名を手がかりに
ファジーに推測すること。title は絶対に空にしない(最低限ファイル名から推測した題を入れる)。
year/journal/author も空でなく最善の推測を入れる。ただし doi だけは本文に明記が無ければ空(捏造禁止)。
JSONのみ返す: {{"title":"...","author":"...","year":"...","journal":"...","doi":"..."}}
--- ファイル名 ---
{fname}
--- OCRテキスト ---
{ocr}
"""

def forced_ocr(pdf, pages=2):
    try:
        out = ""
        doc = pdfium.PdfDocument(str(pdf))   # pypdfium2 4.x: with 非対応 → try/finally
        try:
            for i in range(min(pages, len(doc))):
                # 共通 helper でグレースケール・ラスタライズ（pypdfium2）
                img = F.render_page_to_pil(doc, i, 300)
                out += pytesseract.image_to_string(img, lang="eng+jpn")
                if len(out) > 800:
                    break
        finally:
            doc.close()
        return out
    except Exception:
        return ""

def _post_with_backoff(url, headers, payload, timeout=90):
    """共通 HTTP バックオフ: 429/5xx は Retry-After を尊重しつつ指数待機(2,4,8s)・最大3リトライ。
    リトライ枯渇・接続障害は TRANSIENT として None を返す（恒久 failed にしない）。"""
    delays = (2, 4, 8)
    for attempt in range(len(delays) + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as e:
            print(f"  [警告] HTTP送信失敗: {e}", file=sys.stderr)
            if attempt < len(delays):
                time.sleep(delays[attempt]); continue
            return None
        if (r.status_code == 429 or r.status_code >= 500) and attempt < len(delays):
            ra = r.headers.get("Retry-After", "")
            try:
                wait = min(max(float(ra), delays[attempt]), 60.0)
            except ValueError:
                wait = delays[attempt]
            time.sleep(wait); continue
        return r
    return None

def _llm_call(prov, prompt):
    """1 プロバイダを REST で呼ぶ。戻り: 応答テキスト / "RATELIMIT"(429系が継続) / None(他失敗)。"""
    if prov == "groq":
        r = _post_with_backoff(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            payload={"model": GROQ_MODEL,
                     "messages": [{"role": "system", "content": "Return only JSON."},
                                  {"role": "user", "content": prompt}],
                     "temperature": 0, "max_tokens": 256})
    else:  # gemini
        r = _post_with_backoff(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            payload={"contents": [{"parts": [{"text": prompt}]}],
                     # maxOutputTokens は設定しない: gemini-2.5 系は thinking トークンも
                     # この上限に算入されるため、小さい上限だと本文が空になり parse 失敗する
                     "generationConfig": {"temperature": 0}})
    if r is None or r.status_code == 429 or r.status_code >= 500:
        return "RATELIMIT"  # transient: このプロバイダは本実行ではもう使わない
    if r.status_code != 200:
        print(f"  [警告] {prov} HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
        return None
    try:
        d = r.json()
        if prov == "groq":
            return d["choices"][0]["message"]["content"].strip()
        return d["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (ValueError, KeyError, IndexError, TypeError) as e:
        print(f"  [警告] {prov} 応答パース失敗: {e}", file=sys.stderr)
        return None

def fuzzy_llm(fname, ocr):
    prompt = FUZZY_PROMPT.format(fname=urllib.parse.unquote(fname), ocr=(ocr or "")[:1200])
    est = F._est_tokens(prompt)
    # キー未設定は起動時に弾いてあるので、ここに来る QUOTA は本当の日次予算切れ/429済のみ
    if not any(F._provider_ok(p, est) for p in PROVS):
        return "QUOTA"
    for prov in PROVS:
        if not F._provider_ok(prov, est):
            continue
        F._throttle(prov)
        raw = _llm_call(prov, prompt)
        F._record_usage(prov, est)
        if raw == "RATELIMIT":
            F._llm_rate_limited.add(prov)
            continue
        if not raw:
            continue
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.M)
        raw = re.sub(r"\s*```$", "", raw, flags=re.M).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m: continue
        try: bib = json.loads(m.group(0))
        except json.JSONDecodeError: continue
        if not isinstance(bib, dict): continue
        bib = {k: str(v).strip() if v else "" for k, v in bib.items()}
        bib["_source"] = f"{prov}_fuzzy"
        return bib
    return None

def resolve_in_papers(pdf):
    """status のキー由来パスを PAPERS 内に限定する（../ 等で外の PDF を書き換えない）。"""
    target = (PAPERS / pdf).resolve()
    try:
        target.relative_to(PAPERS)
    except ValueError:
        print(f"  [警告] papers ディレクトリ外を指すため除外: {pdf}", file=sys.stderr)
        return None
    return target

def ensure_backup(target):
    """PDF 変更前に <name>.pdf.bak を作成する。既存 .bak は原本なので決して上書きしない。
    コピーは同一ディレクトリの一時ファイル → os.replace で原子的に置く。"""
    bak = target.with_name(target.name + ".bak")
    if bak.exists():
        return True
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=bak.name + ".", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copy2(target, tmp)
        os.replace(tmp, bak)
        return True
    except OSError as e:
        try: os.unlink(tmp)
        except OSError: pass
        print(f"  [警告] バックアップ作成失敗のため書込中止: {target.name}: {e}", file=sys.stderr)
        return False

def save_status_atomic(s):
    """status JSON は同一ディレクトリの一時ファイル経由で os.replace（原子的書込）。"""
    fd, tmp = tempfile.mkstemp(dir=str(STATUS.parent), prefix=STATUS.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def write_exif(target, b):
    if not ensure_backup(target):  # 契約: PDF 変更前に .pdf.bak（未存在時のみ）
        return False
    args = ["exiftool", "-charset", "filename=utf8", "-overwrite_original"]
    if b.get("title"):  args.append(f"-Title={b['title']}")
    if b.get("author"): args.append(f"-Author={b['author']}")
    subj = []
    if b.get("doi"):     subj.append("DOI: " + b["doi"])
    if b.get("journal"): subj.append(b["journal"])
    if b.get("year"):    subj.append(b["year"])
    if subj: args.append(f"-Subject={' | '.join(subj)}")
    if b.get("doi"): args.append(f"-Keywords={b['doi']}")
    args.append(str(target))
    try:
        return subprocess.run(args, capture_output=True, text=True).returncode == 0
    except FileNotFoundError:  # 起動時に確認済みだが念のため
        sys.exit("[設定エラー] exiftool が見つかりません。")

def main():
    # 実行時 state（日次 LLM 使用量）も対象ディレクトリ側に置く（skill install dir を汚さない）
    F.load_llm_usage(PAPERS / "_llm_usage.json")
    if not STATUS.is_file():
        sys.exit(f"[設定エラー] status ファイルが見つかりません: {STATUS}\n"
                 "  先に fill_pdf_metadata.py を実行してください。")
    s = json.load(open(STATUS, encoding="utf-8"))
    targets = [k for k, v in s.items()
               if v.get("status") == "done_groq_proposal" and not v.get("bib", {}).get("title")]
    print(f"fuzzy-fill 対象(no-title 空): {len(targets)}  [LLM: {'/'.join(PROVS)}]")
    ok = skip = defer = 0
    try:
        for i, f in enumerate(targets, 1):
            target = resolve_in_papers(f)
            if target is None or not target.is_file():
                skip += 1
                continue
            bib = fuzzy_llm(f, forced_ocr(target))
            if bib == "QUOTA":
                # 元の done_groq_proposal レコード(bib含む)は壊さず、保留マークだけ追記する
                rec = dict(s.get(f) or {})
                rec["fuzzy"] = "deferred_quota"
                rec["fuzzy_note"] = "fuzzy-fill: 無料枠日次上限。翌日 fuzzy_fill.py 再実行で継続。"
                s[f] = rec
                defer += 1
                continue
            if not bib or not bib.get("title"):
                skip += 1; continue
            if write_exif(target, bib):
                s[f] = {"status": "done_fuzzy", "bib": bib, "note": "OCR+ファイル名からLLMファジー推測(未検証)"}
                ok += 1
            else:
                skip += 1
            if i % 25 == 0:
                save_status_atomic(s)
                print(f"  ...{i}/{len(targets)} ok={ok} defer={defer}", file=sys.stderr)
    finally:
        save_status_atomic(s)  # 中断・例外でも進捗を失わない
    print(f"fuzzy-fill 完了: 埋めた {ok} / skip {skip} / 保留 {defer}")
    if F._llm_usage:
        print("LLM日次使用:", {p: F._llm_usage.get(p, 0) for p in F.LLM_LIMITS})

if __name__ == "__main__":
    main()
