import os
import io
import json
import asyncio
import requests
import discord
from discord import app_commands
from discord.ext import tasks, commands
from PIL import Image
from typing import Optional, Dict, List, Set, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Google GenAI SDK
from google import genai
from google.genai import types

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
# 3. 情報収集関数
# --------------------------------------------------------------------------------
def get_vrc_status_data() -> str:
    """公式ステータス情報を取得"""
    url = "https://status.vrchat.com/api/v2/status.json"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", {})
        return f"公式Status: {status.get('indicator', 'unknown')} - {status.get('description', '')}"
    except Exception as e:
        return f"公式Status取得エラー: {e}"

def get_twitter_data(query: str = "VRChat") -> str:
    """Twitter検索結果を取得"""
    if not TWITTER_API_IO_KEY:
        return "Twitter API Key未設定のため検索スキップ"
    
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_IO_KEY}
    params = {"query": query, "search_type": "Latest"}
    
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tweets = data.get("tweets", [])
        
        if not tweets:
            return "関連ツイートなし"
            
        result_text = "Twitter検索結果:\n"
        for t in tweets[:5]:
            user = t.get("user", {}).get("username", "unknown")
            text = t.get("text", "")
            created_at = t.get("created_at", "")
            result_text += f"- @{user} ({created_at}): {text}\n"
        return result_text
    except Exception as e:
        return f"Twitter検索エラー: {e}"

def search_web_data(query: str) -> str:
    """Web検索結果を取得 (Google Custom Search)"""
    if not GOOGLE_SEARCH_API_KEY:
        return "Google Search API Key未設定のためスキップ"

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_SEARCH_CX,
        "q": query,
        "num": 3 
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        result_text = "Web検索結果:\n"
        for item in data.get("items", []):
            result_text += f"- {item.get('title')}: {item.get('snippet')}\n"
        return result_text
    except Exception as e:
        return f"Web検索エラー: {e}"

# --------------------------------------------------------------------------------
# 4. Gemini クライアントラッパー
# --------------------------------------------------------------------------------
class GeminiHandler:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.chat_model_id = "gemini-3-flash-preview"
        self.analyze_model_id = "gemini-2.5-flash"
        self.chat_sessions: Dict[int, object] = {}

    def get_session(self, channel_id: int):
        if channel_id not in self.chat_sessions:
            system_instruction = (
                "あなたはVRChatコミュニティをサポートする頼れるアシスタントAIです。\n"
                "名前は特にありませんが、丁寧かつ親しみやすい口調で話します。\n"
                "ユーザーからのVRChatに関する質問や雑談に答えてください。\n"
                "もしシステムステータスなどの情報が提供された場合は、それを最優先事実として扱ってください。"
            )
            self.chat_sessions[channel_id] = self.client.chats.create(
                model=self.chat_model_id,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.7,
                )
            )
        return self.chat_sessions[channel_id]

    def generate_chat_response(self, channel_id: int, text: str, image_bytes: Optional[bytes] = None, status_context: str = "") -> str:
        chat = self.get_session(channel_id)
        
        prompt_content = text
        if status_context:
            prompt_content = (
                f"【システム情報（最優先）】\n{status_context}\n"
                f"--------------------------------------------------\n"
                f"【ユーザーの発言】\n{text}\n"
                f"--------------------------------------------------\n"
                f"回答指示: 上記のシステム情報を踏まえて、ユーザーの疑問や不安に答えてください。"
            )

        contents = [prompt_content]
        if image_bytes:
            try:
                contents.append(Image.open(io.BytesIO(image_bytes)))
            except: pass
        
        try:
            response = chat.send_message(contents)
            return response.text
        except Exception as e:
            return f"AI応答エラー: {str(e)}"

    def analyze_situation(self, official_info: str, twitter_info: str) -> OutageAnalysis:
        prompt = (
            "以下の情報を分析し、VRChatで現在障害が発生しているか、ユーザーに通知すべきかを判断してください。\n\n"
            f"=== 公式ステータス情報 ===\n{official_info}\n\n"
            f"=== Twitter (X) ユーザーの声 ===\n{twitter_info}\n\n"
            "判断基準:\n"
            "1. 公式がMajor Outage等を認めている場合は即座に通知対象。\n"
            "2. 公式が正常でも、直近10分以内のツイートで「入れない」「落ちた」報告が複数ある場合は「サイレント障害」として通知対象。\n"
            "3. 単発の「重い」程度なら通知不要。\n"
            "思考プロセスを経て、JSONで出力してください。"
        )

        try:
            response = self.client.models.generate_content(
                model=self.analyze_model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=OutageAnalysis,
                    thinking_config=types.ThinkingConfig(
                        include_thoughts=True,
                        thinking_budget=2048
                    )
                )
            )
            return response.parsed
        except Exception as e:
            print(f"分析失敗: {e}")
            return OutageAnalysis(is_outage=False, severity="Unknown", should_notify=False, notification_message="")

# --------------------------------------------------------------------------------
# 5. Discord Bot with Slash Commands
# --------------------------------------------------------------------------------
class VRChatStatusBot(commands.Bot):
    def __init__(self):
        # 権限設定
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.guilds = True # ギルド情報の取得に必要
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.gemini = GeminiHandler(GEMINI_API_KEY)
        self.channel_manager = ChannelManager(DATA_FILE)
        self.last_notification_sent = False

    async def setup_hook(self):
        # スラッシュコマンド同期
        await self.tree.sync()
        
        # 起動時に監視タスクを開始
        if self.channel_manager.get_all_channels():
            self.monitor_task.start()
            print("🔄 監視タスクを開始しました")
        else:
            print("⚠️ 通知先が登録されていません。監視タスクは待機中です。")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print(f'登録済みチャンネル数: {len(self.channel_manager.get_all_channels())}')
        # 初期ステータス設定
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="VRChatステータス"))

    # ------------------------------------------------------------------
    # 監視ループ (10分ごと)
    # ------------------------------------------------------------------
    @tasks.loop(minutes=10)
    async def monitor_task(self):
        target_channels = self.channel_manager.get_all_channels()
        if not target_channels:
            return

        print(f"🔍 定期チェック実行中... (対象: {len(target_channels)}チャンネル)")
        
        loop = asyncio.get_running_loop()
        official_data = await loop.run_in_executor(None, get_vrc_status_data)
        twitter_data = await loop.run_in_executor(None, lambda: get_twitter_data("VRChat (落ちた OR 重い OR 鯖落ち)"))
        
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation, official_data, twitter_data)

        # === Botステータスの自動更新 (ここを追加) ===
        if analysis.is_outage:
            # 障害時: 赤(取り込み中)で警告表示
            await self.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(type=discord.ActivityType.watching, name=f"⚠️ 障害発生中 ({analysis.severity})")
            )
        else:
            # 正常時: 緑(オンライン)で通常表示
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching, name="VRChat: 正常稼働中")
            )
        # ============================================

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
        """登録されている全チャンネルにメッセージを送信（メンション対応）"""
        target_channels = self.channel_manager.get_all_channels()
        for ch_id in target_channels:
            channel = self.get_channel(ch_id)
            if channel:
                try:
                    # メンション文字列を生成
                    mention_str = ""
                    # チャンネルがギルドに属している場合のみメンション設定を確認
                    if hasattr(channel, 'guild') and channel.guild:
                        mention_str = self.channel_manager.get_mention_string(channel.guild.id)
                    
                    # メッセージ本文にメンションを追記
                    final_content = content
                    if mention_str:
                        final_content = f"{content}\n\n{mention_str}"

                    await channel.send(final_content)

                except discord.Forbidden:
                    print(f"送信失敗(権限なし): {ch_id}")
                except Exception as e:
                    print(f"送信失敗({ch_id}): {e}")

    @monitor_task.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    # ------------------------------------------------------------------
    # テスト用ロジック (Slash & Message共通)
    # ------------------------------------------------------------------
    async def run_test_diagnosis(self, responder):
        """偽データを作成し、AIに分析させて結果を送信する共通処理"""
        # 偽の障害データを作成 (AIが「障害」と判定しそうな内容)
        fake_official = "公式Status: major_outage - We are investigating login issues."
        fake_twitter = (
            "Twitter検索結果:\n"
            "- @userA (1分前): VRChat入れないんだけど、鯖落ち？\n"
            "- @userB (2分前): ログインできん。Loadingで止まる。\n"
            "- @userC (3分前): 落ちたー！みんな落ちてるっぽい。\n"
        )
        
        loop = asyncio.get_running_loop()
        # ★ここが重要: 実際にAIに推論させる
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation, fake_official, fake_twitter)
        
        if analysis.should_notify:
            msg = f"**[TEST] 🧪 AI診断テスト: 障害検知成功**\nAI判定: {analysis.severity}\n\n{analysis.notification_message}"
            
            # テスト時も、そのチャンネルが属するサーバーのメンション設定を付加して確認できるようにする
            if hasattr(responder, 'guild') and responder.guild:
                 mention_str = self.channel_manager.get_mention_string(responder.guild.id)
                 if mention_str:
                     msg += f"\n\n(メンションテスト: {mention_str})"

            # responderがInteractionならfollowup, Contextならsend
            if isinstance(responder, discord.Interaction):
                await responder.followup.send(msg)
            else:
                await responder.send(msg)
        else:
            msg = f"**[TEST] ❓ AI診断テスト: 検知失敗（正常判定）**\nAIはこれを障害と判定しませんでした。\n理由: {analysis.severity}"
            if isinstance(responder, discord.Interaction):
                await responder.followup.send(msg)
            else:
                await responder.send(msg)

    # ------------------------------------------------------------------
    # 通常会話 (メンション応答) & 旧コマンドサポート
    # ------------------------------------------------------------------
    async def on_message(self, message):
        if message.author == self.user: return

        # === [C:V1仕様] 旧テストコマンド (!test_notify) ===
        if message.content == "!test_notify":
            await message.channel.send("🧪 テストモード起動 (Legacy Command)... AIによる分析を実行中...")
            await self.run_test_diagnosis(message.channel)
            return
        
        # メンション応答
        is_mentioned = self.user in message.mentions or isinstance(message.channel, discord.DMChannel)
        if is_mentioned:
            async with message.channel.typing():
                user_text = message.content.replace(f'<@!{self.user.id}>', '').replace(f'<@{self.user.id}>', '').strip()
                
                # 特定キーワードで状況確認モードへ
                keywords = ["状況", "ステータス", "status", "重い", "落ち", "入れない", "ログイン", "障害", "鯖"]
                status_context = ""
                
                if any(k in user_text for k in keywords):
                    # === [C:V1仕様] 会話時もフルスペックで検索する ===
                    loop = asyncio.get_running_loop()
                    official = await loop.run_in_executor(None, get_vrc_status_data)
                    twitter = await loop.run_in_executor(None, lambda: get_twitter_data("VRChat"))
                    status_context = f"{official}\n\n{twitter}" 

                image_bytes = None
                if message.attachments:
                    try:
                        image_bytes = await message.attachments[0].read()
                    except: pass

                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None, 
                    self.gemini.generate_chat_response, 
                    message.channel.id, 
                    user_text, 
                    image_bytes,
                    status_context 
                )
                await message.reply(response[:2000])

# --------------------------------------------------------------------------------
# Main Entry Point
# --------------------------------------------------------------------------------
if __name__ == "__main__":
    if not GEMINI_API_KEY or not DISCORD_BOT_TOKEN:
        print("Error: .envファイルを確認してください (GEMINI_API_KEY, DISCORD_BOT_TOKEN)")
        exit(1)

    bot = VRChatStatusBot()

    # ==========================================
    # スラッシュコマンド定義
    # ==========================================

    # --- チャンネル登録・解除 ---
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

    # --- ロール・ユーザーメンション設定 ---

    @bot.tree.command(name="add_notify_role", description="[管理者用] 障害通知時にメンションするロールを追加します")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="通知を送りたいロール")
    async def add_notify_role(interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用可能です。", ephemeral=True)
            return

        success = bot.channel_manager.add_role_mention(interaction.guild.id, role.id)
        if success:
            await interaction.response.send_message(f"✅ ロール {role.mention} を通知対象に追加しました。障害発生時にメンションされます。")
        else:
            await interaction.response.send_message(f"ℹ️ ロール {role.mention} は既に登録されています。")

    @bot.tree.command(name="remove_notify_role", description="[管理者用] 障害通知時のロールメンションを解除します")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="解除したいロール")
    async def remove_notify_role(interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用可能です。", ephemeral=True)
            return

        success = bot.channel_manager.remove_role_mention(interaction.guild.id, role.id)
        if success:
            await interaction.response.send_message(f"👋 ロール {role.mention} の通知メンションを解除しました。")
        else:
            await interaction.response.send_message(f"ℹ️ ロール {role.mention} は登録されていません。")

    @bot.tree.command(name="subscribe_mention", description="障害通知時に自分へのメンションを受け取る設定にします")
    async def subscribe_mention(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用可能です。", ephemeral=True)
            return

        success = bot.channel_manager.add_user_mention(interaction.guild.id, interaction.user.id)
        if success:
            await interaction.response.send_message(f"✅ {interaction.user.mention} さんを通知リストに追加しました！\nこのサーバーで障害通知がある際にメンションでお知らせします。")
        else:
            await interaction.response.send_message("ℹ️ 既に登録済みです。通知が必要なくなったら `/unsubscribe_mention` してくださいね。")

    @bot.tree.command(name="unsubscribe_mention", description="障害通知時の自分へのメンションを解除します")
    async def unsubscribe_mention(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用可能です。", ephemeral=True)
            return

        success = bot.channel_manager.remove_user_mention(interaction.guild.id, interaction.user.id)
        if success:
            await interaction.response.send_message(f"👋 {interaction.user.mention} さんの通知メンションを解除しました。")
        else:
            await interaction.response.send_message("ℹ️ リストに登録されていません。")

    # --- 診断・テスト ---

    @bot.tree.command(name="vrc_status", description="現在のVRChatサーバーステータスをAIが診断します")
    async def vrc_status(interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            loop = asyncio.get_running_loop()
            official = await loop.run_in_executor(None, get_vrc_status_data)
            twitter = await loop.run_in_executor(None, lambda: get_twitter_data("VRChat"))
            
            analysis = await loop.run_in_executor(None, bot.gemini.analyze_situation, official, twitter)
            
            color = discord.Color.red() if analysis.is_outage else discord.Color.green()
            embed = discord.Embed(title="📊 VRChat ステータス診断", color=color)
            embed.add_field(name="判定", value=f"**{'⚠️ 障害の可能性あり' if analysis.is_outage else '✅ おそらく正常'}**", inline=False)
            embed.add_field(name="AIコメント", value=analysis.notification_message or "特筆すべき異常は見当たりません。", inline=False)
            embed.add_field(name="詳細", value=f"深刻度: {analysis.severity}", inline=False)
            embed.set_footer(text="Powered by Google Gemini 2.5")
            
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ 診断中にエラーが発生しました: {e}")

    @bot.tree.command(name="test_notify", description="[管理者用] 偽データを使ってAIの障害診断をテストします")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_notify(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        await interaction.followup.send("🧪 テストモード起動... 偽の障害データを生成し、AIに分析させています...")
        # 共通のテストロジックを呼び出し
        await bot.run_test_diagnosis(interaction)

    bot.run(DISCORD_BOT_TOKEN)