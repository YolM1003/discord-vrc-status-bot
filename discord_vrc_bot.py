"""
VRChat Status Bot - Hybrid Ultimate Edition
Version: 4.0 (Hybrid-Ultimate)

v3.0 Hybrid版をベースに、v4.0 Masterの優れた機能を統合した最強版

主な機能:
- 高度なFlapping対策（複数カウンター + クールダウン）
- 本格的なロギングシステム (logging)
- 5回リトライ + Keep-Alive (最強の安定性)
- リプライ対応 + 賢い分割送信
- 分析不確実性チェック
- 深刻度ランキング (0-4段階)

作成: 2024-12-26
"""

import os
import io
import json
import asyncio
import time
import requests
import logging
import discord
from discord import app_commands
from discord.ext import tasks, commands
from PIL import Image
from typing import Optional, Dict, List, Any
from enum import Enum
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
# 0. 環境設定 & ロギング設定
# --------------------------------------------------------------------------------
load_dotenv()

# ロガーの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("VRChatStatusBot")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TWITTER_API_IO_KEY = os.getenv("TWITTER_API_IO_KEY")
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX")

DATA_FILE = "notify_channels.json"

# --------------------------------------------------------------------------------
# 共通 HTTPセッションの作成 (v4.0 Robust Version)
# --------------------------------------------------------------------------------
def create_robust_session():
    """
    リトライ戦略とキープアライブを組み込んだ高耐久セッションを作成
    TwitterAPI.io や Google API との通信安定性を確保する
    """
    session = requests.Session()
    
    # リトライ設定: 
    # 429(Rate Limit), 5xx(Server Error) に対して指数バックオフでリトライ
    retries = Retry(
        total=5,  # 最大リトライ回数を5回に強化
        backoff_factor=1,  # 1s, 2s, 4s, 8s... と待機時間が増える
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "VRChatStatusBot/4.0 (Hybrid-Ultimate; DiscordBot)",
        "Connection": "keep-alive"
    })
    
    return session

# グローバルセッション（TCPコネクション再利用）
http_client = create_robust_session()

# --------------------------------------------------------------------------------
# 1. 状態管理クラス (Enhanced State Machine with Flapping Prevention)
# --------------------------------------------------------------------------------
class MonitorState(Enum):
    NORMAL = "NORMAL"           # 正常
    SUSPECTED = "SUSPECTED"     # 疑義（1回目検知、まだ通知しない）
    OUTAGE = "OUTAGE"           # 障害発生中（通知済み）
    MONITORING = "MONITORING"   # 復旧監視中（障害通知後、状況は改善したがまだ様子見）

class StatusContext:
    """監視の状態を保持するクラス（高度なFlapping対策搭載）"""
    def __init__(self):
        self.state = MonitorState.NORMAL
        
        # === 基本カウンター ===
        self.consecutive_outage_count = 0    # 障害検知カウンター
        self.consecutive_recovery_count = 0  # 復旧検知カウンター
        
        # === GPT版から追加: 高度なカウンター ===
        self.notify_streak = 0               # should_notify=True の連続回数
        self.degraded_streak = 0             # 不安定状態の連続回数
        self.normal_streak = 0               # 正常の連続回数
        
        # === タイムスタンプ管理（クールダウン用） ===
        self.last_alert_timestamp = 0.0      # 最後の障害通知時刻
        self.last_recovery_timestamp = 0.0   # 最後の復旧通知時刻
        
        # === 深刻度管理 ===
        self.last_severity = "なし"
        self.last_severity_score = 0
        
        # === 通知済みフラグ ===
        self.notification_sent = False

    def get_severity_score(self, severity_str: str) -> int:
        """深刻度文字列を数値スコアに変換（より詳細な分類）"""
        s = severity_str.lower()
        
        # GPT版の_severity_rank を統合・拡張
        if "公式" in s or "official" in s or "major" in s:
            return 4
        if "接続不可" in s or "入れない" in s or "cannot connect" in s:
            return 3
        if "不安定" in s or "重い" in s or "degraded" in s or "unstable" in s:
            return 2
        if "軽微" in s or "minor" in s:
            return 1
        if "なし" in s or "normal" in s:
            return 0
        
        # 不明の場合は中間値
        return 2
    
    def reset_counters(self):
        """カウンターをリセット"""
        self.consecutive_outage_count = 0
        self.consecutive_recovery_count = 0
        self.notify_streak = 0
        self.degraded_streak = 0
        self.normal_streak = 0

# グローバルな状態管理インスタンス
status_context = StatusContext()

# === クールダウン設定（GPT版から追加） ===
ALERT_COOLDOWN_SEC = 1800       # 30分（障害通知の最小間隔）
RECOVERY_COOLDOWN_SEC = 1800    # 30分（復旧通知の最小間隔）

# === 確定閾値設定 ===
OUTAGE_NOTIFY_CONFIRM_COUNT = 2  # 通常の障害確定までの回数
RECOVERY_CONFIRM_COUNT = 3       # 復旧確定までの回数
MONITORING_ENTER_CONFIRM_COUNT = 2  # MONITORING状態への移行閾値

# --------------------------------------------------------------------------------
# ヘルパー関数: レポート処理
# --------------------------------------------------------------------------------
def extract_sections(report: str) -> list:
    """
    AIレポートを ### 見出しで分割してセクションごとに返す
    
    Args:
        report: AI生成レポート全文
    
    Returns:
        セクションのリスト
    """
    sections = []
    current_section = []
    
    for line in report.split('\n'):
        if line.startswith('###'):
            if current_section:
                sections.append('\n'.join(current_section))
            current_section = [line]
        else:
            current_section.append(line)
    
    if current_section:
        sections.append('\n'.join(current_section))
    
    return sections

def extract_twitter_summary(full_report: str) -> str:
    """
    レポートからTwitter分析の要点を抽出
    
    Args:
        full_report: AI生成レポート全文
    
    Returns:
        Twitter分析の要約（1行）
    """
    # Twitter関連のセクションを探す
    lines = full_report.lower().split('\n')
    for i, line in enumerate(lines):
        if 'twitter' in line or 'ツイート' in line:
            # 次の数行から要点を抽出
            for j in range(i, min(i+10, len(lines))):
                if '報告' in lines[j] or 'なし' in lines[j] or '多数' in lines[j]:
                    summary = lines[j].strip()
                    # 簡潔化
                    if len(summary) > 50:
                        summary = summary[:50] + "..."
                    return summary
    
    return "情報収集中"

def smart_split(text: str, max_length: int = 2000) -> list:
    """
    改行位置を優先してテキストを分割
    
    Args:
        text: 分割するテキスト
        max_length: 最大文字数
    
    Returns:
        分割されたテキストのリスト
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    remaining = text
    
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        
        # max_length以内で最後の改行位置を探す
        split_index = remaining.rfind('\n', 0, max_length)
        if split_index == -1:
            split_index = max_length
        else:
            split_index += 1
        
        chunks.append(remaining[:split_index])
        remaining = remaining[split_index:]
    
    return chunks

# --------------------------------------------------------------------------------
# 2. データ管理クラス (JSONファイルで通知先を永続化)
# --------------------------------------------------------------------------------
class ChannelManager:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = self._load_data()

    def _load_data(self) -> Dict[str, Any]:
        default_data = {"channels": [], "mentions": {}}
        if not os.path.exists(self.filepath):
            return default_data
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, dict): return default_data
                if "channels" not in data: data["channels"] = []
                if "mentions" not in data: data["mentions"] = {}
                return data
        except Exception:
            return default_data

    def save_data(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"データ保存エラー: {e}")

    def add_channel(self, channel_id: int) -> bool:
        channels = set(self.data["channels"])
        if channel_id in channels: return False
        channels.add(channel_id)
        self.data["channels"] = list(channels)
        self.save_data()
        return True

    def remove_channel(self, channel_id: int) -> bool:
        channels = set(self.data["channels"])
        if channel_id not in channels: return False
        channels.remove(channel_id)
        self.data["channels"] = list(channels)
        self.save_data()
        return True

    def get_all_channels(self) -> List[int]:
        return self.data["channels"]

    def _get_guild_mentions(self, guild_id: int) -> Dict[str, List[int]]:
        sid = str(guild_id)
        if sid not in self.data["mentions"]:
            self.data["mentions"][sid] = {"roles": [], "users": []}
        return self.data["mentions"][sid]

    def add_role_mention(self, guild_id: int, role_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        roles = set(settings["roles"])
        if role_id in roles: return False
        roles.add(role_id)
        settings["roles"] = list(roles)
        self.save_data()
        return True

    def remove_role_mention(self, guild_id: int, role_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        roles = set(settings["roles"])
        if role_id not in roles: return False
        roles.remove(role_id)
        settings["roles"] = list(roles)
        self.save_data()
        return True

    def add_user_mention(self, guild_id: int, user_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        users = set(settings["users"])
        if user_id in users: return False
        users.add(user_id)
        settings["users"] = list(users)
        self.save_data()
        return True

    def remove_user_mention(self, guild_id: int, user_id: int) -> bool:
        settings = self._get_guild_mentions(guild_id)
        users = set(settings["users"])
        if user_id not in users: return False
        users.remove(user_id)
        settings["users"] = list(users)
        self.save_data()
        return True

    def get_mention_string(self, guild_id: int) -> str:
        sid = str(guild_id)
        if sid not in self.data["mentions"]: return ""
        settings = self.data["mentions"][sid]
        mentions = [f"<@&{rid}>" for rid in settings.get("roles", [])]
        mentions.extend([f"<@{uid}>" for uid in settings.get("users", [])])
        return " ".join(mentions)

# --------------------------------------------------------------------------------
# 3. データ構造定義 (Pydantic)
# --------------------------------------------------------------------------------
class OutageAnalysis(BaseModel):
    is_outage: bool = Field(description="障害が発生している、または発生している可能性が高い場合はTrue。")
    is_official: bool = Field(description="VRChat公式ステータスAPIで障害が発表されている場合はTrue。")
    severity: str = Field(description="深刻度 (例: 'なし', '軽微', '不安定', '接続不可', 'Major Outage')")
    should_notify: bool = Field(description="ユーザーに通知を送るべき状況かどうか。")
    notification_message: str = Field(description="通知用のメッセージ本文。推測による断定は避けること。通知不要なら空文字。")

# --------------------------------------------------------------------------------
# 4. 情報収集ツール群
# --------------------------------------------------------------------------------
def get_vrc_status_data() -> str:
    url = "https://status.vrchat.com/api/v2/status.json"
    try:
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", {})
        return f"公式Status: {status.get('indicator', 'unknown')} - {status.get('description', '')}"
    except Exception as e:
        return f"公式Status取得エラー: {e}"

def get_twitter_data(query: str = "VRChat", limit: int = 10) -> str:
    if not TWITTER_API_IO_KEY: return "Twitter API Key未設定のため検索スキップ"
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_IO_KEY}
    safe_limit = min(limit, 20)
    
    # AIが指定したクエリにフィルタがなければ安全策として付与
    final_query = query
    if "filter:" not in query:
        final_query += " -filter:retweets" 

    params = {
        "query": final_query,
        "queryType": "Latest",
        "limit": safe_limit
    }
    try:
        resp = http_client.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tweets = data.get("tweets", [])
        if not tweets: return "関連ツイートなし"
        
        result_text = f"Twitter検索結果 (Latest, limit={safe_limit}):\n"
        for t in tweets:
            user_obj = t.get("author") or t.get("user") or {}
            user = user_obj.get("userName") or "unknown"
            text = t.get("text", "")
            created_at = t.get("createdAt", t.get("created_at", ""))
            result_text += f"- @{user} ({created_at}): {text}\n"
        return result_text
    except Exception as e:
        return f"Twitter検索エラー: {e}"

def search_web_data(query: str, num_results: int = 3) -> str:
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX: return "Google Search API Key/CX 未設定のためスキップ"
    url = "https://www.googleapis.com/customsearch/v1"
    safe_num = min(num_results, 5)
    params = {"key": GOOGLE_SEARCH_API_KEY, "cx": GOOGLE_SEARCH_CX, "q": query, "num": safe_num}
    try:
        resp = http_client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items: return "Web検索結果: 関連情報なし"
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
    try:
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        for script in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            script.decompose()
        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        clean_text = '\n'.join(chunk for chunk in lines for chunk in chunk.split("  ") if chunk)
        if len(clean_text) > 5000: clean_text = clean_text[:5000] + "\n...(以下省略)"
        return f"URL: {url}\n\n[Page Content]\n{clean_text}"
    except Exception as e:
        return f"URL取得エラー ({url}): {e}"

# --------------------------------------------------------------------------------
# 5. Gemini クライアントラッパー
# --------------------------------------------------------------------------------
class GeminiHandler:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.investigator_model_id = "gemini-3-flash-preview"
        self.chat_model_id = "gemini-3-flash-preview"
        self.judge_model_id = "gemini-2.5-flash"
        self.chat_sessions: Dict[int, object] = {}
        self.tools = [get_vrc_status_data, get_twitter_data, search_web_data, fetch_url_content]
        self.tool_map = {
            "get_vrc_status_data": get_vrc_status_data,
            "get_twitter_data": get_twitter_data,
            "search_web_data": search_web_data,
            "fetch_url_content": fetch_url_content
        }

    def get_session(self, channel_id: int):
        if channel_id not in self.chat_sessions:
            sys_inst = (
                "あなたはVRChatステータスボットです。ユーザーの質問に正確に答えてください。\n"
                "検索ツールを活用し、常に最新情報を確認してください。\n"
                "推測で断定的なことを言うのは避けてください。"
            )
            self.chat_sessions[channel_id] = self.client.chats.create(
                model=self.chat_model_id,
                config=types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=1.0,
                    tools=self.tools
                )
            )
        return self.chat_sessions[channel_id]

    def _execute_tool_loop(self, chat_session, contents, max_turns=5) -> str:
        try:
            response = chat_session.send_message(contents)
            current_turn = 0
            while response.function_calls and current_turn < max_turns:
                current_turn += 1
                func_res_list = []
                for call in response.function_calls:
                    if call.name in self.tool_map:
                        logger.debug(f"Tool実行: {call.name}")
                        try:
                            res = self.tool_map[call.name](**call.args)
                        except Exception as e:
                            res = f"Error: {e}"
                        func_res_list.append(types.FunctionResponse(name=call.name, response={"result": res}))
                if func_res_list:
                    response = chat_session.send_message([types.Part.from_function_response(i) for i in func_res_list])
                else: break
            return response.text
        except Exception as e:
            return f"Error in tool loop: {e}"

    def generate_chat_response(self, channel_id: int, text: str, image_bytes: Optional[bytes] = None, status_context: str = "") -> str:
        chat = self.get_session(channel_id)
        prompt_content = text
        if status_context:
            prompt_content = f"{text}\n\n[最新調査レポート(参考)]\n{status_context}"
        contents = [prompt_content]
        if image_bytes:
            try: contents.append(Image.open(io.BytesIO(image_bytes)))
            except: pass
        return self._execute_tool_loop(chat, contents)

    def investigate_server_status(self) -> str:
        """Phase 1: 自律調査"""
        sys_inst = (
            "あなたはVRChatサーバー監視の専門家です。現在の障害状況を調査してください。\n"
            "1. 公式APIを確認\n"
            "2. Twitter検索でユーザーの生の反応を確認 (検索クエリ例: 'VRChat (落ちた OR 重い) lang:ja -filter:retweets')\n"
            "3. 調査結果を客観的な事実に基づいてまとめてください。"
        )
        chat = self.client.chats.create(
            model=self.investigator_model_id,
            config=types.GenerateContentConfig(system_instruction=sys_inst, tools=self.tools)
        )
        return self._execute_tool_loop(chat, ["現在のVRChatの状況を詳しく調査してレポートを作成してください。"], max_turns=5)

    def analyze_situation_report(self, investigation_report: str, current_status_obj: StatusContext) -> OutageAnalysis:
        """Phase 2: 障害判定 (JSON) - ステートマシンを考慮した指示"""
        
        # 現在の状態をプロンプトに含める（文脈の注入）
        state_desc = {
            MonitorState.NORMAL: "現在、システムは【正常】です。",
            MonitorState.SUSPECTED: "現在、システムは【障害の疑い】があります。",
            MonitorState.OUTAGE: "現在、システムは【障害発生中】として通知済みです。",
            MonitorState.MONITORING: "現在、システムは【復旧監視中】です（まだ油断できない状態）。"
        }
        current_state_text = state_desc.get(current_status_obj.state, "状態不明")

        prompt = (
            f"{current_state_text}\n"
            "以下の調査レポートに基づき、現状を判定してください。\n\n"
            f"=== 調査レポート ===\n{investigation_report}\n\n"
            "**判定のガイドライン:**\n"
            "1. **公式発表(Major Outage)**がある場合は `is_official=True`, `is_outage=True`。\n"
            "2. **サイレント障害**: 公式が正常でも、Twitter等で「入れない」報告が多数あれば `is_outage=True`。\n"
            "3. **復旧判定の慎重化**: 「障害発生中」または「復旧監視中」の場合、少しでも不安要素（一部まだ重いなど）があれば、"
            "   安易に `is_outage=False` にせず、警戒を続けてください。\n"
            "4. **文章**: 「AWSが原因」などの根拠のない断定は禁止。事実のみを伝えること。\n\n"
            "JSON形式で出力してください。"
        )

        try:
            response = self.client.models.generate_content(
                model=self.judge_model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=OutageAnalysis,
                    thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_budget=2048)
                )
            )
            return response.parsed
        except Exception as e:
            logger.error(f"AI分析失敗: {e}")
            # エラー時は現状維持のような安全な値を返す
            return OutageAnalysis(
                is_outage=False, is_official=False, severity="Unknown", should_notify=False, notification_message=""
            )

# --------------------------------------------------------------------------------
# Discord UI Components: 詳細表示ボタン
# --------------------------------------------------------------------------------
class DetailButton(discord.ui.View):
    """詳細分析をスレッドで表示するためのボタン"""
    
    def __init__(self, full_report: str):
        super().__init__(timeout=3600)  # 1時間有効
        self.full_report = full_report
        self.message = None
    
    @discord.ui.button(label="📖 詳細分析を読む", style=discord.ButtonStyle.primary)
    async def show_detail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        # スレッド作成可能かチェック（Guildメッセージのみ）
        if not hasattr(self.message, 'guild') or self.message.guild is None:
            await interaction.followup.send(
                "❌ DMやGuild外のチャンネルではスレッドを作成できません。\n"
                "サーバー内のチャンネルで `/vrc_status` コマンドを使用してください。",
                ephemeral=True
            )
            return
        
        try:
            # スレッド作成
            thread = await self.message.create_thread(
                name="📊 障害分析レポート詳細",
                auto_archive_duration=60  # 1時間
            )
            
            logger.info(f"詳細分析スレッド作成: {thread.id}")
            
            # セクション分割送信
            sections = extract_sections(self.full_report)
            
            if not sections:
                # セクション分割できない場合は通常の分割
                chunks = smart_split(self.full_report, 2000)
                for chunk in chunks:
                    await thread.send(chunk)
            else:
                # セクションごとに送信
                for section in sections:
                    if len(section) > 2000:
                        # セクションが長すぎる場合は更に分割
                        chunks = smart_split(section, 2000)
                        for chunk in chunks:
                            await thread.send(chunk)
                    else:
                        await thread.send(section)
            
            # ボタン無効化
            button.disabled = True
            button.label = "✅ スレッド作成済み"
            button.style = discord.ButtonStyle.success
            await self.message.edit(view=self)
            
            await interaction.followup.send(
                f"📊 詳細分析を {thread.mention} に投稿しました！",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"スレッド作成エラー: {e}")
            await interaction.followup.send(
                "❌ スレッド作成に失敗しました。少し時間をおいてから再度お試しください。",
                ephemeral=True
            )

# --------------------------------------------------------------------------------
# 6. Discord Bot Implementation (Enhanced Hybrid State Machine)
# --------------------------------------------------------------------------------
class VRChatStatusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.gemini = GeminiHandler(GEMINI_API_KEY)
        self.channel_manager = ChannelManager(DATA_FILE)
        self.latest_investigation_report = ""

    def create_summary_embed(self, analysis: OutageAnalysis, full_report: str, is_manual: bool = False) -> discord.Embed:
        """
        やや詳しめの要約Embedを生成
        
        Args:
            analysis: AI分析結果
            full_report: 完全なレポート
            is_manual: 手動診断かどうか
        
        Returns:
            Discord Embed
        """
        # 色の決定
        if analysis.is_outage:
            color = discord.Color.red()
            title = "⚠️ VRChat 障害検知"
        else:
            color = discord.Color.green()
            title = "🔍 VRChat ステータス診断"
        
        embed = discord.Embed(title=title, color=color)
        
        # 公式ステータス
        official_status = "⚠️ Major Outage" if analysis.is_official else "✅ 正常稼働中"
        embed.add_field(name="📊 公式", value=official_status, inline=True)
        
        # Twitter状況
        twitter_summary = extract_twitter_summary(full_report)
        embed.add_field(name="🐦 Twitter", value=twitter_summary, inline=True)
        
        # 改行
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        
        # 総合判定
        if analysis.is_outage:
            judgment = "⚠️ 障害の可能性"
        else:
            judgment = "✅ 正常"
        embed.add_field(name="🎯 判定", value=judgment, inline=True)
        
        # 深刻度
        severity_emoji = {
            "なし": "🟢",
            "軽微": "🟡",
            "不安定": "🟠",
            "接続不可": "🔴",
            "Major Outage": "🔴"
        }
        severity_display = severity_emoji.get(analysis.severity, "⚪") + " " + analysis.severity
        embed.add_field(name="📈 深刻度", value=severity_display, inline=True)
        
        # 改行
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        
        # ヒント
        if is_manual:
            embed.add_field(
                name="💡 ヒント",
                value="📊 詳細な分析はスレッドを確認してください",
                inline=False
            )
        else:
            embed.add_field(
                name="💡 ヒント",
                value="📖 詳細な分析を見るには下のボタンを押してください",
                inline=False
            )
        
        # タイムスタンプ
        embed.timestamp = discord.utils.utcnow()
        
        return embed

    async def setup_hook(self):
        await self.tree.sync()
        if self.channel_manager.get_all_channels():
            self.monitor_task.start()
            logger.info("🔄 監視タスクを開始しました")

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        await self.update_presence_from_state()

    # === GPT版から追加: 分析不確実性チェック ===
    def _is_analysis_inconclusive(self, analysis: OutageAnalysis) -> bool:
        """AI分析が失敗/不確実で、状態遷移に使うべきでない場合True"""
        sev = (analysis.severity or "").strip().lower()
        if sev == "unknown":
            return True
        return False

    async def update_presence_from_state(self, analysis: Optional[OutageAnalysis] = None):
        """現在のステータスに基づいてBotのPresenceを更新（GPT版の詳細表示を統合）"""
        state = status_context.state
        
        if state == MonitorState.OUTAGE:
            severity_display = analysis.severity if analysis else status_context.last_severity
            await self.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"⚠️ 障害発生中 ({severity_display})"
                )
            )
        elif state == MonitorState.MONITORING:
            # GPT版の改善: 監視中 vs 復旧確認中の区別
            is_still_degraded = (analysis and analysis.is_outage) if analysis else False
            label = "🟡 不安定監視中" if is_still_degraded else "🟡 復旧確認中"
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.Activity(type=discord.ActivityType.watching, name=label)
            )
        elif state == MonitorState.SUSPECTED:
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.Activity(type=discord.ActivityType.watching, name="🔍 確認中...")
            )
        else:  # NORMAL
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching, name="VRChat: 正常稼働中")
            )

    # ------------------------------------------------------------------
    # 監視ループ (10分ごと) - Enhanced Hybrid State Machine
    # ------------------------------------------------------------------
    @tasks.loop(minutes=10)
    async def monitor_task(self):
        target_channels = self.channel_manager.get_all_channels()
        if not target_channels: return

        logger.info(f"定期チェック: State={status_context.state.name}")
        loop = asyncio.get_running_loop()
        
        # Phase 1: 調査
        report = await loop.run_in_executor(None, self.gemini.investigate_server_status)
        self.latest_investigation_report = report
        
        # Phase 2: 判定
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, report, status_context)
        
        # === GPT版から追加: 分析不確実性チェック ===
        if self._is_analysis_inconclusive(analysis):
            logger.warning("分析が不確実なため、状態更新をスキップします (誤復旧防止)")
            return
        
        # デバッグ出力
        logger.info(f"AI判定: Outage={analysis.is_outage}, Notify={analysis.should_notify}, Official={analysis.is_official}, Severity={analysis.severity}")
        
        # === GPT版から追加: カウンター更新 ===
        raw_outage = bool(analysis.is_outage)
        raw_notify = bool(analysis.should_notify)
        
        # 各種カウンター更新
        status_context.notify_streak = status_context.notify_streak + 1 if raw_notify else 0
        status_context.normal_streak = status_context.normal_streak + 1 if not raw_outage else 0
        status_context.degraded_streak = status_context.degraded_streak + 1 if (raw_outage and not raw_notify) else 0
        
        # 深刻度スコア
        new_severity_score = status_context.get_severity_score(analysis.severity)
        
        # === GPT版から追加: クールダウンチェック ===
        now_ts = time.monotonic()
        can_send_alert = (now_ts - status_context.last_alert_timestamp) >= ALERT_COOLDOWN_SEC
        can_send_recovery = (now_ts - status_context.last_recovery_timestamp) >= RECOVERY_COOLDOWN_SEC
        
        # === GPT版から追加: 深刻度による早期確定 ===
        is_critical = new_severity_score >= 3  # 接続不可 or 公式発表
        notify_confirm = 1 if (is_critical or analysis.is_official) else OUTAGE_NOTIFY_CONFIRM_COUNT
        
        send_alert = False
        send_recovery = False
        alert_title = "**⚠️ VRChat 障害検知レポート**"
        alert_body = analysis.notification_message
        
        # ========================================================================
        # Enhanced State Machine Logic (Gemini版ベース + GPT版のFlapping対策)
        # ========================================================================
        
        # === NORMAL / SUSPECTED / MONITORING からの障害検知 ===
        if status_context.state in [MonitorState.NORMAL, MonitorState.SUSPECTED, MonitorState.MONITORING]:
            if analysis.is_outage:
                status_context.consecutive_outage_count += 1
                status_context.consecutive_recovery_count = 0
                
                # 確定条件: 公式障害 or クリティカル or notify_confirm回連続
                is_confirmed = (
                    analysis.is_official or
                    (status_context.consecutive_outage_count >= notify_confirm)
                )
                
                if is_confirmed:
                    if status_context.state != MonitorState.OUTAGE:
                        # 障害発生 (新規)
                        status_context.state = MonitorState.OUTAGE
                        if can_send_alert:
                            send_alert = True
                            status_context.last_alert_timestamp = now_ts
                            status_context.notification_sent = True
                            status_context.last_severity = analysis.severity
                            status_context.last_severity_score = new_severity_score
                            logger.info("状態変更: OUTAGE (新規障害通知)")
                    else:
                        # MONITORING からの再悪化
                        status_context.state = MonitorState.OUTAGE
                        if can_send_alert:
                            send_alert = True
                            alert_title = "**⚠️ 障害状況の再悪化**"
                            status_context.last_alert_timestamp = now_ts
                            logger.warning("状態変更: OUTAGE (再悪化)")
                else:
                    # まだ確定していない
                    status_context.state = MonitorState.SUSPECTED
                    logger.info(f"状態変更: SUSPECTED (Count: {status_context.consecutive_outage_count})")
            
            else:
                # 正常判定
                if status_context.state == MonitorState.MONITORING:
                    # 復旧カウンターを進める
                    status_context.consecutive_recovery_count += 1
                    logger.debug(f"Recovery Count: {status_context.consecutive_recovery_count}")
                    
                    # RECOVERY_CONFIRM_COUNT 回連続で正常なら復旧確定
                    if status_context.consecutive_recovery_count >= RECOVERY_CONFIRM_COUNT:
                        status_context.state = MonitorState.NORMAL
                        status_context.reset_counters()
                        status_context.last_severity = "なし"
                        status_context.last_severity_score = 0
                        
                        if status_context.notification_sent and can_send_recovery:
                            send_recovery = True
                            status_context.last_recovery_timestamp = now_ts
                        
                        status_context.notification_sent = False
                        logger.info("状態変更: NORMAL (復旧確定)")
                
                elif status_context.state == MonitorState.SUSPECTED:
                    # SUSPECTED で正常なら NORMAL に戻る
                    status_context.state = MonitorState.NORMAL
                    status_context.consecutive_outage_count = 0
                    logger.info("状態変更: NORMAL (誤検知クリア)")
                
                else:
                    # NORMAL で正常ならそのまま
                    status_context.consecutive_outage_count = 0

        # === OUTAGE 状態での処理 ===
        elif status_context.state == MonitorState.OUTAGE:
            if analysis.is_outage:
                # 障害継続
                status_context.consecutive_recovery_count = 0
                
                # === GPT版から追加: 深刻度アップ時の追加通知 ===
                if new_severity_score > status_context.last_severity_score and raw_notify:
                    if can_send_alert:
                        send_alert = True
                        alert_title = "**⚠️ 障害状況アップデート（深刻化）**"
                        status_context.last_alert_timestamp = now_ts
                        status_context.last_severity = analysis.severity
                        status_context.last_severity_score = new_severity_score
                        logger.warning(f"深刻度上昇: {analysis.severity}")
                
                else:
                    status_context.last_severity_score = new_severity_score
            
            else:
                # AIが「正常」と言い始めた -> MONITORING へ（サイレント移行）
                status_context.state = MonitorState.MONITORING
                status_context.consecutive_recovery_count = 1
                logger.info("状態変更: MONITORING (復旧の兆候、様子見開始)")

        # === Bot表示の更新 ===
        await self.update_presence_from_state(analysis)

        # === 通知送信 ===
        if send_alert:
            logger.warning(f"障害通知送信: {alert_title}")
            # 新形式: 要約Embed + ボタン
            await self.broadcast_message(
                content="",  # 後方互換用（使われない）
                is_alert=True,
                analysis=analysis,
                full_report=report
            )

        if send_recovery:
            logger.info("復旧通知送信")
            recovery_msg = analysis.notification_message if analysis.notification_message else "障害状況は解消されたようです。（安定確認済み）"
            # 復旧通知は旧形式（シンプルなテキスト）
            await self.broadcast_message(
                content=f"✅ **復旧報告**\n{recovery_msg}",
                is_alert=False
            )

    @monitor_task.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    async def broadcast_message(self, content: str, is_alert: bool, analysis: Optional[OutageAnalysis] = None, full_report: str = ""):
        """
        通知をブロードキャスト（要約Embed + ボタン対応）
        
        Args:
            content: 通知メッセージ（後方互換用）
            is_alert: 障害通知かどうか
            analysis: AI分析結果（Embed生成用）
            full_report: 完全なレポート（スレッド送信用）
        """
        target_channels = self.channel_manager.get_all_channels()
        
        for ch_id in target_channels:
            channel = self.get_channel(ch_id)
            if channel:
                try:
                    # メンション文字列の取得
                    mention_str = ""
                    if is_alert and hasattr(channel, 'guild') and channel.guild:
                        mention_str = self.channel_manager.get_mention_string(channel.guild.id)
                    
                    # 新形式（Embed + 詳細）を使用
                    if analysis and full_report:
                        # 要約Embed作成
                        embed = self.create_summary_embed(analysis, full_report, is_manual=False)
                        
                        # メンション付きコンテンツ
                        message_content = mention_str if mention_str else None
                        
                        # ボタン付きで送信（Guildチャンネルのみ）
                        if hasattr(channel, 'guild') and channel.guild:
                            view = DetailButton(full_report)
                            message = await channel.send(content=message_content, embed=embed, view=view)
                            view.message = message  # 後から設定
                            logger.info(f"要約Embed + ボタン送信 (ch_id={ch_id})")
                        else:
                            # DMチャンネルの場合はボタンなし（Embedのみ）
                            await channel.send(content=message_content, embed=embed)
                            # 詳細は分割送信
                            chunks = smart_split(full_report, 2000)
                            for chunk in chunks:
                                await channel.send(chunk)
                            logger.info(f"要約Embed送信（DMのためボタンなし） (ch_id={ch_id})")
                    
                    # 旧形式（テキストのみ）
                    else:
                        final_content = content
                        if mention_str:
                            final_content = f"{content}\n\n{mention_str}"
                        await channel.send(final_content)
                        logger.info(f"テキスト通知送信 (ch_id={ch_id})")
                    
                except Exception as e:
                    logger.error(f"送信失敗 (ch_id={ch_id}): {e}")

    # ------------------------------------------------------------------
    # テスト & 手動診断（要約Embed + 即スレッド）
    # ------------------------------------------------------------------
    async def run_realtime_diagnosis_test(self, responder):
        """現在の実データ診断（要約Embed + 即スレッド）"""
        loop = asyncio.get_running_loop()
        
        # 開始メッセージ
        if isinstance(responder, discord.Interaction):
            await responder.followup.send("🕵️ **現在のリアルタイム調査を実行します...**")
        else:
            await responder.send("🕵️ **現在のリアルタイム調査を実行します...**")

        # Phase 1: 調査
        report = await loop.run_in_executor(None, self.gemini.investigate_server_status)
        self.latest_investigation_report = report
        
        # Phase 2: 判定
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, report, status_context)
        
        # 結果送信（要約Embed + 即スレッド）
        await self._send_diagnosis_with_thread(responder, analysis, report)

    async def run_fake_diagnosis_test(self, responder):
        """偽データテスト（要約Embed + 即スレッド）"""
        fake_report = (
            "### 1. 公式ステータス\n"
            "公式APIは「Major Outage」を報告しています。\n\n"
            "### 2. Twitter分析\n"
            "過去30分で「ログインできない」「落ちた」という報告が多数確認されています。\n\n"
            "### 3. 専門家による分析とアドバイス\n"
            "現在、VRChat全体で大規模な障害が発生しています。公式の復旧を待つことをお勧めします。"
        )
        
        loop = asyncio.get_running_loop()
        
        # 開始メッセージ
        if isinstance(responder, discord.Interaction):
            await responder.followup.send("🧪 **テストモード(FAKE)を実行します...**")
        else:
            await responder.send("🧪 **テストモード(FAKE)を実行します...**")
        
        # 判定
        analysis = await loop.run_in_executor(None, self.gemini.analyze_situation_report, fake_report, status_context)
        
        # 結果送信（要約Embed + 即スレッド）
        await self._send_diagnosis_with_thread(responder, analysis, fake_report)

    async def _send_diagnosis_with_thread(self, responder, analysis: OutageAnalysis, full_report: str):
        """
        手動診断結果を要約Embed + 即スレッドで送信
        
        Args:
            responder: discord.Interaction または discord.TextChannel
            analysis: AI分析結果
            full_report: 完全なレポート
        """
        # 要約Embed作成
        embed = self.create_summary_embed(analysis, full_report, is_manual=True)
        
        # Guildかどうかを事前チェック
        is_guild_context = False
        if isinstance(responder, discord.Interaction):
            is_guild_context = responder.guild is not None
        elif hasattr(responder, 'guild'):
            is_guild_context = responder.guild is not None
        
        # Guild外の場合は分割送信にフォールバック
        if not is_guild_context:
            logger.warning("DMまたはGuild外のため、スレッド作成をスキップして分割送信します")
            # Embed送信
            if isinstance(responder, discord.Interaction):
                await responder.followup.send(embed=embed)
            else:
                await responder.send(embed=embed)
            
            # 従来の分割送信
            chunks = smart_split(full_report, 2000)
            for chunk in chunks:
                if isinstance(responder, discord.Interaction):
                    await responder.followup.send(chunk)
                else:
                    await responder.send(chunk)
            return
        
        # Embed送信とMessageオブジェクトの取得
        if isinstance(responder, discord.Interaction):
            # Interactionの場合は original_response() で正しいMessageを取得
            await responder.followup.send(embed=embed)
            message = await responder.original_response()
        else:
            # TextChannelの場合は普通にsend()
            message = await responder.send(embed=embed)
        
        # スレッド作成
        try:
            thread = await message.create_thread(
                name="🔍 診断レポート詳細",
                auto_archive_duration=60  # 1時間
            )
            
            logger.info(f"手動診断スレッド作成: {thread.id}")
            
            # セクション分割送信
            sections = extract_sections(full_report)
            
            if not sections:
                # セクション分割できない場合は通常の分割
                chunks = smart_split(full_report, 2000)
                for chunk in chunks:
                    await thread.send(chunk)
            else:
                # セクションごとに送信
                for section in sections:
                    if len(section) > 2000:
                        # セクションが長すぎる場合は更に分割
                        chunks = smart_split(section, 2000)
                        for chunk in chunks:
                            await thread.send(chunk)
                    else:
                        await thread.send(section)
            
            logger.info(f"詳細レポート送信完了: {len(sections)}セクション")
            
        except Exception as e:
            logger.error(f"スレッド作成エラー: {e}")
            # エラー時は従来の分割送信にフォールバック
            chunks = smart_split(full_report, 2000)
            for chunk in chunks:
                if isinstance(responder, discord.Interaction):
                    await responder.followup.send(chunk)
                else:
                    await responder.send(chunk)

    async def on_message(self, message):
        """メンション応答ロジック（v2版の充実した機能を統合）"""
        if message.author == self.user: return
        
        # === 判定ロジック ===
        
        # 1. ユーザーメンション (@BotName)
        is_user_mentioned = self.user in message.mentions
        
        # 2. DM (ダイレクトメッセージ)
        is_dm = isinstance(message.channel, discord.DMChannel)
        
        # 3. ロールメンション
        is_role_mentioned = False
        if message.guild and message.guild.me:
            bot_role_ids = [role.id for role in message.guild.me.roles]
            is_role_mentioned = any(role_id in bot_role_ids for role_id in message.raw_role_mentions)
        
        # 4. リプライ (Botのメッセージへの返信)
        is_reply_to_bot = False
        if message.reference and message.reference.cached_message:
            if message.reference.cached_message.author == self.user:
                is_reply_to_bot = True
        
        # いずれかの条件に当てはまれば反応する
        if is_user_mentioned or is_role_mentioned or is_reply_to_bot or is_dm:
            async with message.channel.typing():
                user_text = message.content
                
                # ユーザーメンション削除
                user_text = user_text.replace(f'<@!{self.user.id}>', '').replace(f'<@{self.user.id}>', '')
                
                # ロールメンション削除
                if message.guild and message.guild.me:
                    for role in message.guild.me.roles:
                        user_text = user_text.replace(f'<@&{role.id}>', '')
                
                user_text = user_text.strip()
                
                # 画像添付対応
                img_bytes = None
                if message.attachments:
                    try: 
                        img_bytes = await message.attachments[0].read()
                    except: 
                        pass
                
                # AI応答生成
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None, 
                    self.gemini.generate_chat_response, 
                    message.channel.id, 
                    user_text, 
                    img_bytes,
                    self.latest_investigation_report
                )
                
                # 賢い分割送信（改行位置を優先）
                if len(response) > 2000:
                    chunks = []
                    while response:
                        if len(response) <= 2000:
                            chunks.append(response)
                            break
                        
                        # 2000文字以内で最後の改行位置を探す
                        split_index = response.rfind('\n', 0, 2000)
                        if split_index == -1:
                            split_index = 2000
                        else:
                            split_index += 1
                        
                        chunks.append(response[:split_index])
                        response = response[split_index:]
                    
                    for chunk in chunks:
                        await message.reply(chunk)
                else:
                    await message.reply(response)

# --------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------
if __name__ == "__main__":
    if not GEMINI_API_KEY or not DISCORD_BOT_TOKEN:
        logger.error("環境変数が設定されていません: GEMINI_API_KEY と DISCORD_BOT_TOKEN を .env ファイルで設定してください")
        exit(1)

    bot = VRChatStatusBot()

    # === コマンド登録 ===
    @bot.tree.command(name="register_notify", description="通知先に登録")
    @app_commands.checks.has_permissions(administrator=True)
    async def register_notify(interaction: discord.Interaction):
        if bot.channel_manager.add_channel(interaction.channel_id):
            await interaction.response.send_message(f"✅ 登録しました: {interaction.channel.mention}")
            if not bot.monitor_task.is_running(): 
                bot.monitor_task.start()
        else: 
            await interaction.response.send_message("ℹ️ 既に登録済みです。")

    @bot.tree.command(name="unregister_notify", description="通知解除")
    @app_commands.checks.has_permissions(administrator=True)
    async def unregister_notify(interaction: discord.Interaction):
        if bot.channel_manager.remove_channel(interaction.channel_id):
            await interaction.response.send_message("👋 解除しました。")
        else: 
            await interaction.response.send_message("ℹ️ 登録されていません。")

    @bot.tree.command(name="add_notify_role", description="通知ロール追加")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_notify_role(interaction: discord.Interaction, role: discord.Role):
        if bot.channel_manager.add_role_mention(interaction.guild.id, role.id):
            await interaction.response.send_message(f"✅ 追加しました: {role.mention}")
        else: 
            await interaction.response.send_message("ℹ️ 既に登録済みです。")

    @bot.tree.command(name="remove_notify_role", description="通知ロール解除")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_notify_role(interaction: discord.Interaction, role: discord.Role):
        if bot.channel_manager.remove_role_mention(interaction.guild.id, role.id):
            await interaction.response.send_message(f"👋 解除しました: {role.mention}")
        else: 
            await interaction.response.send_message("ℹ️ 登録されていません。")

    @bot.tree.command(name="subscribe_mention", description="個人メンションON")
    async def subscribe_mention(interaction: discord.Interaction):
        if bot.channel_manager.add_user_mention(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(f"✅ 通知ON: {interaction.user.mention}")
        else: 
            await interaction.response.send_message("ℹ️ 既にONです。")

    @bot.tree.command(name="unsubscribe_mention", description="個人メンションOFF")
    async def unsubscribe_mention(interaction: discord.Interaction):
        if bot.channel_manager.remove_user_mention(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(f"👋 通知OFF: {interaction.user.mention}")
        else: 
            await interaction.response.send_message("ℹ️ 登録されていません。")

    @bot.tree.command(name="vrc_status", description="手動診断")
    async def vrc_status(interaction: discord.Interaction):
        await interaction.response.defer()
        await bot.run_realtime_diagnosis_test(interaction)

    @bot.tree.command(name="test_fake", description="[管理者] 偽データテスト")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_fake(interaction: discord.Interaction):
        await interaction.response.defer()
        await bot.run_fake_diagnosis_test(interaction)

    @bot.tree.command(name="test_real", description="[管理者] 実データテスト")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_real(interaction: discord.Interaction):
        await interaction.response.defer()
        await bot.run_realtime_diagnosis_test(interaction)

    @bot.tree.command(name="status_info", description="現在の監視状態を表示")
    async def status_info(interaction: discord.Interaction):
        """現在の状態とカウンターを表示"""
        embed = discord.Embed(
            title="📊 監視状態レポート",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="現在の状態", value=status_context.state.name, inline=True)
        embed.add_field(name="深刻度", value=status_context.last_severity, inline=True)
        embed.add_field(name="通知送信済み", value="✅" if status_context.notification_sent else "❌", inline=True)
        
        counters = (
            f"障害検知: {status_context.consecutive_outage_count}\n"
            f"復旧検知: {status_context.consecutive_recovery_count}\n"
            f"通知推奨: {status_context.notify_streak}\n"
            f"正常連続: {status_context.normal_streak}\n"
            f"不安定連続: {status_context.degraded_streak}"
        )
        embed.add_field(name="カウンター", value=counters, inline=False)
        
        # クールダウン情報
        now = time.monotonic()
        alert_cooldown_remain = max(0, ALERT_COOLDOWN_SEC - (now - status_context.last_alert_timestamp))
        recovery_cooldown_remain = max(0, RECOVERY_COOLDOWN_SEC - (now - status_context.last_recovery_timestamp))
        
        cooldown_info = (
            f"障害通知CD: {alert_cooldown_remain/60:.1f}分\n"
            f"復旧通知CD: {recovery_cooldown_remain/60:.1f}分"
        )
        embed.add_field(name="クールダウン", value=cooldown_info, inline=False)
        
        await interaction.response.send_message(embed=embed)

    bot.run(DISCORD_BOT_TOKEN)