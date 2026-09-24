# Discord Rank + VC 読み上げBot

## 起動
1. `.env.example` を `.env` にコピー
2. `.env` の `DISCORD_BOT_TOKEN` にBot Tokenを入れる
3. FFmpegをインストール
4. `pip install -r requirements.txt`
5. `python3 main.py`

## VCコマンド
- `/vc_join` : 自分がいるVCへBotを接続
- `/vc_leave` : Botを切断
- `/vc_say text:...` : 入力した文章を日本語で読み上げ
- `/vc_test` : テスト音声
- `/vc_auto enabled:true/false` : 通常のチャットを自動読み上げ（管理者のみ）

## ランク
- `/rank`
- `/leaderboard`
- `/reset_rank member:...`（管理者のみ）

Discord Developer Portal の Bot 設定で以下をONにしてください。
- Message Content Intent
- Server Members Intent

Chromebook/Linuxの場合:
`sudo apt update && sudo apt install -y python3 python3-pip ffmpeg`
