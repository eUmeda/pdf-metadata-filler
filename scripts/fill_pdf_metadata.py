#!/usr/bin/env python3
"""
fill_pdf_metadata.py

Meaning: ディレクトリ内のPDFを走査し、Title/Author/DOIのいずれかが
         欠けているファイルに対してOpenAlex APIから書誌情報を取得して
         メタデータを書き込む。

Action:  1. ディレクトリを再帰走査してPDFを列挙する
         2. pdf_metadata_status.json を読み込む（なければ新規作成）
         3. 各PDFのメタデータを検査する（Title/Author/DOIの欠損チェック）
         4. 欠損PDFの本文冒頭テキストからDOIを正規表現で抽出する
         5. DOIが見つからなければタイトル文字列でOpenAlexを全文検索する
         6. 取得した書誌情報をpypdfでPDFに書き込む
         7. 処理結果をpdf_metadata_status.jsonに記録する
"""

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from pypdf import PdfReader, PdfWriter

# OCR関連（スキャンPDF対応）
# pypdfium2: PDFページを高解像度画像にラスタライズする（Apache-2.0/BSD。旧 PyMuPDF/fitz は AGPL のため置換）
# pytesseract: Tesseract OCRエンジンのPythonラッパー
try:
    import pypdfium2 as pdfium
    import pytesseract
    from PIL import Image  # availability guard: pypdfium2 の .to_pil() は Pillow を要する
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# LLM関連（書誌情報構造化抽出用）
# SDK（google-generativeai / groq）は使わず、requests による素の REST 呼び出しに統一する。
# 根拠: google-generativeai SDK は 2025-11-30 にサポート終了（リポジトリ archived）。
#       依存を requests のみに減らし、SDK の世代交代に巻き込まれないようにする。
# 実行時に api_keys.json から読み込んで設定される（load_api_keys() で初期化）
_LLM_PROVIDER   = None   # "gemini" | "groq" | None（primary プロバイダ）
_LLM_MODEL      = None   # primary プロバイダのモデル名

# Gemini / Groq の API キーとモデル（REST 呼び出し用）。
# Groq は primary が Gemini でも、最終フォールバック（本文/OCRテキストから
# Groq に書誌を提案させ、その提案をそのまま採用する）のために常時保持しておく。
_GEMINI_API_KEY = ""
_GEMINI_MODEL   = "gemini-2.5-flash"           # api_keys.json の gemini.model で上書き可
_GROQ_API_KEY   = ""
_GROQ_MODEL     = "llama-3.3-70b-versatile"    # api_keys.json の groq.model で上書き可

# LLM REST エンドポイント
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_CHAT_URL       = "https://api.groq.com/openai/v1/chat/completions"

# ------------------------------------------------------------------ #
# 定数
# ------------------------------------------------------------------ #
STATUS_FILE_NAME = "pdf_metadata_status.json"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CINII_OPENSEARCH_URL = "https://cir.nii.ac.jp/opensearch/articles"  # 和文紀要に強い

# 連絡先メール（CrossRef etiquette 用。api_keys.json の openalex.email から設定される）。
# 実在しうるアドレスが設定されたときだけ mailto を送る。プレースホルダ
# （example.com / your-email 等）や空のままでは mailto を一切送らない
# （偽の連絡先を polite pool に申告するのは匿名アクセスより悪いため）。
OPENALEX_EMAIL = ""
# OpenAlex API キー。2026-02-13 以降、OpenAlex は全 API リクエストでキー必須
# （無料。openalex.org/settings/api で取得し api_key クエリパラメータとして送る）。
OPENALEX_API_KEY = ""
# CiNii OpenSearch の appid（NII 開発者登録で取得・必須パラメータ）。
# 未設定なら CiNii ティアはスキップする（規約外の keyless 呼び出しをしない）。
CINII_APPID = ""

# プレースホルダメール判定（example.com 系ドメイン / "your-email" 系ローカル部）
_PLACEHOLDER_EMAIL_PATTERN = re.compile(
    r"(@example\.(com|org|net)$)|(^your[-_.]?e?mail)", re.IGNORECASE)


def _is_real_email(email: str) -> bool:
    """
    Meaning: mailto に載せてよい「実在しうる連絡先」かを判定する。
    Action:  空文字・@なし・プレースホルダ（example.com 系 / your-email 系）を拒否する。
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        return False
    return not _PLACEHOLDER_EMAIL_PATTERN.search(email)


def make_headers(email: str = "") -> dict:
    """
    Meaning: 書誌 API 用の User-Agent ヘッダを組み立てる。
    Action:  実在しうるメールが与えられたときだけ "(mailto:...)" を付ける。
             それ以外はツール名＋リポジトリ URL のみ（匿名アクセス）。
    """
    if _is_real_email(email):
        return {"User-Agent": f"pdf-metadata-filler/1.0 (mailto:{email.strip()})"}
    return {"User-Agent": "pdf-metadata-filler/1.0 "
                          "(https://github.com/eUmeda/pdf-metadata-filler)"}


HEADERS = make_headers()  # デフォルトは mailto なし。load_api_keys() で実メール設定時のみ更新

# タイトル類似度の最低閾値（0.0〜1.0）
# 根拠: difflib.SequenceMatcher は完全一致で1.0、無関係で0.0に近い値を返す。
#       APIの全文検索は関連性スコア順ではなくキーワード適合順のため、
#       全く別の論文が返ることがある（今回のVon Mises論文で実際に発生）。
#       0.6を下回る一致率は「別の論文」とみなして棄却する。
TITLE_SIMILARITY_THRESHOLD = 0.6

# 本文冒頭から何文字読むか（DOI検索用）
TEXT_SCAN_CHARS = 3000

# DOIの正規表現パターン
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)

# スキャンPDF判定: pypdfで抽出できたテキストがこの文字数未満なら画像PDFとみなす
# 根拠: 正常なテキストPDFは1ページで数百文字以上抽出できる。
#       スキャンPDFは0〜数十文字（PDFのアーティファクト文字のみ）になる。
SCANNED_TEXT_THRESHOLD = 50

# OCR時のDPI（解像度）。300dpiが精度と速度のバランスが良い。
# 高解像度ほど精度は上がるが処理時間も増える（150→高速低精度、400→低速高精度）。
OCR_DPI = 300

# OCRで読み込む最大ページ数（冒頭ページにDOIがある前提）
OCR_MAX_PAGES = 3

# Tesseractに渡す言語パック。英語のみインストール済みの場合は "eng"。
# 日本語論文を扱う場合は "eng+jpn" に変更し、tesseract-ocr-jpn を要インストール。
OCR_LANG = "eng+jpn"


# ------------------------------------------------------------------ #
# 書誌 API 共通 HTTP ヘルパ（429/5xx バックオフ + 一時障害とティア無効化の区別）
# ------------------------------------------------------------------ #
class ApiTransientError(Exception):
    """
    Meaning: 一時的な API 障害（429/5xx がリトライ後も継続、またはネットワーク断）。
    Action:  呼び出し側はこのファイルを deferred（後で再実行）として扱い、
             LLM の未検証提案へはフォールバックしない。
             （ネットワーク断を「ヒットなし」と混同して未検証メタデータを
             恒久書込しないための区別。恒久 failed にもしない。）
    """


# この実行中に 429/5xx が解消しなかった書誌 API プロバイダ（以後は即 transient 扱い）
_api_rate_limited = set()
# この実行中に無効化された書誌 API ティア（401/403/409 = キー必須・権限なし・クレジット枯渇）
_api_tier_disabled = set()
# バックオフの待機秒（指数: 2→4→8 秒。最大3回リトライ）
_BACKOFF_DELAYS = (2.0, 4.0, 8.0)


def _disable_api_tier(provider: str, reason: str) -> None:
    """
    Meaning: 書誌 API ティアをこの実行では無効化する（per-file failed にはしない）。
    Action:  初回のみ警告を出し、以後そのティアの fetch は即 None（次ティアへ移る）。
    """
    if provider not in _api_tier_disabled:
        _api_tier_disabled.add(provider)
        print(f"[警告] {provider} ティアをこの実行では無効化します: {reason}")


def _http_get_with_backoff(provider: str, url: str, params=None, timeout=10):
    """
    Meaning: 書誌 API（OpenAlex/CrossRef/CiNii）への GET を共通のバックオフ付きで行う。
    Action:  429/5xx のときは Retry-After ヘッダを尊重（無ければ 2→4→8 秒の指数バックオフ）
             して最大3回まで再試行する。再試行が尽きた場合と接続レベルの失敗
             （requests.RequestException）は ApiTransientError を送出する（恒久 failed にしない）。
             それ以外はレスポンスをそのまま返す（ステータスコード判定は呼び出し側で行う）。
    """
    if provider in _api_rate_limited:
        raise ApiTransientError(f"{provider}: この実行中に 429/5xx が解消しなかったため休止中")
    for attempt in range(len(_BACKOFF_DELAYS) + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
            if attempt < len(_BACKOFF_DELAYS):
                time.sleep(_BACKOFF_DELAYS[attempt])
                continue
            raise ApiTransientError(f"{provider}: 接続失敗（リトライ3回後も継続）: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < len(_BACKOFF_DELAYS):
                wait = _BACKOFF_DELAYS[attempt]
                retry_after = resp.headers.get("Retry-After", "")
                try:
                    wait = min(float(retry_after), 120.0)  # 異常に長い指示は2分で打ち切る
                except ValueError:
                    pass  # 欠落・HTTP-date 形式のときは指数バックオフ値のまま
                time.sleep(wait)
                continue
            _api_rate_limited.add(provider)
            raise ApiTransientError(f"{provider}: HTTP {resp.status_code} がリトライ3回後も継続")
        return resp
    raise ApiTransientError(f"{provider}: 到達不能")  # 論理上ここには来ない（防御）


# ------------------------------------------------------------------ #
# LLM フリー枠ガード（オーバーフロー防止: 日次トークン予算 + RPM スロットル）
# ------------------------------------------------------------------ #
# Meaning: 無料枠を超えないよう、プロバイダ別に「1日のトークン予算」を事前管理し、
#          予算に達したら別プロバイダへ切替、両方尽きたら当該PDFは deferred（翌日再開）にする。
#          429 を踏む前に止めるのが要点（事後ではなく事前ガード）。
# LLM に渡す本文の最大文字数（トークン節約。書誌情報は冒頭に集中するため短くてよい）
LLM_TEXT_CHARS = 1200
# プロバイダ別フリー枠の保守的な日次トークン予算と最小呼出間隔(秒, RPM由来)
#   Groq llama-3.3-70b-versatile 無料: 100,000 TPD / 30 RPM      → 予算 90k / 2.2s
#   Gemini 2.5 flash 無料: 250 req/day / 10 RPM（TPD は非公開）   → 予算 150k / 6.1s
#   （デフォルトモデル変更 2026-06: 旧 llama-3.1-8b / gemini-1.5-flash は廃止・引退済み）
LLM_LIMITS = {
    "groq":   {"tpd": 90_000,  "min_interval": 2.2},
    "gemini": {"tpd": 150_000, "min_interval": 6.1},
}
_LLM_USAGE_FILE   = None     # 日次使用量の永続化先（main で走査対象ディレクトリに設定）
_llm_usage        = {}       # {provider: tokens_used_today}
_llm_last_call    = {}       # {provider: epoch_seconds}
_llm_rate_limited = set()    # この実行中に 429 を返したプロバイダ（以後使わない）
_llm_disabled     = set()    # モデル不存在(404)等の恒久的設定エラーで無効化したプロバイダ


def _today_str():
    return datetime.date.today().isoformat()


def load_llm_usage(path):
    """日次トークン使用量を読み込む（日付が変わっていればリセット）。
    壊れたファイルは黙ってリセットせず、.corrupt-<時刻> に退避してから新規に始める。"""
    global _LLM_USAGE_FILE, _llm_usage
    _LLM_USAGE_FILE = str(path)
    if not Path(_LLM_USAGE_FILE).exists():
        _llm_usage = {}
        return
    try:
        d = json.load(open(_LLM_USAGE_FILE, encoding="utf-8"))
        _llm_usage = d.get("usage", {}) if d.get("date") == _today_str() else {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, AttributeError) as e:
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        corrupt = f"{_LLM_USAGE_FILE}.corrupt-{ts}"
        try:
            os.replace(_LLM_USAGE_FILE, corrupt)
            print(f"[警告] {_LLM_USAGE_FILE} が壊れています ({e})。{corrupt} に退避して新規に始めます。")
        except OSError as e2:
            print(f"[警告] 壊れた {_LLM_USAGE_FILE} の退避に失敗: {e2}")
        _llm_usage = {}


_llm_usage_save_warned = False   # 保存失敗の警告は1回だけ出す


def _save_llm_usage():
    """日次使用量をアトミックに保存する（同一ディレクトリの一時ファイル → os.replace）。
    保存に失敗した場合は黙殺せず、1回だけ警告を出す（無料枠ガードが実行間で持続しない）。"""
    global _llm_usage_save_warned
    if not _LLM_USAGE_FILE:
        return
    tmp = f"{_LLM_USAGE_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": _today_str(), "usage": _llm_usage}, f)
        os.replace(tmp, _LLM_USAGE_FILE)
    except OSError as e:
        if not _llm_usage_save_warned:
            print(f"[警告] LLM日次使用量を保存できません: {_LLM_USAGE_FILE} ({e})")
            print("       無料枠ガードが実行をまたいで持続しません。書込可能なディレクトリか確認してください。")
            _llm_usage_save_warned = True


def _est_tokens(text):
    """おおまかなトークン見積り（入力 ~4文字/トークン + プロンプト/出力の固定分）。"""
    return len(text[:LLM_TEXT_CHARS]) // 4 + 320


def _provider_ok(prov, est):
    """そのプロバイダが（恒久無効化されておらず、429未踏 かつ 日次予算内）で使えるか。"""
    if prov in _llm_disabled or prov in _llm_rate_limited:
        return False
    return (_llm_usage.get(prov, 0) + est) <= LLM_LIMITS[prov]["tpd"]


def _throttle(prov):
    """RPM を守るためプロバイダ別に最小間隔を空ける。"""
    iv = LLM_LIMITS[prov]["min_interval"]
    wait = iv - (time.time() - _llm_last_call.get(prov, 0))
    if wait > 0:
        time.sleep(wait)
    _llm_last_call[prov] = time.time()


def _record_usage(prov, est):
    _llm_usage[prov] = _llm_usage.get(prov, 0) + est
    _save_llm_usage()


def _is_rate_limit(e) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("429", "rate limit", "rate_limit", "quota",
                                "resource_exhausted", "tokens per day", "tpd"))


def llm_propose(text):
    """
    Meaning: フリー枠を超えないように書誌情報の LLM 提案を得る。
    Action:  Groq→Gemini の順に、(1)恒久無効化されていない (2)この実行で429未踏
             (3)日次トークン予算内 のものだけ使う。呼出前に RPM スロットルする
             （使用トークンの加算は REST 呼び出し側 _llm_rest_call が「レスポンスが
             実際に返った呼び出し」についてのみ行う）。
             戻り値: bib(dict)=提案成功 / None=提案得られず（恒久エラー含む） /
                     "QUOTA"=全プロバイダ予算切れ、または走行中の 429 で全滅
                     （→ 呼び出し側は deferred_quota とし、failed にしない）。
    """
    est = _est_tokens(text)
    candidates = []
    if _GROQ_API_KEY:
        candidates.append("groq")
    if _LLM_PROVIDER == "gemini" and _GEMINI_API_KEY:
        candidates.append("gemini")
    if not candidates:
        return None
    usable = [p for p in candidates if p not in _llm_disabled]
    if not usable:
        return None  # モデル不存在等の恒久エラーのみ → 翌日待ちにせず提案なしで返す
    available = [p for p in usable if _provider_ok(p, est)]
    if not available:
        return "QUOTA"
    for prov in available:
        _throttle(prov)
        if prov == "groq":
            bib = extract_bib_with_groq(text)
        else:
            bib = extract_bib_with_llm(text)
            if bib:
                bib["_source"] = "gemini_text_fallback"
        if bib:
            return bib
        # 提案が得られなかった（429 含む）→ 次の available へ。429 は _provider_ok が以後弾く
    # 走行中の 429 で全プロバイダが脱落した場合は恒久 failed ではなく QUOTA（deferred_quota）
    remaining = [p for p in available
                 if p not in _llm_rate_limited and p not in _llm_disabled]
    if not remaining and any(p in _llm_rate_limited for p in available):
        return "QUOTA"
    return None


# ------------------------------------------------------------------ #
# APIキー設定ファイルの読み込みとLLMクライアント初期化
# ------------------------------------------------------------------ #
def load_api_keys(keys_path: Path) -> None:
    """
    Meaning: api_keys.json を読み込み、書誌 API キー・連絡先メール・LLM 設定を初期化する
    Action:  openalex.email / openalex.api_key / cinii.appid / gemini.* / groq.* を読み込む。
             LLM は groq → gemini の優先順で primary を決める（requests による REST 呼び出し。
             SDK は使わない）。ファイルが存在しない・キーが空の場合はLLMなしで動作する
             （エラーにしない）。OpenAlex キー未設定時は1回だけ警告して keyless で試行する。
    """
    global _LLM_PROVIDER, _LLM_MODEL, HEADERS, OPENALEX_EMAIL, OPENALEX_API_KEY
    global _GEMINI_API_KEY, _GEMINI_MODEL, _GROQ_API_KEY, _GROQ_MODEL, CINII_APPID

    cfg = {}
    if not keys_path.exists():
        print(f"[情報] APIキーファイルが見つかりません: {keys_path}")
        print("       LLMなしで動作します（ヒューリスティックOCRフォールバックのみ）。")
    else:
        try:
            with open(keys_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"[警告] APIキーファイルの読み込み失敗: {e}")
            cfg = {}

    # 連絡先メール: 実在しうるアドレスのときのみ mailto として送る（プレースホルダは送らない）
    oa_email = cfg.get("openalex", {}).get("email", "").strip()
    if _is_real_email(oa_email):
        OPENALEX_EMAIL = oa_email
        HEADERS = make_headers(oa_email)
        print(f"[設定] 連絡先メール (User-Agent mailto / CrossRef polite): {OPENALEX_EMAIL}")
    elif oa_email:
        print(f"[警告] openalex.email がプレースホルダのため mailto は送りません: {oa_email}")

    # OpenAlex API キー（2026-02-13 以降は全リクエストでキー必須・無料）
    OPENALEX_API_KEY = cfg.get("openalex", {}).get("api_key", "").strip()
    if not OPENALEX_API_KEY:
        print("[警告] OpenAlex は 2026-02-13 以降 API キーが必須です"
              "（無料。openalex.org/settings/api で取得し api_keys.json の openalex.api_key に設定）。")
        print("       未設定のままテスト枠（約100クレジット/日）で試行します。"
              "401/403/409 が返ったら OpenAlex ティアをスキップし CrossRef 以降で継続します。")

    # CiNii appid（NII 開発者登録で取得。未設定なら CiNii ティアはスキップ＝fetch 側で1回警告）
    CINII_APPID = cfg.get("cinii", {}).get("appid", "").strip()

    # Groq の API キーは primary が何であれ最終フォールバック用に常時保持する
    # （API全滅したPDFについて本文/OCRテキストから書誌情報を提案させ、そのまま採用するため）
    _GROQ_API_KEY = cfg.get("groq", {}).get("api_key", "").strip()
    if _GROQ_API_KEY:
        _GROQ_MODEL = cfg.get("groq", {}).get("model", "llama-3.3-70b-versatile")
        print(f"[設定] Groq フォールバック有効 ({_GROQ_MODEL})")

    # Gemini を優先して primary にする
    _GEMINI_API_KEY = cfg.get("gemini", {}).get("api_key", "").strip()
    if _GEMINI_API_KEY:
        _GEMINI_MODEL = cfg.get("gemini", {}).get("model", "gemini-2.5-flash")
        _LLM_PROVIDER = "gemini"
        _LLM_MODEL    = _GEMINI_MODEL
        print(f"[設定] LLM: Gemini ({_GEMINI_MODEL})")
        return

    # Geminiが使えなければGroqをprimaryにする
    if _GROQ_API_KEY:
        _LLM_PROVIDER = "groq"
        _LLM_MODEL    = _GROQ_MODEL
        print(f"[設定] LLM: Groq ({_GROQ_MODEL})")
        return

    if not _LLM_PROVIDER:
        print("[情報] 有効なLLM APIキーが見つかりません。ヒューリスティックOCRフォールバックで動作します。")


# ------------------------------------------------------------------ #
# LLMによる書誌情報構造化抽出
# ------------------------------------------------------------------ #

# LLMに渡すプロンプトテンプレート
# Meaning: OCRテキストを渡して書誌情報をJSONで返させる
# Action:  JSONのみを返すよう明示し、キーをfixedにすることでパースを安定させる
_LLM_PROMPT_TEMPLATE = """\
以下は学術論文のOCRテキスト（冒頭部分）です。
このテキストから書誌情報を抽出し、必ず以下のJSONフォーマットのみで返してください。
コードブロック・説明文・前置き・後置きは一切不要です。JSONだけを返してください。

抽出するフィールド（テキストに明示されていない項目は推測せず空文字列にする。特に DOI は捏造しない）:
- title: 論文タイトル（英語はタイトルケースに正規化。不明なら空文字列）
- author: 第一著者の実際の姓名（複数著者なら "<実際の姓名> et al."）。判別できなければ空文字列。
  "First Author" 等のプレースホルダ語は絶対に使わない
- year: 出版年（4桁の数字。不明なら空文字列）
- journal: 掲載誌名（不明なら空文字列）
- doi: テキスト内に明記された DOI のみ（"10." 始まり）。無ければ空文字列。推測・捏造は禁止

出力フォーマット（このJSONのみ返すこと）:
{{"title": "...", "author": "...", "year": "...", "journal": "...", "doi": "..."}}

--- OCRテキスト開始 ---
{ocr_text}
--- OCRテキスト終了 ---
"""

# LLM へ system role として渡す共通指示（Groq の messages / Gemini はプロンプトに内包済み）
_LLM_SYSTEM_PROMPT = ("You are a bibliographic metadata extractor. "
                      "Return only valid JSON, no explanation, no markdown.")


def _llm_rest_call(prov: str, prompt: str) -> str | None:
    """
    Meaning: LLM を素の REST（requests）で呼び出し、応答テキストを返す（SDK 不使用）。
    Action:  gemini は generateContent エンドポイント（x-goog-api-key ヘッダ）、
             groq は OpenAI 互換 chat/completions（Bearer）に POST する。
             日次使用量は「レスポンスが実際に返った呼び出し」についてのみ加算する
             （接続レベルの失敗ではトークンを消費していないため加算しない）。
             429/レート制限 → _llm_rate_limited、404/モデル不存在 → _llm_disabled に登録し、
             以後そのプロバイダは使わない。失敗時は None を返す。
    """
    est = _est_tokens(prompt)
    try:
        if prov == "gemini":
            resp = requests.post(
                GEMINI_GENERATE_URL.format(model=_GEMINI_MODEL),
                headers={"x-goog-api-key": _GEMINI_API_KEY,
                         "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0}},
                timeout=60,
            )
        else:  # groq
            resp = requests.post(
                GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {_GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": _GROQ_MODEL,
                      "messages": [
                          {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                          {"role": "user", "content": prompt},
                      ],
                      "temperature": 0,
                      "max_tokens": 256},
                timeout=60,
            )
    except requests.RequestException as e:
        print(f"  [警告] {prov} API接続失敗: {e}")
        return None

    _record_usage(prov, est)  # レスポンスが返った呼び出しのみ使用量に加算する

    if resp.status_code == 429:
        _llm_rate_limited.add(prov)
        print(f"  [警告] {prov} がレート制限 (429)。この実行では以後使いません。")
        return None
    if resp.status_code == 404:
        _llm_disabled.add(prov)
        model = _GEMINI_MODEL if prov == "gemini" else _GROQ_MODEL
        print(f"  [警告] {prov} のモデルが見つかりません (404: {model})。プロバイダを無効化します。")
        print(f"         廃止/改名されたモデルの可能性。api_keys.json の {prov}.model を現行モデルに更新してください。")
        return None
    if resp.status_code >= 400:
        body = resp.text[:200]
        if _is_rate_limit(body):
            _llm_rate_limited.add(prov)
        print(f"  [警告] {prov} API呼び出し失敗 (HTTP {resp.status_code}): {body}")
        return None

    try:
        data = resp.json()
        if prov == "gemini":
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            raw = data["choices"][0]["message"]["content"]
        return (raw or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as e:
        print(f"  [警告] {prov} レスポンスの解析失敗: {e}")
        return None


def extract_bib_with_llm(ocr_text: str) -> dict | None:
    """
    Meaning: OCRテキストをLLMに渡して書誌情報をJSON形式で構造化抽出する
    Action:  _LLM_PROVIDER に応じてGeminiまたはGroqのREST APIを叩き、
             返ってきたJSON文字列をパースして辞書を返す。
             パース失敗・API失敗の場合はNoneを返す。
             LLMが未設定（_LLM_PROVIDER=None）の場合も即座にNoneを返す。
    """
    if _LLM_PROVIDER is None:
        return None

    # OCRテキストは冒頭 LLM_TEXT_CHARS 文字に絞る（トークン節約。書誌情報は冒頭に集中する）
    prompt = _LLM_PROMPT_TEMPLATE.format(ocr_text=ocr_text[:LLM_TEXT_CHARS])

    # REST 呼び出し（429/404 のプロバイダ無効化・使用量加算はヘルパ側で行う）
    raw = _llm_rest_call(_LLM_PROVIDER, prompt)
    if not raw:
        return None

    # JSON文字列のパース
    # Meaning: LLMがコードブロック（```json...```）で囲んで返す場合に対応する
    # Action:  バッククォートと "json" ラベルを除去してからパースする
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        bib = json.loads(raw)
    except json.JSONDecodeError:
        # Meaning: 完全なJSONでなくても { } の中身だけ取り出せる場合がある
        # Action:  最初の { から最後の } までを切り出して再度パースを試みる
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                bib = json.loads(m.group(0))
            except json.JSONDecodeError:
                print(f"  [警告] LLMレスポンスのJSONパース失敗: {raw[:100]}")
                return None
        else:
            print(f"  [警告] LLMレスポンスにJSONが見つからない: {raw[:100]}")
            return None

    # 必須フィールドが揃っているか確認する
    required = {"title", "author", "year", "journal", "doi"}
    if not required.issubset(bib.keys()):
        print(f"  [警告] LLM抽出結果にフィールド不足: {bib}")
        return None

    # 値を文字列に正規化する（LLMがnullや数値を返す場合がある）
    bib = {k: str(v).strip() if v else "" for k, v in bib.items()}
    bib["_source"] = "llm_ocr_fallback"
    return bib


def extract_bib_with_groq(text: str) -> dict | None:
    """
    Meaning: 本文/OCRテキストを Groq に渡して書誌情報をJSONで提案させる（API全滅時の最終手段）。
    Action:  primary が Gemini でも常に Groq の REST API を直接使う（user 方針: 失敗分は Groq 提案を
             そのまま採用）。返ったJSONをパースし、_source に "groq_text_fallback" を付して返す。
             API（OpenAlex/CrossRef）で確証が取れていない提案であることを示すマーカー。
    """
    if not _GROQ_API_KEY:
        return None

    prompt = _LLM_PROMPT_TEMPLATE.format(ocr_text=text[:LLM_TEXT_CHARS])
    raw = _llm_rest_call("groq", prompt)
    if not raw:
        return None

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE).strip()
    try:
        bib = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            print(f"  [警告] GroqレスポンスにJSONが見つからない: {raw[:100]}")
            return None
        try:
            bib = json.loads(m.group(0))
        except json.JSONDecodeError:
            print(f"  [警告] GroqレスポンスのJSONパース失敗: {raw[:100]}")
            return None

    required = {"title", "author", "year", "journal", "doi"}
    if not required.issubset(bib.keys()):
        print(f"  [警告] Groq抽出結果にフィールド不足: {bib}")
        return None

    bib = {k: str(v).strip() if v else "" for k, v in bib.items()}
    bib["_source"] = "groq_text_fallback"
    return bib


def _get_text_for_llm(pdf_path: Path) -> str:
    """
    Meaning: LLM に渡す本文テキストを取得する。テキストPDFは pypdf 抽出、スキャンPDFは OCR。
    Action:  まず pypdf で先頭5ページから TEXT_SCAN_CHARS まで読む。抽出量が
             SCANNED_TEXT_THRESHOLD 未満なら OCR（_get_ocr_text）にフォールバックする。
    """
    try:
        reader = PdfReader(str(pdf_path))
        text = ""
        for page in reader.pages[:5]:
            text += page.extract_text() or ""
            if len(text) >= TEXT_SCAN_CHARS:
                break
        text = text[:TEXT_SCAN_CHARS]
    except Exception:
        text = ""
    if len(text.strip()) < SCANNED_TEXT_THRESHOLD:
        return _get_ocr_text(pdf_path)
    return text


def extract_doi_from_filename(pdf_path: Path) -> str:
    """
    Meaning: ファイル名自体に埋め込まれた DOI を拾う（URLエンコードされている場合も対応）。
    Action:  ファイル名を URL デコード（%2F→/ 等）してから DOI_PATTERN で検索する。
             例: '10.1016%2FS0065-2504%2808%2960215-9 (1).pdf' → 10.1016/S0065-2504(08)60215-9
             論文を DOI 由来の名前で保存していることが多く、API 検証付きで確実に補完できる。
    """
    import urllib.parse
    # .stem で拡張子(.pdf)を落としてからデコード（DOI に .pdf が混入しないように）
    name = urllib.parse.unquote(pdf_path.stem)
    m = DOI_PATTERN.search(name)
    if m:
        return m.group(0).strip(".,;)")
    return ""


# ------------------------------------------------------------------ #
# メタデータ検査
# ------------------------------------------------------------------ #
def check_missing(meta: dict) -> list[str]:
    """
    Meaning: Title / Author / DOI のうち空または未設定のフィールドを返す
    Action:  各フィールドの値が None か空文字列かを確認してリストに追加する
    """
    missing = []
    for key in ["title", "author", "doi"]:
        val = meta.get(key, "")
        if not val or not str(val).strip():
            missing.append(key)
    return missing


def read_pdf_metadata(pdf_path: Path) -> dict:
    """
    Meaning: PDFのメタデータフィールドを辞書として返す
    Action:  PdfReaderでメタデータを読み取り、title/author/subjectを抽出する。
             /Subject または /Keywords にDOI文字列が入っている場合もDOIとして拾う。
    """
    try:
        reader = PdfReader(str(pdf_path))
        meta = reader.metadata or {}
    except Exception as e:
        print(f"  [警告] メタデータ読み取り失敗: {pdf_path.name} ({e})")
        return {"title": "", "author": "", "doi": ""}

    title  = meta.get("/Title",   "") or ""
    author = meta.get("/Author",  "") or ""
    subj   = meta.get("/Subject", "") or ""
    kw     = meta.get("/Keywords","") or ""

    # /Subject や /Keywords にDOIが含まれていれば拾う
    doi = ""
    for text in [subj, kw]:
        m = DOI_PATTERN.search(text)
        if m:
            doi = m.group(0).strip(".,;)")
            break

    return {"title": title.strip(), "author": author.strip(), "doi": doi.strip()}


# ------------------------------------------------------------------ #
# スキャンPDF判定とOCR
# ------------------------------------------------------------------ #
def is_scanned_pdf(pdf_path: Path) -> bool:
    """
    Meaning: PDFが画像のみのスキャンPDF（テキストレイヤーなし）かどうかを判定する
    Action:  pypdfで最初の3ページのテキストを抽出し、合計文字数が
             SCANNED_TEXT_THRESHOLD 未満であればスキャンPDFと判定してTrueを返す。
             テキストPDFなら通常数百文字以上抽出できるため、この閾値で区別できる。
    """
    try:
        reader = PdfReader(str(pdf_path))
        total_chars = 0
        for page in reader.pages[:3]:
            total_chars += len(page.extract_text() or "")
        return total_chars < SCANNED_TEXT_THRESHOLD
    except Exception:
        return False  # 判定失敗時はスキャンPDFとみなさない


# =============================================================================
# PDF page rasterization helper (PyMuPDF/fitz -> pypdfium2 port)
# -----------------------------------------------------------------------------
# fitz -> pypdfium2 API mapping (derivation chain for the AGPL -> Apache/BSD swap):
#   fitz.open(str(p))                            -> pdfium.PdfDocument(str(p))
#   doc[i]                                       -> doc[i]            (PdfPage)
#   zoom = DPI/72; fitz.Matrix(zoom, zoom)       -> scale = DPI/72.0
#   page.get_pixmap(matrix=, colorspace=csGRAY)  -> page.render(scale=, grayscale=True)
#   pix.tobytes("png") -> Image.open(BytesIO)    -> bitmap.to_pil()
# Behavior preserved: grayscale raster at DPI, returned as a PIL Image (mode "L").
# NOTE: bitmap.to_pil() shares the bitmap's native buffer, so .copy() BEFORE
#       closing the bitmap (else use-after-free). Reviewed: Codex + Claude (sonnet).
# =============================================================================
def render_page_to_pil(doc, page_index: int, dpi: int):
    """Render one PDF page to a grayscale PIL Image (mode 'L') at the given DPI."""
    page = doc[page_index]
    try:
        scale = dpi / 72.0                                 # == former fitz zoom (DPI/72)
        bitmap = page.render(scale=scale, grayscale=True)  # grayscale == former fitz.csGRAY
        try:
            img = bitmap.to_pil().copy()                   # own the buffer before close
        finally:
            bitmap.close()                                 # close even if to_pil() raises
    finally:
        page.close()                                       # close even if render() raises
    return img


def extract_doi_via_ocr(pdf_path: Path) -> str:
    """
    Meaning: スキャンPDFの冒頭ページをOCRしてDOI文字列を抽出する
    Action:  pypdfium2でページをOCR_DPIの解像度でグレースケール画像にラスタライズし、
             pytesseractでテキストに変換してからDOI_PATTERNで検索する。
             OCRが利用不可（インポートエラー）の場合は空文字列を返す。

    依存: pypdfium2, pytesseract, Pillow, tesseract CLI
    インストール: pip install pypdfium2 pytesseract Pillow
                  Mac: brew install tesseract
                  Ubuntu: apt install tesseract-ocr
    日本語論文の場合: OCR_LANG = "eng+jpn" に変更 + tesseract-ocr-jpn をインストール
    """
    if not OCR_AVAILABLE:
        print("  [警告] OCRライブラリ未インストール。スキップします。")
        print("         pip install pypdfium2 pytesseract Pillow を実行してください。")
        return ""

    try:
        ocr_text = ""
        doc = pdfium.PdfDocument(str(pdf_path))   # 注: pypdfium2 4.x の PdfDocument は with 非対応 → try/finally で close
        try:
            for page_num in range(min(OCR_MAX_PAGES, len(doc))):
                # ページをグレースケール画像にラスタライズ（render_page_to_pil 経由）
                img = render_page_to_pil(doc, page_num, OCR_DPI)
                page_text = pytesseract.image_to_string(img, lang=OCR_LANG)
                ocr_text += page_text

                # DOIが見つかった時点で後続ページのOCRを打ち切る（速度最適化）
                if DOI_PATTERN.search(ocr_text):
                    break
        finally:
            doc.close()

    except Exception as e:
        print(f"  [警告] OCR処理失敗: {pdf_path.name} ({e})")
        return ""

    matches = DOI_PATTERN.findall(ocr_text)
    if matches:
        doi = matches[0].strip(".,;)")
        return doi
    return ""


# ------------------------------------------------------------------ #
# 本文テキストからDOIを抽出
# ------------------------------------------------------------------ #
def extract_doi_from_text(pdf_path: Path) -> str:
    """
    Meaning: PDFの本文冒頭テキストからDOI文字列を正規表現で検出する。
             テキスト抽出量が少ない（スキャンPDF）場合はOCRにフォールバックする。
    Action:  まずpypdfでテキスト抽出を試みる。
             抽出量が SCANNED_TEXT_THRESHOLD 未満なら is_scanned_pdf() でスキャン判定し、
             スキャンPDFであれば extract_doi_via_ocr() でOCRを実行する。
    """
    try:
        reader = PdfReader(str(pdf_path))
        text = ""
        for page in reader.pages[:5]:  # 最初の5ページだけ読む
            text += page.extract_text() or ""
            if len(text) >= TEXT_SCAN_CHARS:
                break
        text = text[:TEXT_SCAN_CHARS]
    except Exception as e:
        print(f"  [警告] 本文テキスト読み取り失敗: {pdf_path.name} ({e})")
        return ""

    # テキストが十分に抽出できたか確認する
    if len(text.strip()) < SCANNED_TEXT_THRESHOLD:
        # Meaning: テキストレイヤーがほぼ空 → スキャンPDFの可能性が高い
        # Action:  OCRにフォールバックしてDOIを探す
        print("  テキスト抽出量が少ない（スキャンPDFの可能性）→ OCRを試みます...")
        return extract_doi_via_ocr(pdf_path)

    matches = DOI_PATTERN.findall(text)
    if matches:
        # 末尾の句読点などを除去
        doi = matches[0].strip(".,;)")
        return doi
    return ""


# ------------------------------------------------------------------ #
# OpenAlex APIによる書誌情報取得
# ------------------------------------------------------------------ #
def fetch_by_doi(doi: str) -> dict | None:
    """
    Meaning: DOIを使ってOpenAlex Works APIから書誌情報を取得する
    Action:  filter=doi:... でAPIを叩き、title/author/year/journal/doiを返す。
             ヒットなし・解析失敗は None。401/403/409（キー必須/権限なし/クレジット枯渇）は
             OpenAlex ティアをこの実行では無効化して None（per-file failed にしない）。
             429/5xx/接続断は _http_get_with_backoff が ApiTransientError を送出する（deferred扱い）。
    """
    if "openalex" in _api_tier_disabled:
        return None
    params = {
        # DOI 中のカンマは OpenAlex の filter 区切りと衝突するため %2C にエスケープする
        "filter": f"doi:{doi.replace(',', '%2C')}",
        "select": "title,authorships,publication_year,primary_location,doi",
    }
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    resp = _http_get_with_backoff("openalex", OPENALEX_WORKS_URL, params=params)
    if resp.status_code in (401, 403, 409):
        _disable_api_tier("openalex",
                          f"HTTP {resp.status_code}（APIキー必須/権限なし/無料クレジット枯渇。"
                          "openalex.org/settings/api で無料キーを取得し api_keys.json に設定）")
        return None
    try:
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        return _parse_openalex_work(results[0])
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
        print(f"  [警告] OpenAlex DOI検索失敗 ({doi}): {e}")
        return None


def _title_similarity(a: str, b: str) -> float:
    """
    Meaning: 2つのタイトル文字列の類似度を0.0〜1.0で返す
    Action:  両方を小文字化・前後空白除去してから difflib.SequenceMatcher に渡す。
             完全一致で1.0、無関係な文字列で0.0に近い値になる。
             大文字小文字やハイフン表記の揺れ（ratio-of-uniforms vs ratio—of—uniforms）
             は小文字化だけでは吸収できないため、SequenceMatcherが部分一致として拾う。
    """
    a = a.lower().strip()
    b = b.lower().strip()
    return difflib.SequenceMatcher(None, a, b).ratio()


def fetch_by_title(title: str) -> dict | None:
    """
    Meaning: タイトル文字列でOpenAlex全文検索し、返ってきた論文が
             クエリタイトルと十分に類似していれば書誌情報を返す
    Action:  search=... でAPIを叩いた後、ヒット論文のタイトルと
             クエリタイトルの類似度を _title_similarity() で計算する。
             TITLE_SIMILARITY_THRESHOLD 未満なら「別の論文」とみなして棄却し None を返す。
             タイトルが短すぎる（10文字未満）場合は誤ヒットリスクが高いためスキップする。
    """
    if len(title.strip()) < 10:
        print(f"  [スキップ] タイトルが短すぎるため全文検索をスキップ: '{title}'")
        return None

    if "openalex" in _api_tier_disabled:
        return None
    params = {
        "search": title,
        "select": "title,authorships,publication_year,primary_location,doi",
        "per-page": 1,
    }
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    resp = _http_get_with_backoff("openalex", OPENALEX_WORKS_URL, params=params)
    if resp.status_code in (401, 403, 409):
        _disable_api_tier("openalex",
                          f"HTTP {resp.status_code}（APIキー必須/権限なし/無料クレジット枯渇。"
                          "openalex.org/settings/api で無料キーを取得し api_keys.json に設定）")
        return None
    try:
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        bib = _parse_openalex_work(results[0])
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
        print(f"  [警告] OpenAlex タイトル検索失敗: {e}")
        return None

    # Meaning: APIが返した論文タイトルがクエリと一致しているか確認する
    # Action:  類似度が閾値未満なら棄却してNoneを返す
    sim = _title_similarity(title, bib.get("title", ""))
    if sim < TITLE_SIMILARITY_THRESHOLD:
        print(f"  [棄却] OpenAlexヒットのタイトルが一致しない (類似度={sim:.2f})")
        print(f"         クエリ:  '{title[:60]}'")
        print(f"         ヒット:  '{bib.get('title', '')[:60]}'")
        return None

    return bib


# ------------------------------------------------------------------ #
# CrossRef APIによる書誌情報取得（OpenAlexフォールバック）
# ------------------------------------------------------------------ #
def fetch_by_doi_crossref(doi: str) -> dict | None:
    """
    Meaning: DOIを使ってCrossRef APIから書誌情報を取得する
    Action:  https://api.crossref.org/works/{doi} を叩く（DOI は percent-encode する。
             '#' '?' '%' 等を含む DOI で URL が壊れないように）。
             CrossRefはOpenAlexより古い論文（1990年代以前）の収録率が高い。
             ヒットなし(404)・解析失敗は None。429/5xx/接続断は ApiTransientError（deferred扱い）。
    """
    url = f"{CROSSREF_WORKS_URL}/{quote(doi, safe='')}"
    params = {}
    if OPENALEX_EMAIL:  # 実在メール設定時のみ CrossRef polite pool に mailto を送る
        params["mailto"] = OPENALEX_EMAIL
    resp = _http_get_with_backoff("crossref", url, params=params or None)
    if resp.status_code == 404:
        return None
    try:
        resp.raise_for_status()
        work = resp.json().get("message", {})
        return _parse_crossref_work(work)
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
        print(f"  [警告] CrossRef DOI検索失敗 ({doi}): {e}")
        return None


def fetch_by_title_crossref(title: str) -> dict | None:
    """
    Meaning: タイトル文字列でCrossRef全文検索し、類似度チェックを経て書誌情報を返す
    Action:  query.title=... でAPIを叩いた後、OpenAlexと同じ類似度チェックを適用する。
             CrossRefのタイトル検索はOpenAlexより古い論文に強い。
    """
    if len(title.strip()) < 10:
        return None

    params = {
        "query.title": title,
        "rows": 1,
        "select": "title,author,published,container-title,DOI",
    }
    if OPENALEX_EMAIL:  # 実在メール設定時のみ CrossRef polite pool に mailto を送る
        params["mailto"] = OPENALEX_EMAIL
    resp = _http_get_with_backoff("crossref", CROSSREF_WORKS_URL, params=params)
    try:
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None
        bib = _parse_crossref_work(items[0])
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
        print(f"  [警告] CrossRef タイトル検索失敗: {e}")
        return None

    # 類似度チェック（OpenAlexと同じ閾値を使う）
    sim = _title_similarity(title, bib.get("title", ""))
    if sim < TITLE_SIMILARITY_THRESHOLD:
        print(f"  [棄却] CrossRefヒットのタイトルが一致しない (類似度={sim:.2f})")
        print(f"         クエリ:  '{title[:60]}'")
        print(f"         ヒット:  '{bib.get('title', '')[:60]}'")
        return None

    return bib


def _parse_crossref_work(work: dict) -> dict:
    """
    Meaning: CrossRefのWorkオブジェクトからtitle/author/year/journal/doiを抽出する
    Action:  CrossRefはtitleをリストで返す（複数表記がある場合のため）ので先頭を取る。
             著者はfamily + given を結合し、複数の場合は "First et al." 形式にする。
             published-print → published-online の優先順でyearを取る。
    """
    # タイトル: CrossRefはリスト形式
    titles = work.get("title", [])
    title = titles[0].strip() if titles else ""

    # 著者
    authors = work.get("author", [])
    if authors:
        a = authors[0]
        first_name = f"{a.get('given', '')} {a.get('family', '')}".strip()
        author = f"{first_name} et al." if len(authors) > 1 else first_name
    else:
        author = ""

    # 出版年: published-print → published-online の順で試す
    year = ""
    for date_key in ["published-print", "published-online", "published"]:
        date_parts = work.get(date_key, {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = str(date_parts[0][0])
            break

    # ジャーナル名: container-title はリスト形式
    container = work.get("container-title", [])
    journal = container[0].strip() if container else ""

    doi = work.get("DOI", "") or ""

    return {
        "title":   title,
        "author":  author,
        "year":    year,
        "journal": journal,
        "doi":     doi,
    }


def _get_ocr_text(pdf_path: Path) -> str:
    """
    Meaning: スキャンPDFの冒頭ページからOCRテキストを取得して返す
    Action:  extract_doi_via_ocr と同じラスタライズ処理を行うが、
             DOI検索せずテキスト文字列をそのまま返す。
             LLMとヒューリスティック抽出の両方がこれを共有して使う。
    """
    if not OCR_AVAILABLE:
        return ""
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            img = render_page_to_pil(doc, 0, OCR_DPI)
            text = pytesseract.image_to_string(img, lang=OCR_LANG)
        finally:
            doc.close()
        return text
    except Exception as e:
        print(f"  [警告] OCRテキスト取得失敗: {pdf_path.name} ({e})")
        return ""


def _extract_bib_from_ocr(pdf_path: Path) -> dict | None:
    """
    Meaning: APIで書誌情報が取れなかったスキャンPDFから、OCRテキストを直接解析して
             タイトルと著者名を抽出する。DOI・ジャーナル情報は空になる。
    Action:  1ページ目のOCRテキストを取得し、以下のヒューリスティックで抽出する。
             - タイトル: 大文字率70%以上の行を最大2行結合する
               （"SUMMARY"等の次セクション見出しで打ち切る）
             - 著者名:   タイトル行群の直後に現れる、先頭が大文字アルファベットで
               始まり単語数が2〜5の行（"Lucio Barabesi" のような人名パターン）を使う
             完全に正確な抽出は保証できないが「空よりはまし」な最終手段として使う。
    """
    if not OCR_AVAILABLE:
        return None

    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            img = render_page_to_pil(doc, 0, OCR_DPI)
            ocr_text = pytesseract.image_to_string(img, lang=OCR_LANG)
        finally:
            doc.close()
    except Exception as e:
        print(f"  [警告] OCR書誌抽出失敗: {pdf_path.name} ({e})")
        return None

    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    if not lines:
        return None

    # Meaning: 大文字率が高い行をタイトル候補とみなす（最大2行）
    # Action:  アルファベット文字のうち大文字が70%以上の行を拾う。
    #          "SUMMARY" "ABSTRACT" "INTRODUCTION" のような次セクション
    #          見出し語が現れたら打ち切る（それ以降はタイトルでない）。
    SECTION_WORDS = {"SUMMARY", "ABSTRACT", "INTRODUCTION", "KEYWORDS", "REFERENCES"}
    title_lines = []
    title_end_idx = -1

    for i, line in enumerate(lines):
        alpha = [c for c in line if c.isalpha()]
        if not alpha:
            continue
        upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
        # セクション見出し語だけの行なら打ち切る
        if line.upper() in SECTION_WORDS:
            title_end_idx = i
            break
        if upper_ratio >= 0.7 and len(line) > 5:
            title_lines.append(line.title())
            title_end_idx = i
            if len(title_lines) >= 2:  # 最大2行
                break

    if not title_lines:
        return None

    title = " ".join(title_lines)

    # Meaning: タイトル行群の直後から著者名候補を探す
    # Action:  先頭が大文字で始まり、単語数が2〜5、数式文字が少ない行を著者名とみなす。
    #          "Lucio Barabesi" は2単語・先頭大文字・アルファベットのみで適合する。
    author = ""
    search_start = title_end_idx + 1 if title_end_idx >= 0 else 0
    for line in lines[search_start: search_start + 6]:
        words = line.split()
        if 2 <= len(words) <= 5:
            alpha_ratio = sum(1 for c in line if c.isalpha()) / max(len(line), 1)
            if alpha_ratio > 0.7 and line[0].isupper():
                author = line
                break

    return {
        "title":   title,
        "author":  author,
        "year":    "",
        "journal": "",
        "doi":     "",
        "_source": "ocr_fallback",  # 手動確認が必要なことをステータスに記録する
    }


def _parse_openalex_work(work: dict) -> dict:
    """
    Meaning: OpenAlexのWorksオブジェクトからtitle/author/year/journal/doiを抽出する
    Action:  authorshipsの最初の著者名を取り出し、primary_locationからジャーナル名を取る。
             複数著者の場合は "First Author et al." 形式にする。
    """
    title = work.get("title", "") or ""

    # 著者
    authorships = work.get("authorships", [])
    if authorships:
        first = authorships[0].get("author", {}).get("display_name", "")
        author = f"{first} et al." if len(authorships) > 1 else first
    else:
        author = ""

    year = str(work.get("publication_year", "") or "")

    # ジャーナル名
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    journal = source.get("display_name", "") or ""

    doi = work.get("doi", "") or ""
    # DOIは "https://doi.org/10.xxxx/..." 形式で返ってくるので短縮する
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]

    return {
        "title":   title.strip(),
        "author":  author.strip(),
        "year":    year.strip(),
        "journal": journal.strip(),
        "doi":     doi.strip(),
    }


# ------------------------------------------------------------------ #
# PDFへのメタデータ書き込み
# ------------------------------------------------------------------ #
_cinii_appid_warned = False   # appid 未設定の警告は1回だけ出す


def fetch_by_title_cinii(title: str) -> dict | None:
    """
    Meaning: CiNii OpenSearch でタイトル検索し、和文紀要など OpenAlex/CrossRef 未収録の論文を拾う。
    Action:  cir.nii.ac.jp/opensearch/articles を appid 付きで叩き、items[0] を正規化。
             appid（NII 開発者登録で取得・必須パラメータ）が未設定の場合は CiNii ティアを
             スキップする（初回のみ警告）。タイトル類似度が TITLE_SIMILARITY_THRESHOLD 未満
             なら棄却。DOI は @id に含まれれば抽出（無ければ空）。
             旧 enrich パイプラインの cinii_search を踏襲。
    """
    global _cinii_appid_warned
    if len(title.strip()) < 6:
        return None
    if not CINII_APPID:
        if not _cinii_appid_warned:
            print("[情報] CiNii appid が未設定のため CiNii ティアをスキップします。")
            print("       NII 開発者登録（support.nii.ac.jp/ja/cinii/api/developer）で appid を取得し、")
            print("       api_keys.json の cinii.appid に設定すると和文紀要の検索が有効になります。")
            _cinii_appid_warned = True
        return None
    if "cinii" in _api_tier_disabled:
        return None
    resp = _http_get_with_backoff(
        "cinii", CINII_OPENSEARCH_URL,
        params={"q": title, "format": "json", "count": 3, "appid": CINII_APPID},
        timeout=15)
    if resp.status_code in (401, 403):
        _disable_api_tier("cinii", f"HTTP {resp.status_code}（appid が無効/未承認の可能性）")
        return None
    try:
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"  [警告] CiNii 検索失敗: {e}")
        return None
    if not items:
        return None
    it = items[0]
    ctitle = it.get("title", "") or ""
    sim = _title_similarity(title, ctitle)
    if sim < TITLE_SIMILARITY_THRESHOLD:
        print(f"  [棄却] CiNiiヒットのタイトルが一致しない (類似度={sim:.2f})")
        return None
    creators = it.get("dc:creator", [])
    if isinstance(creators, str):
        creators = [creators]
    if creators:
        author = f"{creators[0]} et al." if len(creators) > 1 else creators[0]
    else:
        author = ""
    cid = it.get("@id", "") or it.get("id", "")
    m = DOI_PATTERN.search(cid)
    doi = m.group(0).strip(".,;)") if m else ""
    return {
        "title":   ctitle.strip(),
        "author":  author.strip(),
        "year":    (it.get("prism:publicationDate") or "")[:4],
        "journal": (it.get("prism:publicationName", "") or "").strip(),
        "doi":     doi.strip(),
    }


def write_pdf_metadata(pdf_path: Path, bib: dict) -> bool:
    """
    Meaning: 取得した書誌情報をPDFのメタデータフィールドに書き込む
    Action:  PdfReaderで全ページを読み込んでPdfWriterにコピーし、
             add_metadataで /Title /Author /Subject /Keywords を上書きする。
             書込はアトミック: 同じディレクトリの一時ファイル（.pdf.tmp）に書き切ってから
             os.replace() で差し替える（途中で失敗しても元PDFは無傷のまま残る）。
             バックアップ .pdf.bak は「原本」: 存在しないときだけ作成し、
             既存の .bak は再実行でも絶対に上書きしない。
    """
    tmp_path = pdf_path.with_suffix(".pdf.tmp")
    try:
        reader = PdfReader(str(pdf_path))
        writer = PdfWriter()

        # 全ページをコピー
        for page in reader.pages:
            writer.add_page(page)

        # 既存メタデータを引き継いだうえで上書き
        existing = reader.metadata or {}
        new_meta = dict(existing)
        if bib.get("title"):
            new_meta["/Title"]  = bib["title"]
        if bib.get("author"):
            new_meta["/Author"] = bib["author"]

        # DOI・年・ジャーナルは /Subject に格納する
        subject_parts = []
        if bib.get("doi"):
            subject_parts.append(f"DOI: {bib['doi']}")
        if bib.get("journal"):
            subject_parts.append(bib["journal"])
        if bib.get("year"):
            subject_parts.append(bib["year"])
        if subject_parts:
            new_meta["/Subject"] = " | ".join(subject_parts)

        if bib.get("doi"):
            new_meta["/Keywords"] = bib["doi"]

        writer.add_metadata(new_meta)

        # 一時ファイルに書き切る（同一ディレクトリ → os.replace がアトミックに働く）
        with open(tmp_path, "wb") as f:
            writer.write(f)

        # バックアップは「原本」。無いときだけ作る（既存 .bak は絶対に上書きしない）
        bak_path = pdf_path.with_suffix(".pdf.bak")
        if not bak_path.exists():
            shutil.copy2(pdf_path, bak_path)

        # アトミックに差し替える（ここまで元PDFは無傷）
        os.replace(tmp_path, pdf_path)

        print(f"  [完了] 書き込み成功: {pdf_path.name}")
        return True

    except Exception as e:
        print(f"  [エラー] メタデータ書き込み失敗: {pdf_path.name} ({e})")
        # 元PDFには一切触れていないため復旧は不要。一時ファイルだけ片付ける
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ------------------------------------------------------------------ #
# ステータスファイルの読み書き
# ------------------------------------------------------------------ #
def load_status(status_path: Path) -> dict:
    """
    Meaning: 過去の処理結果を記録したJSONファイルを読み込む
    Action:  ファイルがなければ空辞書を返す。壊れていた場合は黙ってリセットせず、
             .corrupt-<時刻> に退避してから（履歴を保全したうえで）空辞書で続行する。
    """
    if not status_path.exists():
        return {}
    try:
        with open(status_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        corrupt_path = status_path.with_name(f"{status_path.name}.corrupt-{ts}")
        try:
            os.replace(status_path, corrupt_path)
            print(f"[警告] {status_path} が壊れています ({e})。")
            print(f"       {corrupt_path.name} に退避しました（履歴は失われていません）。新規に始めます。")
        except OSError as e2:
            print(f"[警告] 壊れた {status_path} の退避に失敗: {e2}。新規に始めます。")
        return {}


def save_status(status_path: Path, status: dict) -> None:
    """
    Meaning: 処理結果をJSONファイルに永続化する
    Action:  ensure_ascii=Falseで日本語を保持し、indent=2で整形する。
             書込はアトミック: 同一ディレクトリの一時ファイルに書き切ってから os.replace()
             で差し替える（書込途中のクラッシュで全処理履歴を失わないため）。
    """
    tmp_path = status_path.with_name(status_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, status_path)


# ------------------------------------------------------------------ #
# メイン処理
# ------------------------------------------------------------------ #
def process_directory(target_dir: Path, dry_run: bool = False, only_list=None,
                      redo: bool = False) -> None:
    """
    Meaning: 指定ディレクトリ以下のPDFを走査し、メタデータ補完を実行する
    Action:  glob("**/*.pdf")で再帰的にPDFを列挙し、各ファイルを検査・処理する。
             only_list が与えられた場合は、その相対パス（target_dir 基準）に限定して処理する
             （papers 全体を再帰せず、欠損リストの 686 件だけを対象にするなどの scope 限定用）。
             リスト由来のパスは resolve + relative_to で target_dir 内に限定する（path safety）。
             dry_run=Trueの場合はAPIを叩かず書き込みも行わない（確認用）。
             redo=True のときは done* 済みファイルも再処理する（既定はスキップ）。
    """
    target_dir = target_dir.resolve()
    status_path = target_dir / STATUS_FILE_NAME
    status = load_status(status_path)

    # 重要: only_list が「空リスト」のときは "対象0件" を意味する（全 glob にフォールバックしない）。
    #       None のときだけ通常の再帰走査を行う。空リストで全走査すると暴走するため厳密に分ける。
    if only_list is not None:
        pdf_files = []
        for name in only_list:
            cand = (target_dir / name).resolve()
            # path safety: '../' 等で papers ディレクトリ外を書き込み対象にしない
            try:
                cand.relative_to(target_dir)
            except ValueError:
                print(f"[警告] 対象ディレクトリ外のパスを無視します: {name}")
                continue
            if cand.is_file():
                pdf_files.append(cand)
        pdf_files = sorted(set(pdf_files))
        print(f"[scope] --only-list 指定: {len(only_list)} 件中 {len(pdf_files)} 件を対象")
        if not pdf_files:
            print("[scope] 対象0件のため何もしません（空リスト）。")
    else:
        pdf_files = sorted(target_dir.glob("**/*.pdf"))
    print(f"\n対象ディレクトリ: {target_dir}")
    print(f"PDF総数: {len(pdf_files)} ファイル")
    print(f"ステータスファイル: {status_path}\n")

    processed = 0
    skipped   = 0
    failed    = 0
    deferred  = 0

    for pdf_path in pdf_files:
        rel = str(pdf_path.relative_to(target_dir))
        print(f"--- {rel}")

        # 既に完了済み（done / done_ocr_fallback / done_groq_proposal 等の done*）ならスキップ。
        # 再実行で書込済みファイルを重ねて処理しないための既定動作。--redo で強制再処理できる。
        # （deferred_* / failed は再処理対象のまま）
        st_now = str(status.get(rel, {}).get("status", ""))
        if st_now.startswith("done") and not redo:
            print("  [スキップ] 処理済み")
            skipped += 1
            continue

        # メタデータ検査
        current_meta = read_pdf_metadata(pdf_path)
        missing = check_missing(current_meta)

        if not missing:
            print("  [OK] メタデータ完備")
            status[rel] = {"status": "done", "note": "already_complete"}
            save_status(status_path, status)
            skipped += 1
            continue

        print(f"  [欠損] {', '.join(missing)}")

        if dry_run:
            print("  [DRY RUN] 書き込みをスキップ")
            continue

        # DOI取得: 既存メタ → ファイル名 → 本文テキスト → タイトル全文検索
        doi = current_meta.get("doi", "")
        if not doi:
            doi = extract_doi_from_filename(pdf_path)
            if doi:
                print(f"  DOIをファイル名から発見: {doi}")
        if not doi:
            print("  DOIを本文テキストから探索中...")
            doi = extract_doi_from_text(pdf_path)
            if doi:
                print(f"  DOI発見: {doi}")

        # OpenAlexで書誌情報を取得
        # 一時障害（429/5xx/接続断 = ApiTransientError）は per-file failed にせず記録し、
        # 書誌が確定できなければ後段で deferred_api として保留する
        bib = None
        api_transient = False
        if doi:
            print(f"  OpenAlex DOI検索: {doi}")
            try:
                bib = fetch_by_doi(doi)
            except ApiTransientError as e:
                print(f"  [一時障害] {e}")
                api_transient = True
            time.sleep(0.5)  # Rate limit対策

        if not bib and doi:
            print(f"  CrossRef DOI検索: {doi}")
            try:
                bib = fetch_by_doi_crossref(doi)
            except ApiTransientError as e:
                print(f"  [一時障害] {e}")
                api_transient = True
            time.sleep(0.5)

        if not bib and current_meta.get("title"):
            print(f"  OpenAlex タイトル検索: {current_meta['title'][:60]}...")
            try:
                bib = fetch_by_title(current_meta["title"])
            except ApiTransientError as e:
                print(f"  [一時障害] {e}")
                api_transient = True
            time.sleep(0.5)

        if not bib and current_meta.get("title"):
            print(f"  CrossRef タイトル検索: {current_meta['title'][:60]}...")
            try:
                bib = fetch_by_title_crossref(current_meta["title"])
            except ApiTransientError as e:
                print(f"  [一時障害] {e}")
                api_transient = True
            time.sleep(0.5)

        if not bib and current_meta.get("title"):
            print(f"  CiNii タイトル検索: {current_meta['title'][:60]}...")
            try:
                bib = fetch_by_title_cinii(current_meta["title"])
            except ApiTransientError as e:
                print(f"  [一時障害] {e}")
                api_transient = True
            time.sleep(1.0)  # CiNii polite ~1 req/s

        # 一時障害で書誌が確定できなかった場合はここで保留にする（恒久 failed にしない）。
        # ネットワーク断や 429 を「ヒットなし」と混同して、検証可能なはずの PDF に
        # LLM の未検証提案を恒久書込してしまわないための関門。
        if not bib and api_transient:
            print("  [保留] API一時障害（429/5xx/接続断）→ deferred_api（再実行で継続）")
            status[rel] = {"status": "deferred_api",
                           "note": "API一時障害（429/5xx/接続断）。再実行で継続。LLM未検証提案へはフォールバックしない。",
                           "missing": missing}
            save_status(status_path, status)
            deferred += 1
            continue

        # API検索が全滅した場合: スキャンPDFならOCRテキストをLLMに渡して書誌情報を抽出する
        # 優先順: LLM（Gemini/Groq）→ ヒューリスティックOCR抽出
        if not bib and is_scanned_pdf(pdf_path) and OCR_AVAILABLE:
            print("  API検索失敗 → OCRテキストをLLMで解析します..." if _LLM_PROVIDER
                  else "  API検索失敗 → OCRヒューリスティック抽出を試みます...")

            # まずOCRテキストを取得する（LLMにもヒューリスティックにも共通して使う）
            ocr_text = _get_ocr_text(pdf_path)

            if ocr_text and _LLM_PROVIDER:
                print(f"  LLM ({_LLM_PROVIDER}/{_LLM_MODEL}) で書誌情報を抽出中...")
                bib = extract_bib_with_llm(ocr_text)
                if bib:
                    print(f"  LLM抽出: Title='{bib['title'][:50]}' Author='{bib['author']}'")
                    print("  [注意] LLM抽出のため要確認。特にDOIは手動検証を推奨します。")

            if not bib:
                # LLMが使えないか失敗した場合はヒューリスティック抽出にフォールバック
                bib = _extract_bib_from_ocr(pdf_path)
                if bib:
                    print(f"  OCR抽出: Title='{bib['title'][:50]}' Author='{bib['author']}'")
                    print("  [注意] DOI・ジャーナル情報なし。手動確認を推奨します。")

        # 最終フォールバック: API全滅 → 本文/OCRテキストを Groq に渡して書誌を提案させ、
        # その提案をそのまま採用する（user 方針）。API未検証マーカー付きで書き込む。
        if not bib and (_GROQ_API_KEY or _LLM_PROVIDER is not None):
            txt = _get_text_for_llm(pdf_path)
            if txt and len(txt.strip()) >= 30:
                print("  API検索失敗 → LLM(無料枠ガード) に書誌を提案させます...")
                bib = llm_propose(txt)
                if bib == "QUOTA":
                    print("  [保留] LLM日次フリー枠に到達 → deferred（24時間後に自動再開）")
                    status[rel] = {"status": "deferred_quota",
                                   "note": "LLM無料枠の日次上限。resume で翌日補完。",
                                   "missing": missing}
                    save_status(status_path, status)
                    deferred += 1
                    continue
                if bib:
                    print(f"  LLM提案[{bib.get('_source')}]: Title='{bib['title'][:50]}' Author='{bib['author']}'")
                    print("  [注意] LLM提案をそのまま採用。API未検証（特にDOIは要手動確認）。")
            else:
                print("  本文テキストを取得できず（LLMフォールバック不可）")

        if not bib:
            print("  [失敗] 書誌情報を取得できませんでした")
            status[rel] = {"status": "failed", "note": "all_methods_failed", "missing": missing}
            save_status(status_path, status)
            failed += 1
            continue

        print(f"  取得: Title='{bib['title'][:50]}' Author='{bib['author']}'")

        # メタデータ書き込み
        success = write_pdf_metadata(pdf_path, bib)
        if success:
            # OCR/LLM/Groqフォールバック由来の場合は手動確認が必要なことを記録する
            src = bib.get("_source")
            if src in ("groq_text_fallback", "gemini_text_fallback"):
                status[rel] = {"status": "done_groq_proposal", "bib": bib,
                               "note": f"{src}: LLM提案をそのまま採用。API未検証。全フィールド（特にDOI）要確認。"}
            elif src in ("ocr_fallback", "llm_ocr_fallback"):
                status[rel] = {"status": "done_ocr_fallback", "bib": bib,
                               "note": "DOI未確認。タイトル・著者は要確認。"}
            else:
                status[rel] = {"status": "done", "bib": bib}
            processed += 1
        else:
            status[rel] = {"status": "failed", "note": "write_error"}
            failed += 1

        save_status(status_path, status)

    print(f"\n====== 完了 ======")
    print(f"  書き込み成功: {processed}")
    print(f"  スキップ:     {skipped}")
    print(f"  失敗:         {failed}")
    print(f"  保留(無料枠/API一時障害): {deferred}")
    print(f"  ステータス保存先: {status_path}")
    if deferred:
        print(f"  ※ {deferred} 件が保留（LLM無料枠の日次上限 or API一時障害）。")
        print(f"     再実行（無料枠の場合は24時間後）で自動補完されます。")
    # LLM 日次使用量の表示（無料枠の消費状況）
    if _llm_usage:
        usg = ", ".join(f"{p}={_llm_usage.get(p,0):,}/{LLM_LIMITS[p]['tpd']:,}tok" for p in LLM_LIMITS)
        print(f"  LLM日次使用: {usg}")


# ------------------------------------------------------------------ #
# エントリポイント
# ------------------------------------------------------------------ #
def main():
    parser = argparse.ArgumentParser(
        description="PDFのメタデータを走査し、OpenAlexから書誌情報を補完する"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="走査するディレクトリ（デフォルト: カレントディレクトリ）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="欠損PDFを表示するだけで書き込みは行わない",
    )
    parser.add_argument(
        "--keys",
        default=None,
        help="APIキー設定ファイルのパス（デフォルト: リポジトリ直下の api_keys.json。"
             "無ければ走査対象ディレクトリの api_keys.json も試す）",
    )
    parser.add_argument(
        "--only-list",
        default=None,
        help="処理対象を限定するファイル（1行1件、target_dir 基準の相対パス）。指定時は再帰走査しない。",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="done* 済み（done/done_ocr_fallback/done_groq_proposal 等）のファイルも再処理する",
    )
    args = parser.parse_args()

    target_dir = Path(args.directory).resolve()
    if not target_dir.is_dir():
        print(f"[エラー] ディレクトリが存在しません: {target_dir}")
        sys.exit(1)

    # APIキーファイルを読み込む
    # 優先順: --keys オプション → リポジトリ直下（scripts/ の1つ上）→ target_dir
    if args.keys:
        keys_path = Path(args.keys).resolve()
    else:
        script_dir = Path(__file__).parent.resolve()
        keys_path  = script_dir.parent / "api_keys.json"   # scripts/ の1つ上 = リポジトリ直下
        if not keys_path.exists():
            keys_path = target_dir / "api_keys.json"       # 走査対象ディレクトリも試す

    load_api_keys(keys_path)
    # LLM 日次トークン使用量（無料枠ガード用）は走査対象（papers）ディレクトリに永続化する
    # （ツール/スキルのインストール先ディレクトリを実行時状態で汚さない）
    load_llm_usage(target_dir / "_llm_usage.json")

    only_list = None
    if args.only_list:
        only_list = [l.strip() for l in open(args.only_list, encoding="utf-8") if l.strip()]

    process_directory(target_dir, dry_run=args.dry_run, only_list=only_list, redo=args.redo)


if __name__ == "__main__":
    main()


# ============================================================================
# Implementation self-review (per /EUmeda-coding-style Step 5)
# Change: OCR page rasterization PyMuPDF(fitz, AGPL) -> pypdfium2 (Apache/BSD)
# ----------------------------------------------------------------------------
# Smoke test results
#   - render_page_to_pil(real_pdf, page 0, 300 DPI) -> PIL Image mode="L" size=(2442,3288)
#   - image survives doc.close() (bitmap.to_pil().copy() confirmed; no use-after-free)
#   - _get_ocr_text(real_pdf): full open->render->try/finally close path runs, returns str
#   - both scripts py_compile clean; no live `fitz` symbol remains (only mapping comments)
#   - tested with pypdfium2 4.30.0
# Good points
#   - Behavior preserved literally: scale = DPI/72 (== fitz zoom); grayscale=True (== fitz.csGRAY);
#     downstream pytesseract.image_to_string(PIL "L", lang=...) unchanged.
#   - Single shared helper render_page_to_pil() centralizes the 4 former call sites (DRY, base-model).
#   - try/finally guarantees doc.close() on exception (native-memory safety).
# Bad points
#   - convert("L") from the original pseudocode was DROPPED: grayscale=True already yields mode "L",
#     and .copy() (not convert) is the explicit buffer-ownership boundary. Assumed L-mode output;
#     if a future pypdfium2 returns non-L for grayscale=True, add .convert("L") back.
# Warnings
#   - END-TO-END OCR ACCURACY NOT TESTED HERE (tesseract not installed in this env). PDFium and
#     MuPDF rasterizers are not pixel-identical; OCR *text* should be equivalent at 300 DPI but
#     MUST be confirmed by running one real scanned PDF through the full pipeline with tesseract.
#   - pypdfium2 render() defaults draw_annots=False (fitz get_pixmap rendered annotations);
#     inconsequential for body-text OCR, relevant only if text lives in annotations/stamps.
#   - pypdfium2 4.x PdfDocument has NO context-manager (`with` raises TypeError) -> try/finally used.
#     (A pseudocode reviewer suggested `with`; smoke test refuted it. Do not reintroduce `with`.)
# User judgment
#   - v1 OK for source publish (AGPL dependency removed). Confirm OCR parity on one real scanned
#     PDF with tesseract before relying on it in production.
# ============================================================================
