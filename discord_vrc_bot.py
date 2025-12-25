import os
import io
import json
import asyncio
import requests
import discord
from discord import app_commands
from discord.ext import tasks, commands
from PIL import Image
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# HTML Parsing for Deep Dive
from bs4 import BeautifulSoup

# Google GenAI SDK (v2025)
from google import genai
from google.genai import types

# HTTP Session & Retry logic
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------------
# 0. 環境設定 ( .env ファイルから読み込み )
# --------------------------------------------------------------------------------
load_dotenv()  # .envファイルをロード

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

TWITTER_API_IO_KEY = os.getenv("TWITTER_API_IO_KEY")
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX")

# データ保存用ファイルパス
DATA_FILE = "notify_channels.json"

# --------------------------------------------------------------------------------
# 共通 HTTPセッションの作成 (Retry & Keep-Alive)
# --------------------------------------------------------------------------------
def create_session():
    """リトライ戦略を組み込んだ共有セッションを作成"""
    session = requests.Session()
    # 合計3回のリトライ (0.5s, 1s, 2s...)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    # User-Agentを設定 (スクレイピング時のマナーとして)
    session.headers.update({
        "User-Agent": "VRChatStatusBot/2.0 (Compatible; DiscordBot)"
    })
    return session

# グローバルセッションインスタンス (TCPコネクション再利用のため)
http_client = create_session()

# --------------------------------------------------------------------------------
# 1. データ管理クラス (JSONファイルで通知先を永続化)
# --------------------------------------------------------------------------------
class ChannelManager:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = self._load_data()

    def _load_data(self) -> Dict[str, Any]:
        """データを読み込み、構造を保証して返す"""
        default_data = {"channels": [], "mentions": {}}
        
        if not os.path.exists(self.filepath):
            return default_data
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # 古いデータ形式(ただのリストやキー不足)からのマイグレーション
                if not isinstance(data, dict):
                    return default_data
                
                if "channels" not in data:
                    data["channels"] = []
                if "mentions" not in data:
                    data["mentions"] = {}
                    
                return data
        except Exception as e:
            print(f"データ読み込みエラー: {e}")
            return default_data

    def save_data(self):
        """現在のデータをJSONに保存"""
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"データ保存エラー: {e}")

    # --- チャンネル管理 ---
    def add_channel(self, channel_id: int) -> bool:
        channels = set(self.data["channels"])
        if channel_id in channels:
            return False
        channels.add(channel_id)
        self.data["channels"] = list(channels)
        self.save_data()
        return True

    def remove_channel(self, channel_id: int) -> bool:
        channels = set(self.data["channels"])
        if channel_id not in channels:
            return False
        channels.remove(channel_id)
        self.data["channels"] = list(channels)
        self.save_data()
        return True

    def get_all_channels(self) -> List[int]:
        return self.data["channels"]

    # --- メンション管理 (Guild単位) ---
    def _get_guild_mentions(self, guild_id: int) -> Dict[str, List[int]]:
        sid = str(guild_id)
        if sid not in self.data["mentions"]:
            self.data["mentions"][sid] = {"roles": [], "users": []}
        return self.data["mentions"][sid]

    def add_role_mention(self, guild_id: int, role_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        roles = set(settings["roles"])
        if role_id in roles:
            return False
        roles.add(role_id)
        settings["roles"] = list(roles)
        self.save_data()
        return True

    def remove_role_mention(self, guild_id: int, role_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        roles = set(settings["roles"])
        if role_id not in roles:
            return False
        roles.remove(role_id)
        settings["roles"] = list(roles)
        self.save_data()
        return True

    def add_user_mention(self, guild_id: int, user_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        users = set(settings["users"])
        if user_id in users:
            return False
        users.add(user_id)
        settings["users"] = list(users)
        self.save_data()
        return True

    def remove_user_mention(self, guild_id: int, user_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        users = set(settings["users"])
        if user_id not in users:
            return False
        users.remove(user_id)
        settings["users"] = list(users)
        self.save_data()
        return True

    def get_mention_string(self, guild_id: int) -> str:
        """指定されたギルドの設定に基づいてメンション文字列を生成"""
        sid = str(guild_id)
        if sid not in self.data["mentions"]:
            return ""
        
        settings = self.data["mentions"][sid]
        mentions = []
        
        # ロールメンション
        for rid in settings.get("roles", []):
            mentions.append(f"<@&{rid}>")
            
        # ユーザーメンション
        for uid in settings.get("users", []):
            mentions.append(f"<@{uid}>")
            
        if not mentions:
            return ""
            
        return " ".join(mentions)

# --------------------------------------------------------------------------------
# 2. データ構造定義 (Pydantic)
# --------------------------------------------------------------------------------
class OutageAnalysis(BaseModel):
    is_outage: bool = Field(description="障害が発生している、または発生している可能性が高い場合はTrue。")
    severity: str = Field(description="深刻度 (例: 'なし', '軽微', '接続不可', '公式発表あり')")
    should_notify: bool = Field(description="ユーザーに通知を送るべき状況かどうか。")
    notification_message: str = Field(description="通知用のメッセージ本文。VRChatユーザーに寄り添った表現で。通知不要なら空文字。")

# --------------------------------------------------------------------------------
# 3. 情報収集ツール群
# --------------------------------------------------------------------------------
def get_vrc_status_data() -> str:
    """公式ステータス情報を取得"""
    url = "https://status.vrchat.com/api/v2/status.json"
    try:
        # 共有セッションを使用
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", {})
        return f"公式Status: {status.get('indicator', 'unknown')} - {status.get('description', '')}"
    except Exception as e:
        return f"公式Status取得エラー: {e}"

def get_twitter_data(query: str = "VRChat", limit: int = 10) -> str:
    """Twitter検索結果を取得 (TwitterAPI.io)
    AIは 'query' に検索演算子を含めることができる (例: 'VRChat lang:ja min_faves:5')
    """
    if not TWITTER_API_IO_KEY:
        return "Twitter API Key未設定のため検索スキップ"
    
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_IO_KEY}
    
    # AIの自由度を高めるため、強制フィルタは必要最低限にする。
    # ただし、コスト節約のため、limitの上限は設ける
    safe_limit = min(limit, 20) 

    params = {
        "query": query,
        "queryType": "Latest", # 最新順を保証
        "limit": safe_limit
    }
    
    try:
        # 共有セッションを使用 (リトライ・コネクションプーリング)
        resp = http_client.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tweets = data.get("tweets", [])
        
        if not tweets:
            return "関連ツイートなし"
            
        result_text = f"Twitter検索結果 (Latest, limit={safe_limit}):\n"
        for t in tweets:
            # レスポンス構造の揺れに対応
            user_obj = t.get("author") or t.get("user") or {}
            user = user_obj.get("userName") or user_obj.get("username") or "unknown"
            
            text = t.get("text", "")
            created_at = t.get("createdAt", t.get("created_at", ""))
            result_text += f"- @{user} ({created_at}): {text}\n"
        return result_text
    except Exception as e:
        return f"Twitter検索エラー: {e}"

def search_web_data(query: str, num_results: int = 3) -> str:
    """Web検索結果を取得 (Google Custom Search API)"""
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX:
        return "Google Search API Key/CX 未設定のためスキップ"

    url = "https://www.googleapis.com/customsearch/v1"
    
    safe_num = min(num_results, 5)

    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_SEARCH_CX,
        "q": query,
        "num": safe_num
    }
    try:
        # 共有セッションを使用
        resp = http_client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        items = data.get("items", [])
        if not items:
            return "Web検索結果: 関連情報なし"

        result_text = f"Web検索結果 (Google Custom Search, num={safe_num}):\n"
        for item in items:
            title = item.get('title', 'No Title')
            link = item.get('link', 'No Link')
            snippet = item.get('snippet', '')
            result_text += f"Title: {title}\nURL: {link}\nSnippet: {snippet}\n---\n"
        return result_text
    except Exception as e:
        return f"Web検索エラー: {e}"

def fetch_url_content(url: str) -> str:
    """[Deep Dive] 指定されたURLのWebページ本文を取得する"""
    try:
        # User-Agentはセッションで設定済み
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        
        # HTML解析
        soup = BeautifulSoup(resp.content, "html.parser")
        
        # 不要なタグを除去 (スクリプト、スタイル、ナビゲーション等はノイズ)
        for script in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            script.decompose()
            
        # テキスト抽出
        text = soup.get_text(separator="\n")
        
        # 空行削除と整形
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = '\n'.join(chunk for chunk in chunks if chunk)
        
        # 文字数制限 (長すぎるとGeminiのトークンを消費しすぎるため、先頭5000文字程度に制限)
        if len(clean_text) > 5000:
            clean_text = clean_text[:5000] + "\n...(以下省略)"
            
        return f"URL: {url}\n\n[Page Content]\n{clean_text}"
    except Exception as e:
        return f"URL取得エラー ({url}): {e}"

# --------------------------------------------------------------------------------
# 4. Gemini クライアントラッパー (Tool Use対応 + 2段階分析)
# --------------------------------------------------------------------------------
class GeminiHandler:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        # 調査担当 (Tool Useが得意)
        self.investigator_model_id = "gemini-3-flash-preview"
        # 分析・チャット担当
        self.chat_model_id = "gemini-3-flash-preview"
        # 判定担当 (JSON出力が得意)
        self.judge_model_id = "gemini-2.5-flash"
        
        self.chat_sessions: Dict[int, object] = {}

        # ツール定義
        self.tools = [
            get_vrc_status_data,
            get_twitter_data,
            search_web_data,
            fetch_url_content
        ]
        # ツール実行用のマッピング
        self.tool_map = {
            "get_vrc_status_data": get_vrc_status_data,
            "get_twitter_data": get_twitter_data,
            "search_web_data": search_web_data,
            "fetch_url_content": fetch_url_content
        }

    # --- チャット用セッション管理 ---
    def get_session(self, channel_id: int):
        if channel_id not in self.chat_sessions:
            system_instruction = (
                "あなたはVRChatコミュニティをサポートする頼れるアシスタントAIです。\n"
                "ユーザーからの質問には正確かつ最新の情報で答えてください。\n\n"
                "**ツール利用のガイドライン:**\n"
                "1. **調査プロセス**: まず「検索(search_web_data/get_twitter_data)」を行い、結果のスニペットを確認します。\n"
                "2. **深掘り(Deep Dive)**: 検索結果の中で重要そうなURLがあれば、必ず `fetch_url_content` を使って**ページの中身**を確認してください。\n"
                "3. **反復改善**: 情報が不足していれば、検索クエリを変えて再度検索を行ってください。\n"
                "4. **Twitter検索**: クエリには適宜フィルタを含めてください (例: 'VRChat lang:ja -filter:retweets')。"
            )
            self.chat_sessions[channel_id] = self.client.chats.create(
                model=self.chat_model_id,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.7,
                    tools=self.tools
                )
            )
        return self.chat_sessions[channel_id]

    def _execute_tool_loop(self, chat_session, contents, max_turns=5) -> str:
        """ツール実行ループの共通ロジック"""
        try:
            response = chat_session.send_message(contents)
            current_turn = 0
            
            while response.function_calls and current_turn < max_turns:
                current_turn += 1
                function_calls = response.function_calls
                function_responses = []

                print(f"🔄 Turn {current_turn}: Processing {len(function_calls)} tool calls...")

                for call in function_calls:
                    func_name = call.name
                    func_args = call.args
                    
                    if func_name in self.tool_map:
                        print(f"  🛠️ Call: {func_name} args={func_args}")
                        try:
                            result = self.tool_map[func_name](**func_args)
                        except Exception as e:
                            result = f"Error executing {func_name}: {e}"
                        
                        function_responses.append(
                            types.FunctionResponse(
                                name=func_name,
                                response={"result": result}
                            )
                        )
                
                if function_responses:
                    response = chat_session.send_message(
                        [types.Part.from_function_response(item) for item in function_responses]
                    )
                else:
                    break
            return response.text
        except Exception as e:
            return f"Error in tool loop: {str(e)}"

    def generate_chat_response(self, channel_id: int, text: str, image_bytes: Optional[bytes] = None, status_context: str = "") -> str:
        """[チャット用] ユーザーの入力に対して応答を生成する"""
        chat = self.get_session(channel_id)
        
        prompt_content = text
        if status_context:
            prompt_content = f"{text}\n\n[参考情報]\n{status_context}"

        contents = [prompt_content]
        if image_bytes:
            try: contents.append(Image.open(io.BytesIO(image_bytes)))
            except: pass
        
        return self._execute_tool_loop(chat, contents)

    def investigate_server_status(self) -> str:
        """[監視用・フェーズ1] 自律的に調査を行い、状況レポートを作成する"""
        system_instruction = (
            "あなたはVRChatサーバー監視の専任調査員です。\n"
            "現在のVRChatの稼働状況を徹底的に調査し、障害の有無や兆候をレポートにまとめてください。\n\n"
            "**調査手順:**\n"
            "1. まず `get_vrc_status_data` で公式発表を確認。\n"
            "2. 次に `get_twitter_data` でユーザーの生の反応を確認。検索クエリは自分で考えて工夫すること。\n"
            "   - 例: 公式が静かでも、ユーザーが「入れない」「重い」と騒いでいればサイレント障害の可能性があります。\n"
            "   - 検索例: 'VRChat (落ちた OR 重い OR ログイン) lang:ja -filter:retweets'\n"
            "3. 必要であれば `search_web_data` や `fetch_url_content` で外部ニュースやRedditなども確認。\n"
            "4. 最終的に、「調査結果の要約」を出力してください。"
        )
        
        # 監視用の使い捨てチャットセッションを作成 (調査モードはGemini 3 Flash推奨)
        chat = self.client.chats.create(
            model=self.investigator_model_id,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.5, # 調査なので創造性より正確性重視
                tools=self.tools
            )
        )
        
        prompt = "現在のVRChatの状況を調査してください。何か異常はありますか？"
        return self._execute_tool_loop(chat, [prompt], max_turns=5)

    def analyze_situation_report(self, investigation_report: str) -> OutageAnalysis:
        """[監視用・フェーズ2] 調査レポートを読み込み、障害判定JSONを出力する"""
        prompt = (
            "以下の調査レポートに基づき、VRChatで現在障害が発生しているか、ユーザーに通知すべきかを判断してください。\n\n"
            f"=== 調査レポート ===\n{investigation_report}\n\n"
            "判断基準:\n"
            "1. 公式がMajor Outage等を認めている場合は即座に通知対象。\n"
            "2. 公式が正常でも、Twitter等で「入れない」「落ちた」報告が多発している場合は「サイレント障害」として通知対象。\n"
            "3. 単発の「重い」程度なら通知不要。\n"
            "思考プロセスを経て、JSONで出力してください。"
        )

        try:
            # 判定はフォーマット重視なのでGemini 2.5 Flashを使用
            response = self.client.models.generate_content(
                model=self.judge_model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=OutageAnalysis,
                    thinking_config=types.ThinkingConfig(
                        include_thoughts=True,
                        thinking_budget=1024
                    )
                )
            )
            return response.parsed
        except Exception as e:
            print(f"分析失敗: {e}")
            return OutageAnalysis(is_outage=False, severity="Unknown", should_notify=False, notification_message="")

# --------------------------------------------------------------------------------
# 5. Discord Bot Implementation
# --------------------------------------------------------------------------------
class VRChatStatusBot(commands.Bot):
    def __init__(self):
        # 権限設定
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.guilds = True
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.gemini = GeminiHandler(GEMINI_API_KEY)
        self.channel_manager = ChannelManager(DATA_FILE)
        self.last_notification_sent = False

    async def setup_hook(self):
        await self.tree.sync()
        if self.channel_manager.get_all_channels():
            self.monitor_task.start()
            print("🔄 監視タスクを開始しました")
        else:
            print("⚠️ 通知先が登録されていません。監視タスクは待機中です。")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print(f'登録済みチャンネル数: {len(self.channel_manager.get_all_channels())}')
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="VRChatステータス"))

    # ------------------------------------------------------------------
    # 監視ループ (10分ごと) -> 2段階AI処理へ変更
    # ------------------------------------------------------------------
    @tasks.loop(minutes=10)
    async def monitor_task(self):
        target_channels = self.channel_manager.get_all_channels()
        if not target_channels: return

        print(f"🔍 定期チェック実行中... (対象: {len(target_channels)}チャンネル)")
        loop = asyncio.get_running_loop()
        
        # --- Phase 1: AIによる自律調査 (Gemini 3 Flash + Tool Use) ---
        print("  Running Phase 1: Investigation...")
        investigation_report = await loop.run_in_executor(None, self.gemini.investigate_server_status)
        
        # --- Phase 2: 障害判定 (Gemini 2.5 Flash + JSON Mode) ---
        print("  Running Phase 2: Analysis...")
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, investigation_report)

        # === Botステータスの自動更新 ===
        if analysis.is_outage:
            await self.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(type=discord.ActivityType.watching, name=f"⚠️ 障害発生中 ({analysis.severity})")
            )
        else:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching, name="VRChat: 正常稼働中")
            )

        # === 通知判定 ===
        if analysis.should_notify:
            if not self.last_notification_sent:
                print(f"⚠️ 障害検知! 通知送信。深刻度: {analysis.severity}")
                await self.broadcast_message(f"**⚠️ VRChat 障害検知レポート**\n{analysis.notification_message}")
                self.last_notification_sent = True
        else:
            if self.last_notification_sent:
                print("✅ 復旧検知")
                await self.broadcast_message("✅ **復旧報告**: 障害状況は解消されたようです。")
                self.last_notification_sent = False

    async def broadcast_message(self, content: str):
        target_channels = self.channel_manager.get_all_channels()
        for ch_id in target_channels:
            channel = self.get_channel(ch_id)
            if channel:
                try:
                    mention_str = ""
                    if hasattr(channel, 'guild') and channel.guild:
                        mention_str = self.channel_manager.get_mention_string(channel.guild.id)
                    
                    final_content = content
                    if mention_str:
                        final_content = f"{content}\n\n{mention_str}"
                    await channel.send(final_content)
                except Exception as e:
                    print(f"送信失敗({ch_id}): {e}")

    @monitor_task.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    # ------------------------------------------------------------------
    # テスト用ロジック (Fake & Real)
    # ------------------------------------------------------------------
    async def run_fake_diagnosis_test(self, responder):
        """[TEST] 偽データを使った動作テスト"""
        # Phase 1の調査結果を模した偽レポートを作成
        fake_report = (
            "【調査レポート (TEST/FAKE)】\n"
            "1. 公式ステータス: Major Outage - We are investigating login issues.\n"
            "2. Twitter状況: 「VRChat入れない」「鯖落ち」というツイートが毎分10件以上急増中。\n"
            "3. Web情報: 公式Statusページで赤色の警告を確認。\n"
            "結論: 明らかに障害が発生しています。"
        )
        
        loop = asyncio.get_running_loop()
        
        # テスト開始メッセージ
        msg_start = "🧪 **[TEST: FAKE] 偽データによる動作確認を開始します...**"
        if isinstance(responder, discord.Interaction):
            await responder.followup.send(msg_start)
        else:
            await responder.send(msg_start)

        # Phase 2 (判定) だけをAIに実行させる
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, fake_report)
        
        await self._send_test_result(responder, analysis, fake_report, title="🧪 診断テスト結果 (偽データ)")

    async def run_realtime_diagnosis_test(self, responder):
        """[TEST] 今現在の情報を元にしたリアルタイム診断テスト"""
        loop = asyncio.get_running_loop()
        
        # テスト開始メッセージ
        msg_start = "🕵️ **[TEST: REAL] 現在のリアルタイム調査を実行します...**\n(Gemini 3 Flashによる自律調査 + Gemini 2.5 Flashによる判定)"
        if isinstance(responder, discord.Interaction):
            await responder.followup.send(msg_start)
        else:
            await responder.send(msg_start)

        # Phase 1: 調査
        report = await loop.run_in_executor(None, self.gemini.investigate_server_status)
        
        # Phase 2: 判定
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, report)
        
        await self._send_test_result(responder, analysis, report, title="📊 リアルタイム診断結果")

    async def _send_test_result(self, responder, analysis: OutageAnalysis, report_text: str, title: str):
        """テスト結果のEmbedを作成して送信する共通メソッド"""
        color = discord.Color.red() if analysis.is_outage else discord.Color.green()
        embed = discord.Embed(title=title, color=color)
        
        embed.add_field(name="判定", value=f"**{'⚠️ 障害の可能性あり' if analysis.is_outage else '✅ おそらく正常'}**", inline=False)
        embed.add_field(name="AIコメント", value=analysis.notification_message or "異常なし", inline=False)
        
        # 文字数制限対策 (1000文字でカット)
        truncated_report = report_text
        if len(truncated_report) > 1000:
            truncated_report = truncated_report[:1000] + "\n...(以下省略)"
            
        embed.add_field(name="調査レポート要約", value=truncated_report, inline=False)
        
        # メンション設定の確認
        if hasattr(responder, 'guild') and responder.guild:
            mention_str = self.channel_manager.get_mention_string(responder.guild.id)
            if mention_str:
                embed.add_field(name="メンション設定", value=mention_str, inline=False)

        if isinstance(responder, discord.Interaction):
            await responder.followup.send(embed=embed)
        else:
            await responder.send(embed=embed)

    # ------------------------------------------------------------------
    # 通常会話 (メンション応答) & 旧コマンドサポート
    # ------------------------------------------------------------------
    async def on_message(self, message):
        if message.author == self.user: return

        if message.content == "!test_notify":
            await message.channel.send("⚠️ `!test_notify` は廃止されました。`/test_fake` または `/test_real` を使用してください。")
            return
        
        if message.content == "!test_fake":
             await message.channel.send("🧪 テストモード(FAKE)起動...")
             await self.run_fake_diagnosis_test(message.channel)
             return

        if message.content == "!test_real":
             await message.channel.send("🕵️ テストモード(REAL)起動...")
             await self.run_realtime_diagnosis_test(message.channel)
             return

        is_mentioned = self.user in message.mentions or isinstance(message.channel, discord.DMChannel)
        if is_mentioned:
            async with message.channel.typing():
                user_text = message.content.replace(f'<@!{self.user.id}>', '').replace(f'<@{self.user.id}>', '').strip()
                image_bytes = None
                if message.attachments:
                    try: image_bytes = await message.attachments[0].read()
                    except: pass

                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None, 
                    self.gemini.generate_chat_response, 
                    message.channel.id, 
                    user_text, 
                    image_bytes
                )
                
                # --- 改行を優先したスマートな分割送信ロジック (2000文字対策) ---
                if len(response) > 2000:
                    chunks = []
                    while response:
                        if len(response) <= 2000:
                            chunks.append(response)
                            break
                        
                        # 2000文字以内で、最も後ろにある改行コードを探す
                        split_index = response.rfind('\n', 0, 2000)
                        
                        if split_index == -1:
                            # 改行がない場合は、やむを得ず2000文字で強制分割
                            split_index = 2000
                        else:
                            # 改行文字の後ろで分割する
                            split_index += 1
                        
                        chunks.append(response[:split_index])
                        response = response[split_index:]
                    
                    for chunk in chunks:
                        await message.reply(chunk)
                else:
                    await message.reply(response)

# --------------------------------------------------------------------------------
# Main Entry Point
# --------------------------------------------------------------------------------
if __name__ == "__main__":
    if not GEMINI_API_KEY or not DISCORD_BOT_TOKEN:
        print("Error: .envファイルを確認してください (GEMINI_API_KEY, DISCORD_BOT_TOKEN)")
        exit(1)

    bot = VRChatStatusBot()
    # (省略なし: スラッシュコマンドは全て前回のコードと同様に定義)
    
    @bot.tree.command(name="register_notify", description="[管理者用] このチャンネルをVRChat障害通知の宛先に登録します")
    @app_commands.checks.has_permissions(administrator=True)
    async def register_notify(interaction: discord.Interaction):
        success = bot.channel_manager.add_channel(interaction.channel_id)
        if success:
            await interaction.response.send_message(f"✅ このチャンネル ({interaction.channel.mention}) を通知先に登録しました！")
            if not bot.monitor_task.is_running():
                bot.monitor_task.start()
        else:
            await interaction.response.send_message("ℹ️ このチャンネルは既に登録されています。")

    @bot.tree.command(name="unregister_notify", description="[管理者用] このチャンネルのVRChat障害通知を解除します")
    @app_commands.checks.has_permissions(administrator=True)
    async def unregister_notify(interaction: discord.Interaction):
        success = bot.channel_manager.remove_channel(interaction.channel_id)
        if success:
            await interaction.response.send_message("👋 通知を解除しました。")
        else:
            await interaction.response.send_message("ℹ️ このチャンネルは登録されていません。")

    @bot.tree.command(name="add_notify_role", description="[管理者用] 障害通知時にメンションするロールを追加します")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_notify_role(interaction: discord.Interaction, role: discord.Role):
        success = bot.channel_manager.add_role_mention(interaction.guild.id, role.id)
        msg = f"✅ ロール {role.mention} を通知対象に追加しました。" if success else "ℹ️ 既に登録されています。"
        await interaction.response.send_message(msg)

    @bot.tree.command(name="remove_notify_role", description="[管理者用] 障害通知時のロールメンションを解除します")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_notify_role(interaction: discord.Interaction, role: discord.Role):
        success = bot.channel_manager.remove_role_mention(interaction.guild.id, role.id)
        msg = f"👋 ロール {role.mention} の通知を解除しました。" if success else "ℹ️ 登録されていません。"
        await interaction.response.send_message(msg)

    @bot.tree.command(name="subscribe_mention", description="障害通知時に自分へのメンションを受け取る設定にします")
    async def subscribe_mention(interaction: discord.Interaction):
        success = bot.channel_manager.add_user_mention(interaction.guild.id, interaction.user.id)
        msg = f"✅ {interaction.user.mention} さんを通知リストに追加しました！" if success else "ℹ️ 既に登録済みです。"
        await interaction.response.send_message(msg)

    @bot.tree.command(name="unsubscribe_mention", description="障害通知時の自分へのメンションを解除します")
    async def unsubscribe_mention(interaction: discord.Interaction):
        success = bot.channel_manager.remove_user_mention(interaction.guild.id, interaction.user.id)
        msg = f"👋 {interaction.user.mention} さんの通知を解除しました。" if success else "ℹ️ 登録されていません。"
        await interaction.response.send_message(msg)

    @bot.tree.command(name="vrc_status", description="現在のVRChatサーバーステータスをAIが診断します")
    async def vrc_status(interaction: discord.Interaction):
        await interaction.response.defer()
        # vrc_status コマンドもリアルタイム診断ロジックに統合
        await bot.run_realtime_diagnosis_test(interaction)

    @bot.tree.command(name="test_fake", description="[管理者用] 偽データで通知テストを行います")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_fake(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await bot.run_fake_diagnosis_test(interaction)

    @bot.tree.command(name="test_real", description="[管理者用] 現在の実データで通知テストを行います")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_real(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await bot.run_realtime_diagnosis_test(interaction)

    # 従来のコマンドのエイリアスとして残す場合（混乱を避けるため廃止推奨だが、念の為メッセージを出す）
    @bot.tree.command(name="test_notify", description="[廃止] /test_fake または /test_real を使用してください")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_notify_legacy(interaction: discord.Interaction):
        await interaction.response.send_message("⚠️ `/test_notify` は廃止されました。偽データテストは `/test_fake`、リアルタイムテストは `/test_real` を使用してください。", ephemeral=True)

    bot.run(DISCORD_BOT_TOKEN)