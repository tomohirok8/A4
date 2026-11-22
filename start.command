#!/bin/zsh
# 株ポートフォリオアプリ起動スクリプト（Finder でダブルクリックして起動）
cd "$(dirname "$0")" || exit 1

PORT=5050
URL="http://127.0.0.1:${PORT}"

# すでに起動している場合はブラウザを開くだけ
if curl -s -o /dev/null "$URL"; then
  echo "アプリはすでに起動しています: $URL"
  open "$URL"
  exit 0
fi

# 初回のみ: 仮想環境の作成とライブラリのインストール
if [ ! -x .venv/bin/python ]; then
  echo "初回セットアップ中（仮想環境を作成しています）..."
  python3 -m venv .venv || { echo "python3 が見つかりません"; read -k1; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt || { echo "インストールに失敗しました"; read -k1; exit 1; }
fi

# サーバーの起動を待ってからブラウザを開く
(
  for i in {1..30}; do
    if curl -s -o /dev/null "$URL"; then open "$URL"; exit 0; fi
    sleep 0.5
  done
) &

echo "株ポートフォリオを起動しました: $URL"
echo "終了するには、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。"
echo
export PYTHONWARNINGS=ignore
exec .venv/bin/python app.py
