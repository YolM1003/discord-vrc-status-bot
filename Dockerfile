# ベースイメージ: Python 3.11 (3.10+が必要なため)
FROM python:3.11-slim

# コンテナ内の作業ディレクトリを設定
WORKDIR /app

# 環境変数の設定
# PYTHONDONTWRITEBYTECODE: .pycファイルを作成しない
# PYTHONUNBUFFERED: ログを即座に出力する（Dockerログで見やすくするため重要）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 依存関係ファイルのコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコード全体をコピー
COPY . .

# Botの起動
CMD ["python", "discord_vrc_bot.py"]