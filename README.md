# VRChat Status Monitor Bot (v2.0 Beta)

VRChatの **公式ステータス**（status.vrchat.com）と、Twitter(X)などの **ユーザー報告** を監視し、障害の兆候を検知したら Discord に通知するBotです。

v2.0 Beta では **エージェント型（2段階）アーキテクチャ** を採用しています。

- **Phase 1: 調査（Investigation）** … Gemini 3 Flash Preview が、公式API / Twitter検索 / Web検索を横断し、必要ならページ本文まで読み込んで調査します（Deep Dive）。
- **Phase 2: 判定（Analysis）** … Gemini 2.5 Flash が、調査レポートを元に「障害か／通知すべきか」を JSON で判定します。

> 公式発表前でも、ユーザー報告が急増している「サイレント障害」を拾いやすい設計です。

---

## 主な機能

- **自律型インテリジェント監視（2段階AI）**
  - 公式Status → Twitter → Web検索 → 本文読み込み（必要時）
  - 判定結果に応じて通知（通知が必要な時だけ）
- **ステータスの視覚化**
  - Botのプレゼンス（🟢 / ⛔️）やアクティビティで概況を表示
- **進化したテスト機能**
  - `/test_fake` … 偽データで通知・メンション・見た目を確認（避難訓練）
  - `/test_real` … 今この瞬間の情報で診断（リアルタイム調査）
- **AIチャット（メンション/DM）**
  - Botにメンション、またはDMで話しかけると、検索込みで回答

---

## 必要要件

- Python **3.10+**
- Discord Bot Token（必須）
- Google Gemini API Key（必須 / Google AI Studio）
- （任意）TwitterAPI.io の API Key（Twitter検索強化）
- （任意）Google Custom Search API Key + CX（Web検索強化）

---

## セットアップ

### 1) リポジトリ取得

```bash
git clone https://github.com/YolM1003/discord-vrc-status-bot.git
cd discord-vrc-status-bot
```

### 2) 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

> v2.0 では Deep Dive のため **beautifulsoup4** が必須です。

### 3) 環境変数（.env）設定

`.env.example` をコピーして `.env` を作成し、キーを入力してください。

```bash
cp .env.example .env
```

| 変数名                     | 説明                                     | 必須 |
| ----------------------- | -------------------------------------- | -- |
| `GEMINI_API_KEY`        | Google AI Studioで取得した Gemini APIキー     | ✅  |
| `DISCORD_BOT_TOKEN`     | Discord Developer Portalで作成したBot Token | ✅  |
| `TWITTER_API_IO_KEY`    | TwitterAPI.io のAPIキー（Twitter検索強化）      | ➖  |
| `GOOGLE_SEARCH_API_KEY` | Google Custom Search API Key（Web検索強化）  | ➖  |
| `GOOGLE_SEARCH_CX`      | Custom Search Engine ID（CX）            | ➖  |

⚠️ **注意**: `.env` には秘密鍵が含まれます。絶対に共有・コミットしないでください。

### 4) Bot起動

```bash
python discord_vrc_bot.py
```

---

## Discord側の設定（重要）

### 権限（Botが必要な権限）

- Send Messages
- Embed Links（Embed表示を使う場合）
- Read Message History（必要に応じて）

### Privileged Intent（メンション会話で必須）

このBotは **メンション/DM会話** のために `message_content` を使用します。 Discord Developer Portal で **Message Content Intent** を有効化してください。

> 監視・スラッシュコマンド中心で使うだけなら会話が不要なケースもありますが、現状の実装では Intent をTrueにしています。

---

## 使い方（最短）

1. Botを起動
2. 通知を受け取りたいチャンネルで **/register\_notify**（管理者）
   - 登録が1件以上あると、**10分ごとの定期監視**が開始されます
3. 必要ならメンション設定
   - ロール: `/add_notify_role`
   - 個人: `/subscribe_mention`
4. 任意で手動診断
   - `/vrc_status`（一般ユーザーOK）

---

## コマンド一覧（概要）

### 管理者向け

- `/register_notify` … 実行したチャンネルを通知先に登録
- `/unregister_notify` … 実行したチャンネルの登録解除
- `/add_notify_role [role]` … 障害通知時にメンションするロールを追加
- `/remove_notify_role [role]` … ロールメンション解除
- `/test_fake` … 偽データで通知テスト
- `/test_real` … 今現在の実データで診断テスト

### 一般ユーザー向け

- `/subscribe_mention` … 自分へのメンション通知をON
- `/unsubscribe_mention` … 自分へのメンション通知をOFF
- `/vrc_status` … 現在の状況をAIが診断

### 旧コマンド（互換）

- `!test_notify` … 廃止（案内表示のみ）
- `!test_fake` / `!test_real` … 旧プレフィックスコマンドとして残しています

---

## データ保存

通知先・メンション設定は、実行ディレクトリの `notify_channels.json` に保存されます。

- サーバー移設時はこのファイルを一緒に移すと設定を引き継げます
- 初期化したい場合は、Bot停止後に削除（または退避）してください

---

## トラブルシューティング

- `Error: .envファイルを確認してください ...`
  - `.env` が存在しない / 変数が空 / 起動ディレクトリが違う可能性
- スラッシュコマンドが出ない
  - Bot招待URLに `applications.commands` スコープが付いているか確認
  - `setup_hook` の `tree.sync()` 後に反映まで少し待つ場合があります
- メンション会話が反応しない
  - Developer Portal で **Message Content Intent** をON
  - サーバー側の権限（メッセージ閲覧/送信）が足りているか確認
- `Twitter API Key未設定のため検索スキップ`
  - `TWITTER_API_IO_KEY` が未設定です（任意）
- `Google Search API Key/CX 未設定のためスキップ`
  - `GOOGLE_SEARCH_API_KEY` / `GOOGLE_SEARCH_CX` が未設定です（任意）

---

## License

MIT License（`LICENSE` を参照）