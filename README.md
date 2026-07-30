# Cycling News Daily Digest

海外の主要サイクリングメディア（ロードバイク・MTB・グラベルバイク）のRSSフィードを毎朝自動収集し、
Gemini APIで要約・日本語翻訳したうえで、指定のメールアドレスへHTML形式で配信するスクリプトです。
GitHub Actionsにより、毎朝6:00（JST）に完全自動で実行されます。

## 構成

```
.
├── main.py                        # メイン処理
├── requirements.txt                # 依存ライブラリ
├── .env.example                    # 環境変数のサンプル
├── .github/workflows/daily_news.yml  # 毎朝6:00 JSTに自動実行するワークフロー
└── README.md
```

## 収集元RSSフィード

- Cyclingnews
- Velo (Outside)
- Bikerumor
- Pinkbike
- Cycling Weekly

過去24時間以内に公開された記事のみを対象とし、重複記事はURLベースで除外します。
フィードの追加・削除は `main.py` 内の `RSS_FEEDS` リストを編集してください。

## セットアップ手順

### 1. リポジトリを用意する

このディレクトリの内容をGitHubリポジトリにpushしてください。

### 2. Gemini APIキーを取得する

1. [Google AI Studio](https://aistudio.google.com/apikey) にアクセスし、APIキーを発行する。
2. 発行したキーを控えておく（`GEMINI_API_KEY`）。

### 3. Gmailのアプリパスワードを発行する

1. 送信元に使うGoogleアカウントで [2段階認証](https://myaccount.google.com/security) を有効にする。
2. [アプリパスワード発行ページ](https://myaccount.google.com/apppasswords) で新しいアプリパスワードを発行する（16桁）。
3. 通常のGoogleアカウントのパスワードではなく、このアプリパスワードを `EMAIL_PASS` に設定する。

### 4. GitHub Secretsを設定する

リポジトリの `Settings > Secrets and variables > Actions` から、以下のSecretsを登録してください。

| Secret名 | 内容 |
|---|---|
| `GEMINI_API_KEY` | 手順2で取得したGemini APIキー |
| `EMAIL_USER` | 送信元Gmailアドレス |
| `EMAIL_PASS` | 手順3で発行したアプリパスワード |
| `TO_EMAIL` | 受信先メールアドレス |

### 5. 動作確認（ローカル実行、任意）

```bash
python -m venv venv
source venv/bin/activate  # Windowsの場合: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .envファイルにAPIキーやメールアドレスを記入する

python main.py
```

### 6. GitHub Actionsでの自動実行

`.github/workflows/daily_news.yml` により、UTC 21:00（JST 翌6:00）に自動実行されます。
GitHubリポジトリの `Actions` タブから `Daily Cycling News Digest` を選び、
`Run workflow` ボタンで手動実行して動作確認することもできます。

## カスタマイズ

- **要約対象記事数の上限**: `main.py` の `MAX_ARTICLES_TO_MODEL`（デフォルト60件）で、Gemini APIに渡す記事数を調整できます。
- **収集期間**: `main.py` の `LOOKBACK_HOURS`（デフォルト24時間）で変更できます。
- **Geminiモデル**: `main.py` の `GEMINI_MODEL`（デフォルト `gemini-2.5-flash`）で変更できます。
- **送信先を複数にしたい場合**: `TO_EMAIL` をカンマ区切りにして `main.py` の `send_email` 内の宛先処理を配列対応に拡張してください。

## 注意事項

- RSSフィードのURLや配信仕様は各メディア側の都合で変更されることがあります。取得件数が急に0件になった場合はURLを確認してください。
- 各メディアの利用規約の範囲内でご利用ください（要約・私的利用目的を想定しています）。
- Gemini APIには無料枠がありますが、利用量に応じて課金が発生する場合があります。[料金ページ](https://ai.google.dev/pricing) を確認してください。
