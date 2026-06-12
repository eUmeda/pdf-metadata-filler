#!/usr/bin/env python3
"""
upgrade_matched.py — _proposals_verified.csv の result=matched を、検証済み API メタデータ
(実 DOI/著者/誌名) で PDF に上書きし、status を done(検証済) に格上げする。
CSV は対象 papers ディレクトリ側 (<papers_dir>/_proposals_verified.csv) から読む
（verify_proposals.py が同じ場所に書く。別ライブラリの CSV を誤適用しないため）。
exiftool で書込。書込前に <name>.pdf.bak を未存在時のみ作成する（既存 .bak は原本なので
決して上書きしない）。旧 LLM 提案は status の prev_proposal に保持（追跡用）。

使い方: upgrade_matched.py <papers_dir> [--keys PATH]   （または環境変数 PAPERS_DIR）
"""
import argparse, csv, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # リポジトリ直下

# ---- 引数: 対象ディレクトリは明示必須 ----
# （旧実装は空文字 → Path('.') が常に is_dir() となりガードが死んでいた）
_ap = argparse.ArgumentParser(description="verify 済み matched 行を実メタデータで PDF に格上げ書込する")
_ap.add_argument("papers_dir", nargs="?", default=os.environ.get("PAPERS_DIR", ""),
                 help="対象 papers ディレクトリ（または環境変数 PAPERS_DIR）")
_ap.add_argument("--keys", default=str(ROOT / "api_keys.json"),
                 help="api_keys.json のパス（本スクリプトは API を呼ばないため未使用。CLI 統一のため受理）")
_args = _ap.parse_args()
if not _args.papers_dir:
    sys.exit("[設定エラー] 対象ディレクトリ未指定。PAPERS_DIR 環境変数を設定するか、"
             "第1引数に papers ディレクトリを渡してください。")
PAPERS = Path(_args.papers_dir).expanduser().resolve()
if not PAPERS.is_dir():
    sys.exit(f"[設定エラー] papers ディレクトリが存在しません: {PAPERS}")
CSV = PAPERS / "_proposals_verified.csv"  # 実行時 state は対象ディレクトリ側（install dir を汚さない）
STATUS = PAPERS / "pdf_metadata_status.json"

# ---- 外部バイナリは起動時に確認（書込を始める前に落とす） ----
if shutil.which("exiftool") is None:
    sys.exit("[設定エラー] exiftool が見つかりません。インストールしてください"
             "（brew install exiftool / apt install libimage-exiftool-perl）。")

def resolve_in_papers(pdf):
    """CSV の file 列由来パスを PAPERS 内に限定する（../ 等で外の PDF を書き換えない）。"""
    target = (PAPERS / pdf).resolve()
    try:
        target.relative_to(PAPERS)
    except ValueError:
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

def save_status_atomic(status):
    """status JSON は同一ディレクトリの一時ファイル経由で os.replace（原子的書込）。"""
    fd, tmp = tempfile.mkstemp(dir=str(STATUS.parent), prefix=STATUS.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(status, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def write_exif(target, title, author, doi, journal):
    if not ensure_backup(target):  # 契約: PDF 変更前に .pdf.bak（未存在時のみ）
        return False, "backup failed"
    args = ["exiftool", "-charset", "filename=utf8", "-overwrite_original"]
    if title:  args.append(f"-Title={title}")
    if author: args.append(f"-Author={author}")
    subj = []
    if doi:     subj.append("DOI: " + doi)
    if journal: subj.append(journal)
    if subj:    args.append(f"-Subject={' | '.join(subj)}")
    if doi:     args.append(f"-Keywords={doi}")
    args.append(str(target))
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:  # 起動時に確認済みだが念のため
        return False, "exiftool not found"
    return r.returncode == 0, (r.stdout + r.stderr).strip()

def main():
    if not CSV.is_file():
        sys.exit(f"[設定エラー] {CSV} が見つかりません。\n"
                 "  先に verify_proposals.py <papers_dir> を実行してください。")
    if not STATUS.is_file():
        sys.exit(f"[設定エラー] status ファイルが見つかりません: {STATUS}")
    rows = [r for r in csv.DictReader(open(CSV, encoding="utf-8")) if r["result"] == "matched"]
    status = json.load(open(STATUS, encoding="utf-8"))
    ok = fail = 0
    for r in rows:
        f = r["file"]
        target = resolve_in_papers(f)
        if target is None:
            fail += 1
            print(f"  [失敗] {f}: papers ディレクトリ外を指すため除外", file=sys.stderr)
            continue
        if not target.is_file():
            fail += 1
            print(f"  [失敗] {f}: PDF が見つからない", file=sys.stderr)
            continue
        success, msg = write_exif(target, r["api_title"], r["api_author"], r["api_doi"], r["api_journal"])
        if success:
            prev = status.get(f, {}).get("bib")
            status[f] = {"status": "done",
                         "bib": {"title": r["api_title"], "author": r["api_author"],
                                 "doi": r["api_doi"], "journal": r["api_journal"],
                                 "_source": "verified_title_match", "_similarity": r["similarity"]},
                         "note": "LLM提案をAPIタイトル照合で検証し実DOI/著者に格上げ",
                         "prev_proposal": prev}
            ok += 1
        else:
            fail += 1
            print(f"  [失敗] {f}: {msg[:80]}", file=sys.stderr)
    save_status_atomic(status)
    print(f"格上げ完了: 成功 {ok} / 失敗 {fail} （計 {len(rows)} matched）")

if __name__ == "__main__":
    main()
