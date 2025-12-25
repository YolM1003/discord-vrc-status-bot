import os
from dotenv import load_dotenv
from google import genai

def test_gemini_connection():
    """
    .envファイルからAPIキーを読み込み、Gemini APIへの接続テストを行います。
    """
    # 1. 環境変数のロード
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")

    print("🔍 設定を確認中...")

    if not api_key:
        print("❌ エラー: .env ファイルが見つからないか、GEMINI_API_KEY が設定されていません。")
        print("   .env.example をコピーして .env を作成し、キーを貼り付けてください。")
        return

    # キーの前後を表示して確認（セキュリティのため一部伏せ字）
    masked_key = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***"
    print(f"🔑 API Key: {masked_key}")

    # 2. クライアントの初期化
    print("📡 Gemini API (gemini-2.5-flash) に接続を試みています...")
    
    try:
        client = genai.Client(api_key=api_key)
        
        # 3. テストプロンプトの送信
        # Bot本体で使用しているモデルと同じ 'gemini-2.5-flash' でテストします
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="こんにちは！APIのテスト中です。元気ですか？一言で返して。"
        )

        # 4. 結果の表示
        if response.text:
            print("\n✅ 接続成功！ Geminiからの応答がありました:")
            print("--------------------------------------------------")
            print(f"🤖 {response.text}")
            print("--------------------------------------------------")
            print("🎉 APIキーは正しく機能しています！")
        else:
            print("\n⚠️ 応答が空でした。何かおかしいかもしれません。")

    except Exception as e:
        print("\n❌ 接続エラーが発生しました。")
        print("エラー内容:")
        print(e)
        print("\n考えられる原因:")
        print("- APIキーが間違っている")
        print("- インターネット接続がない")
        print("- Google AI Studio側でAPIが無効化されている")

if __name__ == "__main__":
    test_gemini_connection()