"""
Cycling News Daily Digest
==========================
海外のロードバイク・MTB・グラベルバイク関連ニュースを主要英語RSSフィードから収集し、
Gemini APIで要約・日本語翻訳したうえで、指定のメールアドレスへ配信するスクリプト。
GitHub Actions から毎朝6:00 (JST) に自動実行されることを想定している。

実行方法:
    python main.py

必要な環境変数 (.env または GitHub Secrets):
    GEMINI_API_KEY  : Gemini APIキー
    EMAIL_USER      : 送信元Gmailアドレス
    EMAIL_PASS      : Gmailのアプリパスワード
    TO_EMAIL        : 送信先メールアドレス
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from time import mktime
from typing import Optional

import feedparser

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cycling_news")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    {"name": "Cyclingnews", "url": "https://www.cyclingnews.com/rss/"},
    {"name": "Velo", "url": "https://www.velo.news/feed/"},
    {"name": "Bikerumor", "url": "https://bikerumor.com/feed/"},
    {"name": "Pinkbike", "url": "https://www.pinkbike.com/pinkbike_xml_olympus.php"},
    {"name": "Cycling Weekly", "url": "https://www.cyclingweekly.com/feeds/all"},
]

LOOKBACK_HOURS = 24
GEMINI_MODEL = "gemini-flash-latest"
MAX_ARTICLES_TO_MODEL = 60  # プロンプトに含める記事数の上限（トークン節約のため）

def _clean_env(value: Optional[str]) -> Optional[str]:
    """GitHub Secrets登録時に紛れ込みやすい問題を補正する。
    - IMEが全角モードのまま入力された「＠」「．」などの全角文字を半角に変換(NFKC正規化)
    - ノーブレークスペース(\\xa0)・改行・前後の空白などを除去
    """
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", normalized)


GEMINI_API_KEY = _clean_env(os.environ.get("GEMINI_API_KEY"))
EMAIL_USER = _clean_env(os.environ.get("EMAIL_USER"))
EMAIL_PASS = _clean_env(os.environ.get("EMAIL_PASS"))
TO_EMAIL = _clean_env(os.environ.get("TO_EMAIL"))

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))


# ---------------------------------------------------------------------------
# 記事収集
# ---------------------------------------------------------------------------


@dataclass
class Article:
    source: str
    title: str
    link: str
    summary: str
    published: Optional[datetime]

    @property
    def uid(self) -> str:
        """URLを正規化してハッシュ化し、重複判定のキーに使う。"""
        normalized = self.link.split("?")[0].rstrip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def strip_html(raw_html: str) -> str:
    """RSSのdescription/summaryに含まれるHTMLタグを簡易的に除去する。"""
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_entry_datetime(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime.fromtimestamp(mktime(value), tz=timezone.utc)
    return None


def fetch_feed(feed: dict, since: datetime) -> list[Article]:
    """1つのRSSフィードから直近記事を取得する。失敗時は空リストを返し処理を継続する。"""
    articles: list[Article] = []
    try:
        parsed = feedparser.parse(feed["url"])
        if parsed.bozo and not parsed.entries:
            logger.warning(
                "フィード取得に問題がある可能性: %s (%s)", feed["name"], parsed.get("bozo_exception")
            )

        for entry in parsed.entries:
            published = parse_entry_datetime(entry)
            # 日付を取得できない記事は、フィード仕様のばらつきを考慮して除外しない
            if published is not None and published < since:
                continue

            link = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()
            if not link or not title:
                continue

            raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            summary = strip_html(raw_summary)

            articles.append(
                Article(
                    source=feed["name"],
                    title=title,
                    link=link,
                    summary=summary[:800],
                    published=published,
                )
            )
        logger.info("%s: %d件取得", feed["name"], len(articles))
    except Exception as exc:  # noqa: BLE001 - フィード単位の失敗は握りつぶして継続する
        logger.error("フィード取得エラー [%s]: %s", feed["name"], exc)
    return articles


def collect_articles() -> list[Article]:
    """全フィードから記事を収集し、24時間以内・重複除外したリストを新しい順で返す。"""
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_articles: list[Article] = []
    for feed in RSS_FEEDS:
        all_articles.extend(fetch_feed(feed, since))

    seen: set[str] = set()
    unique_articles: list[Article] = []
    for article in all_articles:
        if article.uid in seen:
            continue
        seen.add(article.uid)
        unique_articles.append(article)

    unique_articles.sort(key=lambda a: a.published or since, reverse=True)
    logger.info("重複除外後の記事数: %d件", len(unique_articles))
    return unique_articles


# ---------------------------------------------------------------------------
# Gemini APIによる要約・翻訳
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "あなたはプロのサイクリングジャーナリストです。以下の英語ニュース記事から、"
    "日本のロードバイク・MTB・グラベルバイク愛好者向けに特に重要な最新ニュースを厳選・要約し、"
    "洗練された日本語で出力してください。マイナーな話題や広告的な内容、重複するニュースは除外し、"
    "レース結果、新製品・新技術、業界動向など読者価値の高いものを優先してください。"
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "【ロード】【グラベル】【MTB】【機材・テクノロジー】などの区分",
                    },
                    "title_ja": {"type": "string", "description": "記事タイトルの日本語訳"},
                    "summary_ja": {
                        "type": "string",
                        "description": "3行程度の日本語要約。各行は改行(\\n)で区切る",
                    },
                    "url": {"type": "string", "description": "元記事のリンク"},
                },
                "required": ["category", "title_ja", "summary_ja", "url"],
            },
        }
    },
    "required": ["articles"],
}


def build_user_prompt(articles: list[Article]) -> str:
    lines = []
    for i, a in enumerate(articles[:MAX_ARTICLES_TO_MODEL], start=1):
        lines.append(
            f"{i}. [出典: {a.source}] タイトル: {a.title}\n"
            f"   概要: {a.summary}\n"
            f"   URL: {a.link}"
        )
    joined = "\n".join(lines)
    return (
        f"{joined}\n\n"
        "上記の記事の中から重要なものを厳選し、指定のJSONスキーマに沿って出力してください。"
        "summary_jaは3行程度（各行を改行\\nで区切る）とし、専門用語は分かりやすく補足してください。"
        "urlは必ず元記事のURLをそのまま使用してください（自分で生成しないこと）。"
    )


@dataclass
class SummarizedArticle:
    category: str
    title_ja: str
    summary_ja: str
    url: str


def summarize_with_gemini(articles: list[Article]) -> list[SummarizedArticle]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。")

    client = genai.Client(api_key=GEMINI_API_KEY)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=build_user_prompt(articles),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.4,
        ),
    )

    raw_text = response.text
    try:
        data = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.error("Geminiレスポンスのパースに失敗しました: %s\n生データ: %s", exc, raw_text)
        raise

    results: list[SummarizedArticle] = []
    for item in data.get("articles", []):
        results.append(
            SummarizedArticle(
                category=(item.get("category") or "").strip(),
                title_ja=(item.get("title_ja") or "").strip(),
                summary_ja=(item.get("summary_ja") or "").strip(),
                url=(item.get("url") or "").strip(),
            )
        )
    logger.info("Geminiが選定した記事数: %d件", len(results))
    return results


# ---------------------------------------------------------------------------
# メール本文の組み立て・送信
# ---------------------------------------------------------------------------


def build_email_html(summaries: list[SummarizedArticle], jst_date: str) -> str:
    if not summaries:
        body = "<p>本日は該当する新着ニュースがありませんでした。</p>"
    else:
        cards = []
        for s in summaries:
            summary_html = s.summary_ja.replace("\n", "<br>")
            cards.append(
                f"""
                <div style="border:1px solid #e2e2e2;border-radius:8px;padding:16px;margin-bottom:16px;">
                  <span style="display:inline-block;background:#2b6cb0;color:#ffffff;font-size:12px;
                        padding:2px 10px;border-radius:12px;margin-bottom:8px;">{s.category}</span>
                  <h3 style="margin:8px 0;font-size:16px;color:#1a202c;">{s.title_ja}</h3>
                  <p style="margin:0 0 10px 0;color:#4a5568;font-size:14px;line-height:1.6;">{summary_html}</p>
                  <a href="{s.url}" style="font-size:13px;color:#2b6cb0;">元記事を読む &rarr;</a>
                </div>
                """
            )
        body = "\n".join(cards)

    return f"""\
<html>
  <body style="font-family:'Hiragino Sans','Helvetica Neue',Arial,sans-serif;background:#f7f7f7;padding:20px;">
    <div style="max-width:640px;margin:0 auto;background:#ffffff;padding:24px;border-radius:10px;">
      <h1 style="font-size:20px;color:#1a202c;margin-bottom:4px;">&#128692; サイクリングニュース ダイジェスト</h1>
      <p style="color:#718096;font-size:13px;margin-top:0;">{jst_date} の海外ロードバイク・MTB・グラベル最新ニュース</p>
      {body}
      <p style="color:#a0aec0;font-size:11px;margin-top:24px;">
        本メールはRSSフィードとGemini APIによって自動生成されています。
      </p>
    </div>
  </body>
</html>
"""


def _diagnose_address(label: str, value: Optional[str]) -> None:
    """メールアドレスの値そのものはログに出さず、疑わしい特徴だけを診断ログに出す。
    公開リポジトリのActionsログにメールアドレス本体が出ないようにするため。"""
    if value is None:
        logger.info("%s diagnostic: 値が設定されていません (None)", label)
        return
    flags = []
    if "=" in value:
        flags.append("'='を含む(Secret入力時に変数名ごと貼り付けた可能性)")
    if "," in value:
        flags.append("','を含む")
    if "<" in value or ">" in value:
        flags.append("山括弧<>を含む")
    if '"' in value or "'" in value:
        flags.append("クォートを含む")
    if value.count("@") != 1:
        flags.append(f"'@'の数が{value.count('@')}個")
    non_ascii = [f"U+{ord(c):04X}" for c in value if ord(c) > 127]
    if non_ascii:
        flags.append(f"非ASCII文字あり: {non_ascii}")
    logger.info(
        "%s diagnostic: 文字数=%d 疑わしい点=%s",
        label,
        len(value),
        flags if flags else "なし",
    )


def send_email(html_body: str, subject: str) -> None:
    if not (EMAIL_USER and EMAIL_PASS and TO_EMAIL):
        raise RuntimeError("EMAIL_USER / EMAIL_PASS / TO_EMAIL が設定されていません。")

    _diagnose_address("EMAIL_USER", EMAIL_USER)
    _diagnose_address("TO_EMAIL", TO_EMAIL)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Cycling News Digest", EMAIL_USER))
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [TO_EMAIL], msg.as_string())

    logger.info("メール送信完了")


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def main() -> int:
    jst = timezone(timedelta(hours=9))
    jst_now = datetime.now(jst)
    jst_date_str = jst_now.strftime("%Y年%m月%d日")

    logger.info("=== サイクリングニュース収集開始 ===")
    articles = collect_articles()

    if not articles:
        logger.warning("新着記事が見つかりませんでした。空のダイジェストを送信します。")
        summaries: list[SummarizedArticle] = []
    else:
        summaries = summarize_with_gemini(articles)

    html = build_email_html(summaries, jst_date_str)
    subject = f"\U0001f6b4 サイクリングニュース ダイジェスト - {jst_date_str}"
    send_email(html, subject)

    logger.info("=== 処理完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
