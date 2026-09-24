# Discord Rank + VC読み上げBot

## コマンド
- `/vc_join` 自分がいるVCへ接続
- `/vc_test` テスト読み上げ
- `/vc_say` 指定文を読み上げ
- `/vc_auto` チャット自動読み上げ（管理者）
- `/vc_status` VC/FFmpeg状態確認
- `/vc_leave` 切断
- `/rank` / `/leaderboard` / `/reset_rank`

## Chromebook/Linux
sudo apt update
sudo apt install -y ffmpeg python3 python3-pip
python3 -m pip install -r requirements.txt --break-system-packages

`.env` に
DISCORD_BOT_TOKEN=あなたのBotトークン
を設定して `python3 main.py` で起動。

Discord Developer Portal の Bot 設定で Message Content Intent と Server Members Intent をONにしてください。
BotにはVCの「接続」「発言(Speak)」権限が必要です。
