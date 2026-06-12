#!/usr/bin/env python3
"""
verify_proposals.py — done_groq_proposal（LLM未検証）の提案タイトルを OpenAlex/CrossRef で
照合し、実在論文と一致するか検証する。PDF は変更しない（read-only）。LLM 不使用＝無料枠無関係。

出力: <papers_dir>/_proposals_verified.csv（file, proposed_title/author, matched,
      api_title/author/doi/journal, similarity）と集計（matched / unmatched / no_title / error）。
      実行時 state は対象 papers ディレクトリ側に置く（skill install dir には書かない）。
matched=実在論文と高類似 → 実 DOI/著者が取れた（done 相当に格上げ候補）。
unmatched=提案タイトルに対応する論文が見つからない（要手動確認/非論文の可能性）。
error=API 一時障害（429/5xx/接続断）で照合できなかった行。unmatched とは区別し、再実行で再照合する。

使い方: verify_proposals.py <papers_dir> [--keys PATH]   （または環境変数 PAPERS_DIR）
api_keys.json の既定位置はリポジトリ直下（scripts/ の親）。
"""
import argparse, csv, json, os, sys, tempfile, time
from pathlib import Path

TOOL = Path(__file__).resolve().parent
ROOT = TOOL.parent  # リポジトリ直下（api_keys.json の既定位置）
sys.path.insert(0, str(TOOL))
import fill_pdf_metadata as F

# ---- 引数: 対象ディレクトリは明示必須 ----
# （旧実装は空文字 → Path('.') が常に is_dir() となりガードが死んでいた）
_ap = argparse.ArgumentParser(description="LLM 提案タイトルを OpenAlex/CrossRef/CiNii で照合検証する（read-only）")
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
OUT = PAPERS / "_proposals_verified.csv"  # 実行時 state は対象ディレクトリ側

def write_csv_atomic(rows):
    """CSV は同一ディレクトリの一時ファイル経由で os.replace（原子的書込）。"""
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), prefix=OUT.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "proposed_title", "proposed_author", "result",
                        "api_title", "api_author", "api_doi", "api_journal", "similarity"])
            w.writerows(rows)
        os.replace(tmp, OUT)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def main():
    F.load_api_keys(KEYS)  # OpenAlex polite pool メール・CiNii appid 等（--keys で指定可）
    if not STATUS.is_file():
        sys.exit(f"[設定エラー] status ファイルが見つかりません: {STATUS}\n"
                 "  先に fill_pdf_metadata.py を実行してください。")
    s = json.load(open(STATUS, encoding="utf-8"))
    props = [(k, v.get("bib", {})) for k, v in s.items() if v.get("status") == "done_groq_proposal"]
    print(f"検証対象 done_groq_proposal: {len(props)} 件")

    rows = []
    matched = unmatched = no_title = errors = 0
    for i, (f, b) in enumerate(props, 1):
        ptitle = (b.get("title") or "").strip()
        if not ptitle:
            no_title += 1
            rows.append([f, "", b.get("author", ""), "no_title", "", "", "", "", ""])
            continue
        try:
            api = F.fetch_by_title(ptitle)
            time.sleep(0.5)
            if not api:
                api = F.fetch_by_title_crossref(ptitle)
                time.sleep(0.5)
            if not api:
                api = F.fetch_by_title_cinii(ptitle)  # 和文紀要など
                time.sleep(1.0)
        except F.ApiTransientError as e:
            # 一時障害は unmatched と混同せず error 行として記録し、run 全体は失わない
            errors += 1
            rows.append([f, ptitle, b.get("author", ""), "error", "", "", "", "", ""])
            print(f"  [一時障害] {f}: {e} → result=error（再実行で再照合）", file=sys.stderr)
            continue
        if api:
            sim = F._title_similarity(ptitle, api.get("title", ""))
            matched += 1
            rows.append([f, ptitle, b.get("author", ""), "matched",
                         api.get("title", ""), api.get("author", ""),
                         api.get("doi", ""), api.get("journal", ""), f"{sim:.2f}"])
        else:
            unmatched += 1
            rows.append([f, ptitle, b.get("author", ""), "unmatched", "", "", "", "", ""])
        if i % 50 == 0:
            print(f"  ...{i}/{len(props)}  (matched={matched} unmatched={unmatched})", file=sys.stderr)

    write_csv_atomic(rows)

    print(f"\n====== 検証完了 ======")
    print(f"  matched(実在論文と一致・実DOI取得): {matched}")
    print(f"  unmatched(対応論文なし・要手動)    : {unmatched}")
    print(f"  no_title(提案が空)                 : {no_title}")
    print(f"  error(API一時障害・再実行で再照合) : {errors}")
    print(f"  出力: {OUT}")

if __name__ == "__main__":
    main()
