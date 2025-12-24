# **VRChat Status Monitor Bot**

VRChatの公式ステータスとTwitter(X)上のユーザー報告をリアルタイムで監視し、障害発生時にDiscordへ通知するBotです。  
Google Gemini API (2.5 Flash/3 Flash) を活用し、単なるキーワード検知ではなく、状況の深刻度をAIがインテリジェントに分析して「本当に通知すべき障害か」を判断します。

## **🚀 主な機能**

* **インテリジェント監視**  
  * 10分ごとに「VRChat公式API」と「Twitter(X)の検索結果」を取得。  
  * **Gemini 2.5 Flash** が情報を統合分析し、以下の基準で障害判定を行います。  
    * **Major Outage**: 公式が認めている場合（即時通知）  
    * **サイレント障害**: 公式発表前でも、ユーザーからの「入れない」「落ちた」報告が急増している場合  
  * 単発のラグや個人の回線落ちは無視し、オオカミ少年化を防ぎます。  
* **視覚的なステータス表示**  
  * Botのオンラインステータス（🟢 オンライン / ⛔️ 取り込み中）を見るだけで、現在のVRChatの状況がわかります。  
  * 障害検知時は自動的にステータスメッセージが「⚠️ 障害発生中」に切り替わります。  
* **柔軟な通知設定**  
  * **チャンネル登録**: コマンド一つで任意のチャンネルを通知先に設定可能。  
  * **メンション管理**:  
    * **ロールメンション**: @VRChat部 のように、特定のロールのみに通知を飛ばせます。  
    * **個人メンション**: ユーザー個人が通知を購読（Subscribe）設定することも可能です。  
* **AIチャット機能**  
  * BotにメンションまたはDMを送ると、**Gemini 3 Flash Preview** を使用したアシスタントとして応答します。  
  * 「今のステータスは？」などの質問に対し、最新の取得情報を踏まえて回答します。

## **📦 必要要件**

* Python 3.10 以上  
* **Google Gemini API Key** (Google AI Studio)  
* **Discord Bot Token**  
* (Optional) Twitter API Key (API.IO) / Google Custom Search API Key

## **🛠 インストールと起動**

### **1\. リポジトリのクローン**
```
git clone https://github.com/YolM1003/discord-vrc-status-bot.git
cd discord-vrc-status-bot
```
### **2\. 依存ライブラリのインストール**
```
pip install -r requirements.txt
```
Note: requirements.txt がない場合は以下をインストールしてください:  
discord.py, requests, Pillow, pydantic, python-dotenv, google-genai

### **3\. 環境変数の設定**

.env.example をコピーして .env ファイルを作成し、キーを入力します。
```
cp .env.example .env
```
| 変数名 | 説明 | 必須 |
| :---- | :---- | :---- |
| GEMINI\_API\_KEY | Google AI Studioで取得したAPIキー | ✅ |
| DISCORD\_BOT\_TOKEN | Discord Developer Portalで取得したBot Token | ✅ |
| TWITTER\_API\_IO\_KEY | Twitter検索用APIキー（API.IOなど） | ➖ |
| Google Search\_API\_KEY | Google Custom Search API Key（Web検索用） | ➖ |

### **4\. Botの起動**

python discord\_vrc\_bot.py

## **📜 License**

This project is licensed under the [MIT License](https://www.google.com/search?q=LICENSE).