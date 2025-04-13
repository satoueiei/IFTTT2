# -*- coding: utf-8 -*-
# (他の import 文は変更なし)
import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
# twikit とその Client, errors をインポート
from twikit import Client, errors as twikit_errors
import datetime
from zoneinfo import ZoneInfo
import requests
import re
import logging
from keep_alive import keep_alive

# --- 初期設定 ---
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

if not DISCORD_BOT_TOKEN or not ENCRYPTION_KEY:
    raise ValueError("DISCORD_BOT_TOKEN と ENCRYPTION_KEY を .env ファイルに設定してください。")

try:
    fernet = Fernet(ENCRYPTION_KEY.encode())
except ValueError:
    raise ValueError("ENCRYPTION_KEYが無効な形式です。キー生成スクリプトで生成されたキーを使用してください。")

# ログレベル設定 (必要に応じて DEBUG, INFO, WARNING などに変更)
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True # ユーザー情報を取得するために必要になる場合がある

bot = commands.Bot(command_prefix="!", intents=intents)

USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

# --- データ管理関数 ---
def load_user_data(user_id):
    """ ユーザーIDに対応する設定ファイルを読み込む """
    filepath = os.path.join(USER_DATA_DIR, f"{user_id}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"ユーザーデータファイル {filepath} の読み込みに失敗しました。")
            return None
    return None # ファイルが存在しない場合は None を返す

def save_user_data(user_id, data):
    """ ユーザーIDに対応する設定ファイルにデータを保存する """
    filepath = os.path.join(USER_DATA_DIR, f"{user_id}.json")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except IOError as e:
        logger.error(f"ユーザーデータファイル {filepath} の保存に失敗しました: {e}")

# --- 暗号化/復号化関数 ---
def encrypt_data(data):
    """ 辞書または文字列データを暗号化して文字列として返す """
    if isinstance(data, dict):
        data_bytes = json.dumps(data).encode('utf-8')
    elif isinstance(data, str):
        data_bytes = data.encode('utf-8')
    else:
        raise TypeError("暗号化できるのは dict または str のみです。")
    return fernet.encrypt(data_bytes).decode('utf-8') # 保存用に文字列にする

def decrypt_data(encrypted_data):
    """ 暗号化された文字列データを復号化して元の型 (dict or str) で返す """
    try:
        decrypted_bytes = fernet.decrypt(encrypted_data.encode('utf-8'))
        # まずJSONとしてデコードを試みる
        try:
            return json.loads(decrypted_bytes.decode('utf-8'))
        except json.JSONDecodeError:
            # JSONでなければ文字列として返す
            return decrypted_bytes.decode('utf-8')
    except InvalidToken:
        logger.error("復号化に失敗しました。キーが変更されたか、データが破損しています。")
        return None
    except Exception as e:
        logger.error(f"復号化中に予期せぬエラー: {e}")
        return None

# --- Botイベントハンドラ ---
@bot.event
async def on_ready():
    """ Botが起動し、Discordへの接続が完了したときに呼ばれる """
    logger.info(f'{bot.user} としてログインしました')
    logger.info(f'Encryption Key Loaded: {"Yes" if ENCRYPTION_KEY else "No"}')
    logger.info("ユーザーデータディレクトリ: " + USER_DATA_DIR)
    tweet_checker_loop.start() # 定期実行タスクを開始

# --- 許可ユーザーIDリストの読み込み ---
ALLOWED_INVITER_IDS_STR = os.getenv("ALLOWED_INVITER_IDS", "")
ALLOWED_INVITER_IDS = set()
if ALLOWED_INVITER_IDS_STR:
    try:
        ALLOWED_INVITER_IDS = {int(uid.strip()) for uid in ALLOWED_INVITER_IDS_STR.split(',') if uid.strip().isdigit()}
        logger.info(f"招待を許可されたユーザーIDリストをロードしました: {ALLOWED_INVITER_IDS}")
    except ValueError:
        logger.error("ALLOWED_INVITER_IDS の形式が無効です。カンマ区切りの数値で指定してください。")
        ALLOWED_INVITER_IDS = set()
else:
    # リストが設定されていない場合の挙動を明確にする
    logger.warning("招待許可ユーザーID (ALLOWED_INVITER_IDS) が .env に設定されていません。")
    # ポリシーに応じて、誰でも招待可能にするか、誰も招待できないようにするか決める
    # ALLOWED_INVITER_IDS = None # 誰でもOKとする場合 (None でチェックをスキップ)
    ALLOWED_INVITER_IDS = set() # 誰も招待できないようにする場合 (空セット)

@bot.event
async def on_guild_join(guild: discord.Guild):
    """ Botが新しいサーバーに参加したときに呼び出されるイベント """
    logger.info(f"新しいサーバーに参加しました: {guild.name} (ID: {guild.id})")

    # 許可ユーザーリストが None (チェック不要) または空 (誰も許可しない) か確認
    if ALLOWED_INVITER_IDS is None:
         logger.info("招待ユーザーチェックはスキップされました (ALLOWED_INVITER_IDS is None)。")
         return
    if not ALLOWED_INVITER_IDS:
         logger.warning(f"招待許可ユーザーリストが空のため、サーバー {guild.name} (ID: {guild.id}) から退出します。")
         await leave_guild(guild, "招待許可ユーザーリストが空です。")
         return

    inviter = None
    try:
        # 監査ログを取得してBotを追加したユーザーを探す
        # 少し待機してから監査ログを取得する (記録されるまでのラグ考慮)
        await asyncio.sleep(2) # 2秒待機 (必要に応じて調整)

        # Botが追加された監査ログエントリーを取得 (limit=5 は念のため直近のログを複数見る)
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
            # 監査ログのターゲットが自分自身(Bot)か確認
            if entry.target.id == bot.user.id:
                inviter = entry.user # Botを追加したユーザー
                logger.info(f"サーバー {guild.name} にBotを追加したユーザーを特定しました: {inviter} (ID: {inviter.id})")
                break # 最初に見つかったエントリーを使用
        else:
             logger.warning(f"サーバー {guild.name} の監査ログからBotを追加したユーザーを特定できませんでした。")
             # 監査ログが見れない場合や記録がない場合の処理 (今回は退出させる)
             await leave_guild(guild, "Botを追加したユーザーを特定できませんでした（監査ログ権限不足または記録なし）。")
             return

    except discord.Forbidden:
        logger.error(f"サーバー {guild.name} (ID: {guild.id}) の監査ログを表示する権限がありません。招待ユーザーチェックを実行できません。")
        # 権限がない場合の処理 (今回は退出させる)
        await leave_guild(guild, "Botに監査ログの表示権限がないため、招待者を確認できませんでした。")
        return
    except discord.HTTPException as e:
        logger.error(f"監査ログの取得中にAPIエラーが発生しました: {e}")
        # APIエラーの場合も退出させる
        await leave_guild(guild, f"監査ログの取得中にエラーが発生しました: {e}")
        return
    except Exception as e:
        logger.error(f"監査ログ処理中に予期せぬエラー: {e}", exc_info=True)
        # 予期せぬエラーでも退出
        await leave_guild(guild, f"招待者の確認中に予期せぬエラーが発生しました。")
        return


    # 招待者が特定でき、かつ許可リストに含まれていない場合
    if inviter and inviter.id not in ALLOWED_INVITER_IDS:
        logger.warning(f"ユーザー {inviter} (ID: {inviter.id}) は招待許可リストに含まれていません。サーバー {guild.name} から退出します。")
        await leave_guild(guild, f"招待ユーザー ({inviter}) が許可されていません。")
    elif inviter:
        # 招待者が許可リストに含まれている場合
        logger.info(f"招待ユーザー {inviter} は許可リストに含まれています。サーバー {guild.name} に留まります。")
    # (inviter が None の場合は上で処理済み)


async def leave_guild(guild: discord.Guild, reason: str):
    """ サーバーから退出する処理をまとめた関数 """
    try:
        # 退出メッセージを送信 (任意)
        channel_to_send = guild.system_channel
        if channel_to_send is None:
            for channel in guild.text_channels:
                 if channel.permissions_for(guild.me).send_messages:
                    channel_to_send = channel
                    break
        if channel_to_send:
             await channel_to_send.send(f"このサーバーはBotの利用条件を満たしていないため退出します。\n理由: {reason}")
        else:
             logger.warning(f"サーバー {guild.name} に退出メッセージを送信できるチャンネルが見つかりませんでした。")

        # サーバーから退出
        await guild.leave()
        logger.info(f"サーバー {guild.name} (ID: {guild.id}) から退出しました。理由: {reason}")
    except discord.Forbidden:
        logger.error(f"サーバー {guild.name} (ID: {guild.id}) からの退出またはメッセージ送信に必要な権限がありません。")
    except discord.HTTPException as e:
        logger.error(f"サーバー {guild.name} (ID: {guild.id}) からの退出中にAPIエラーが発生しました: {e}")
    except Exception as e:
         logger.error(f"サーバー {guild.name} からの退出処理中に予期せぬエラー: {e}", exc_info=True)

# --- Botコマンド ---
@bot.command(name="setup", help="DM専用: Twitter追跡の設定（Cookie, 追跡対象, Webhook URL）を行います。既存の設定は上書きされます。")
@commands.dm_only()
async def setup_tracking(ctx):
    """ ユーザーごとにTwitter追跡の設定を行うコマンド """
    user_id = ctx.author.id
    # 既存の設定を読み込む (なければ空の辞書)
    user_data = load_user_data(user_id) or {}
    # 既存設定がある場合、上書きされることを通知しても良い (任意)
    # if user_data:
    #    await ctx.send("既存の設定が見つかりました。新しい設定で上書きします。")

    temp_client = None # スクリーンネーム検索用

    # === ステップ1: Cookie情報の入力 ===
    await ctx.send("1/3: Twitter Cookie の JSON ファイルを添付、またはJSON文字列を直接貼り付けてください。\n"
                   "(EditThisCookie拡張機能推奨)\n"
                   "**警告:** Cookie情報は機密情報です。信頼できる場合にのみ提供してください。")

    def check_cookie(message):
        # メッセージがコマンド実行者本人からで、現在のDMチャンネルに送られ、
        # ファイル添付があるか、テキスト内容があるかを確認
        return message.author == ctx.author and message.channel == ctx.channel and (message.attachments or message.content)

    try:
        cookie_msg = await bot.wait_for('message', check=check_cookie, timeout=300.0) # 5分待つ
        cookie_data = None

        # ファイル添付の場合
        if cookie_msg.attachments:
            try:
                attached_file = cookie_msg.attachments[0]
                if not attached_file.filename.endswith('.json'):
                    await ctx.send("エラー: JSONファイル形式で添付してください。")
                    return
                cookie_bytes = await attached_file.read()
                # EditThisCookie形式のリストからtwikit用の辞書に変換
                raw_cookie_list = json.loads(cookie_bytes.decode('utf-8'))
                cookie_data = {item['name']: item['value'] for item in raw_cookie_list if 'name' in item and 'value' in item}
            except Exception as e:
                await ctx.send(f"エラー: Cookieファイルの処理中に問題が発生しました。\n```{e}```")
                logger.error(f"Cookieファイル処理エラー (ユーザー: {user_id}): {e}")
                return
        # テキスト直接入力の場合
        else:
            try:
                 # JSON文字列として解釈し、twikit用の辞書に変換
                raw_cookie_list = json.loads(cookie_msg.content)
                cookie_data = {item['name']: item['value'] for item in raw_cookie_list if 'name' in item and 'value' in item}
            except json.JSONDecodeError:
                await ctx.send("エラー: 無効なJSON形式です。EditThisCookieからエクスポートした形式で貼り付けてください。")
                return
            except Exception as e:
                await ctx.send(f"エラー: Cookieデータの処理中に問題が発生しました。\n```{e}```")
                logger.error(f"Cookieデータ処理エラー (ユーザー: {user_id}): {e}")
                return

        # Cookieデータが取得できたか最終確認
        if not cookie_data:
            await ctx.send("エラー: 有効なCookie情報が取得できませんでした。")
            return

        # Cookieを暗号化して一時保持 (まだファイルには保存しない)
        encrypted_cookies_temp = encrypt_data(cookie_data)
        await ctx.send("Cookie情報を受け取りました。")

        # 一時的にtwikitクライアントを初期化し、Cookieの有効性を軽くチェック
        try:
            temp_client = Client('ja')
            temp_client.set_cookies(cookie_data)
            logger.info(f"ユーザー {user_id} の一時twikitクライアントを初期化")
        except Exception as e:
            await ctx.send(f"エラー: Cookie情報でTwitterに接続できませんでした。\n```{e}```\nCookie情報が正しいか確認してください。")
            logger.error(f"ユーザー {user_id} のCookieでの一時クライアント初期化失敗: {e}")
            return

    except asyncio.TimeoutError:
        await ctx.send("タイムアウトしました (5分)。もう一度 `!setup` を実行してください。")
        return
    except Exception as e: # その他の予期せぬエラー
        await ctx.send(f"予期せぬエラーが発生しました (Cookie処理中): {e}")
        logger.error(f"ユーザー {user_id} のCookie処理中エラー: {e}", exc_info=True)
        return

    # === ステップ2: 追跡対象のTwitterスクリーンネーム入力 ===
    target_user_id = None
    target_screen_name = None
    if temp_client: # Cookieチェックが成功した場合のみ続行
        await ctx.send("2/3: 追跡したいTwitterユーザーの **スクリーンネーム** を入力してください。(例: `@twitter` または `twitter`)")

        def check_screen_name(message):
            # メッセージが本人からで、テキスト入力があるか
            return message.author == ctx.author and message.channel == ctx.channel and message.content

        try:
            target_name_msg = await bot.wait_for('message', check=check_screen_name, timeout=120.0) # 2分待つ
            # 入力から@を除去し、前後の空白を削除
            input_screen_name = target_name_msg.content.strip().lstrip('@')

            if not input_screen_name:
                await ctx.send("エラー: スクリーンネームが入力されていません。")
                return

            await ctx.send(f"`{input_screen_name}` の情報を検索します...")
            try:
                # スクリーンネームからユーザー情報を取得
                twitter_user = await temp_client.get_user_by_screen_name(input_screen_name)
                if twitter_user:
                    target_user_id = twitter_user.id
                    target_screen_name = twitter_user.screen_name # APIから取得した正式な名前を使う
                    await ctx.send(f"ユーザーが見つかりました！\n"
                                   f"スクリーンネーム: `@{target_screen_name}`\n"
                                   f"ユーザーID: `{target_user_id}`\n"
                                   "このユーザーを追跡対象として設定します。")
                    # ユーザーデータ辞書を更新 (まだファイル保存はしない)
                    user_data['target_twitter_id'] = target_user_id
                    user_data['target_screen_name'] = target_screen_name
                else:
                    # twikitがNoneを返す稀なケース
                    await ctx.send(f"エラー: `{input_screen_name}` というユーザーが見つかりませんでした (API応答が空)。")
                    return

            except twikit_errors.UserNotFound:
                await ctx.send(f"エラー: `{input_screen_name}` というスクリーンネームのユーザーが見つかりませんでした。入力内容を確認してください。")
                return
            except twikit_errors.HTTPException as e:
                 await ctx.send(f"エラー: Twitter APIへのアクセス中に問題が発生しました。\n```{e}```\nしばらく待つか、Cookie情報が有効か確認してください。")
                 return
            except Exception as e:
                await ctx.send(f"予期せぬエラーが発生しました (ユーザー情報取得中): {e}")
                return

        except asyncio.TimeoutError:
            await ctx.send("タイムアウトしました (2分)。もう一度 `!setup` を実行してください。")
            return
    else:
        # temp_clientの初期化に失敗した場合
        await ctx.send("エラー: Cookie情報の検証に失敗したため、処理を中断しました。")
        return

    # === ステップ3: Webhook URLの入力 ===
    if target_user_id: # スクリーンネーム検索が成功した場合のみ続行
        await ctx.send("3/3: 通知を送りたいDiscord WebhookのURLを入力してください。")

        def check_webhook(message):
            # メッセージが本人からで、テキスト入力があり、URL形式が正しいか
            is_correct_author_channel = message.author == ctx.author and message.channel == ctx.channel
            is_valid_url_format = message.content.startswith("https://discord.com/api/webhooks/") or \
                                  message.content.startswith("https://discordapp.com/api/webhooks/")
            return is_correct_author_channel and is_valid_url_format

        try:
            # 正しい形式のURLが入力されるまでループ
            while True:
                webhook_msg = await bot.wait_for('message', check=lambda m: m.author == ctx.author and m.channel == ctx.channel, timeout=120.0) # 2分待つ

                # check_webhook関数で形式を検証
                if check_webhook(webhook_msg):
                    user_data['webhook_url'] = webhook_msg.content # ユーザーデータ辞書を更新
                    await ctx.send("Webhook URLを設定しました。")
                    break # 正しい形式なのでループを抜ける
                else:
                    await ctx.send("エラー: 無効なWebhook URL形式です。`https://discord.com/api/webhooks/...` で始まるURLを入力してください。")
                    # 再度入力を待つ

        except asyncio.TimeoutError:
            await ctx.send("タイムアウトしました (2分)。もう一度 `!setup` を実行してください。")
            return
        except Exception as e:
             await ctx.send(f"予期せぬエラーが発生しました (Webhook URL処理中): {e}")
             return

    # === ステップ4: 全て成功したら設定をファイルに保存 ===
    # 必要なキーが全て user_data に存在するか確認
    if all(k in user_data for k in ['target_twitter_id', 'target_screen_name', 'webhook_url']):
        # 最初に一時保持した暗号化Cookieを user_data に追加
        user_data['encrypted_cookies'] = encrypted_cookies_temp
        # 追跡を有効にするフラグを設定
        user_data['enabled'] = True
        # 既読ツイートリストを初期化 (空にする)
        user_data['seen_tweet_ids'] = []

        # ここで初めてファイルに保存（上書き）する
        save_user_data(user_id, user_data)
        await ctx.send(f"設定が完了しました！ `@ {target_screen_name}` のツイート追跡を開始します。\n"
                       f"追跡を一時停止/再開するには `!track_toggle` コマンドを使用してください。")
        logger.info(f"ユーザー {user_id} の設定を保存/更新しました (Target: @{target_screen_name}, ID: {target_user_id})。")
    else:
         # 通常ここには到達しないはずだが、念のため
         await ctx.send("エラー: 設定の途中で必要な情報が不足したため、保存できませんでした。")
         logger.error(f"ユーザー {user_id} のセットアップフローが完了しませんでした（キー不足）。")


@bot.command(name="track_toggle", help="DM専用: Twitter追跡の有効/無効を切り替えます。")
@commands.dm_only()
async def track_toggle(ctx):
    """ ユーザーのTwitter追跡設定の有効/無効を切り替えるコマンド """
    user_id = ctx.author.id
    user_data = load_user_data(user_id)

    # 設定がまだ存在しない場合
    if not user_data:
        await ctx.send("設定がまだありません。`!setup` コマンドで設定してください。")
        return

    # 'enabled' フラグを反転させる (存在しない場合はデフォルト False として扱う)
    user_data['enabled'] = not user_data.get('enabled', False)
    save_user_data(user_id, user_data) # 変更を保存

    status = "有効" if user_data['enabled'] else "無効"
    # 追跡対象のスクリーンネームがあれば表示に加える
    target_info = f"(@{user_data.get('target_screen_name', 'N/A')})" if 'target_screen_name' in user_data else ""
    await ctx.send(f"Twitter追跡 {target_info} を **{status}** にしました。")
    logger.info(f"ユーザー {user_id} の追跡ステータスを {status} に変更しました。")


@bot.command(name="checknow", help="DM専用: 手動でツイートチェックを実行します。(Botオーナー用)")
@commands.dm_only()
@commands.is_owner() # Botのオーナーのみ実行可能にするデコレータ
async def check_now_command(ctx):
    """ 手動でツイートチェックを実行するコマンド (デバッグ用) """
    user_id = ctx.author.id
    user_data = load_user_data(user_id)

    # 設定が存在し、かつ有効になっているかチェック
    if not user_data:
        await ctx.send("設定がありません。`!setup` で設定してください。")
        return
    if not user_data.get('enabled'):
         await ctx.send("追跡が無効になっています。`!track_toggle` で有効にしてください。")
         return

    await ctx.send("手動でツイートチェックを実行します...")
    try:
        await check_tweets_for_user(user_id, user_data)
        await ctx.send("チェックが完了しました。")
    except Exception as e:
        await ctx.send(f"チェック中にエラーが発生しました。\n```{e}```")
        logger.error(f"ユーザー {user_id} の手動チェック中にエラー", exc_info=True)


# --- 定期実行タスク ---
@tasks.loop(minutes=15) # 15分ごとに tweet_checker_loop を実行
async def tweet_checker_loop():
    """ 定期的に全ユーザーのツイートをチェックするタスク """
    logger.info("定期ツイートチェックを開始...")
    # user_data ディレクトリ内の全 .json ファイルを取得
    user_files = [f for f in os.listdir(USER_DATA_DIR) if f.endswith('.json')]

    active_tasks = [] # 非同期実行するタスクを格納するリスト
    for filename in user_files:
        try:
            user_id = int(filename.split('.')[0]) # ファイル名からユーザーIDを取得
            user_data = load_user_data(user_id)

            # ユーザーデータがあり、追跡が有効になっている場合のみ処理
            if user_data and user_data.get('enabled'):
                logger.info(f"ユーザー {user_id} (Target: @{user_data.get('target_screen_name', 'N/A')}) のチェックタスクを作成します...")
                # check_tweets_for_user を非同期タスクとして作成しリストに追加
                active_tasks.append(asyncio.create_task(check_tweets_for_user(user_id, user_data)))
            else:
                # スキップする場合のログ（デバッグレベル）
                logger.debug(f"ユーザー {user_id} は無効またはデータなしのためスキップします。")
        except ValueError:
            logger.warning(f"無効なファイル名形式です: {filename}。スキップします。")
        except Exception as e:
             logger.error(f"ユーザー {filename} の処理準備中にエラー: {e}", exc_info=True)


    # 作成したタスクが1つでもあれば実行
    if active_tasks:
        logger.info(f"{len(active_tasks)} 件のチェックタスクを実行します。")
        # asyncio.gather で全てのタスクが完了するのを待つ
        # return_exceptions=True にすると、タスク内で例外が発生しても他のタスクは中断されず、gatherが例外を返す
        results = await asyncio.gather(*active_tasks, return_exceptions=True)
        # 結果のログ（エラーチェックなど）
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"チェックタスク {i} でエラーが発生しました: {result}", exc_info=result)
        logger.info("全チェックタスク完了。")
    else:
        logger.info("チェック対象の有効なユーザーがいません。")

    logger.info("定期ツイートチェック完了。")


@tweet_checker_loop.before_loop
async def before_tweet_checker_loop():
    """ 定期実行タスクが開始される前に一度だけ呼ばれる """
    # Botが完全に起動し、Discordに接続するまで待機
    await bot.wait_until_ready()
    logger.info("ツイートチェッカーループの準備完了。")


# --- ツイートチェック処理 (メインロジック) ---
async def check_tweets_for_user(user_id, user_data):
    """ 特定のユーザーの追跡対象ツイートをチェックし、Webhookに通知する """
    # 必要な設定キーが揃っているか確認
    required_keys = ['encrypted_cookies', 'target_twitter_id', 'webhook_url', 'target_screen_name']
    if not all(k in user_data for k in required_keys):
        logger.warning(f"ユーザー {user_id} の設定が不完全です。スキップします。")
        # 設定不備の場合は追跡を無効にする (設定ミスのループを防ぐため)
        user_data['enabled'] = False
        save_user_data(user_id, user_data)
        logger.info(f"ユーザー {user_id} の設定不備のため追跡を無効にしました。")
        return

    # 設定値を変数に格納
    webhook_url = user_data['webhook_url']
    target_twitter_id = user_data['target_twitter_id']
    target_screen_name = user_data['target_screen_name']
    seen_tweet_ids = set(user_data.get('seen_tweet_ids', [])) # 既読IDリスト (Setに変換)
    encrypted_cookies = user_data['encrypted_cookies']

    # === Cookieの復号化 ===
    decrypted_cookies = decrypt_data(encrypted_cookies)
    if not decrypted_cookies or not isinstance(decrypted_cookies, dict):
        logger.error(f"ユーザー {user_id} のCookieの復号化に失敗しました。")
        # ユーザーに通知し、追跡を無効化
        await notify_user_and_disable(user_id, user_data,
                                      f"Twitter追跡 (@{target_screen_name}) の認証情報(Cookie)の読み込みに失敗しました。"
                                      "`!setup` で再設定してください。追跡は無効になりました。")
        return

    client = None # twikitクライアントの初期化
    try:
        # === twikitクライアントの初期化と設定 ===
        client = Client('ja') # 日本語モードで初期化
        client.set_cookies(decrypted_cookies) # 復号化したCookieを設定
        logger.info(f"ユーザー {user_id} の twikit クライアントを初期化しました。")

        # === 検索範囲の設定 ===
        MINUTES_THRESHOLD = 30 # 何分前までのツイートを検索対象とするか
        current_time = datetime.datetime.now(ZoneInfo("UTC")) # 現在のUTC時間
        time_threshold = current_time - datetime.timedelta(minutes=MINUTES_THRESHOLD) # 検索開始時間
        logger.info(f"[User: {user_id} Target: @{target_screen_name}] 検索範囲: {time_threshold} から {current_time} (UTC)")

        # === Twitter APIからの情報取得 ===
        user_info = None # 追跡対象のユーザー情報
        tweets = None    # 取得したツイートのリスト
        try:
            # まず追跡対象のユーザー情報をIDから取得 (アイコンや名前表示のため)
            user_info = await client.get_user_by_id(target_twitter_id)
            if not user_info:
                 logger.warning(f"[User: {user_id}] ターゲットユーザーID {target_twitter_id} (@{target_screen_name}) の情報取得に失敗しました。")
                 await notify_user_and_disable(user_id, user_data, f"追跡対象 @{target_screen_name} が見つかりません。アカウントが存在しないか、アクセス制限されている可能性があります。")
                 return

            # ユーザーのツイートを取得 (取得件数は適宜調整)
            TWEET_COUNT = 150 # 一度に取得するツイート数 (元のコードに近い値)
            tweets = await client.get_user_tweets(target_twitter_id, 'Tweets', count=TWEET_COUNT)
            logger.debug(f"[User: {user_id}] Got {len(tweets) if tweets else 0} tweets from API.")

        except twikit_errors.UserNotFound:
             logger.error(f"[User: {user_id}] ターゲットユーザーID {target_twitter_id} (@{target_screen_name}) が見つかりませんでした (UserNotFound)。")
             await notify_user_and_disable(user_id, user_data, f"追跡対象 @{target_screen_name} が見つかりません。")
             return
        except twikit_errors.HTTPException as e:
             logger.error(f"[User: {user_id} Target: @{target_screen_name}] Twitter API HTTPエラー: {e}")
             # 認証エラーの場合、ユーザーに通知して無効化
             if "authenticate" in str(e).lower() or "401" in str(e) or "403" in str(e):
                 logger.error(f"ユーザー {user_id} の認証に失敗した可能性があります。Cookieを確認してください。")
                 await notify_user_and_disable(user_id, user_data, f"Twitter追跡 (@{target_screen_name}) の認証に失敗しました。Cookieが無効になった可能性があります。`!setup` で再設定してください。")
             # 他のHTTPエラー(レートリミット等)は今回は中断のみ
             return
        except Exception as e:
             logger.error(f"[User: {user_id} Target: @{target_screen_name}] Twitterデータ取得中に予期せぬエラー: {e}", exc_info=True)
             return # 不明なエラー時は処理を中断

        # === 新規ツイートのフィルタリング ===
        recent_tweets = [] # 通知対象の新規ツイートリスト
        all_current_ids_in_window = set() # 今回の検索範囲で見つかった全ツイートID (RT除く)

        if not tweets:
             logger.info(f"[User: {user_id} Target: @{target_screen_name}] ターゲットユーザーからツイートを取得できませんでした (API応答が空)。")
             # seen_tweet_ids は変更しない
             return

        # 取得したツイートをループ処理
        for tweet in tweets:
            try:
                tweet_time = tweet.created_at_datetime # ツイート時刻 (UTC)
                # 検索範囲内のツイートか？
                if tweet_time >= time_threshold:
                    # RTではないか？ (大文字小文字無視)
                    if re.match(r'RT @.*', tweet.text, re.IGNORECASE) is None:
                        all_current_ids_in_window.add(tweet.id) # 検索範囲内のIDとして記録
                        # まだ通知していないツイートか？ (既読リストにないか)
                        if tweet.id not in seen_tweet_ids:
                            recent_tweets.append(tweet)
                            logger.info(f"[User: {user_id} Target: @{target_screen_name}] 新規ツイート検出: ID={tweet.id}")
                else:
                    # 時系列で取得される前提なので、これ以上古いものは見なくて良い
                    logger.debug(f"[User: {user_id} Target: @{target_screen_name}] 古いツイートのためチェック終了: ID={tweet.id}")
                    break # ループを抜ける
            except Exception as e:
                 # 個別ツイート処理中のエラーはログに残してスキップ
                 logger.error(f"[User: {user_id} Target: @{target_screen_name}] 個別ツイート処理中にエラー (ID: {getattr(tweet, 'id', 'N/A')}): {e}")
                 continue

        # === Webhook通知処理 ===
        # 新規ツイートを古い順にソート
        recent_tweets.sort(key=lambda x: x.created_at_datetime)
        newly_posted_ids = set() # 今回Webhookに送信成功したIDのセット

        # 新規ツイートをループしてWebhookに送信
        for tweet in recent_tweets:
            try:
                # user_info が取得できていることを前提とする (上でチェック済み)
                tweet_url = f"https://twitter.com/{getattr(user_info, 'screen_name', '')}/status/{tweet.id}"

                image_urls_for_embed = [] # Embedに画像として表示するURLのリスト
                media_links = [] # 動画/GIFのexpanded_urlを格納するリスト

                if tweet.media:
                    logger.debug(f"[User: {user_id}] Processing {len(tweet.media)} media items for Tweet ID: {tweet.id}")
                    for idx, medium in enumerate(tweet.media):
                        medium_type = getattr(medium, 'type', 'unknown')
                        media_url = getattr(medium, 'media_url', None) # 共通で取得しておく

                        logger.debug(f"  - Media {idx+1}: Type='{medium_type}', Media URL: {media_url}")

                        if medium_type == 'photo':
                            # Photoオブジェクトから表示可能なURLを取得
                            # 優先度: url > media_url
                            photo_url = getattr(medium, media_url, None)
                            if not photo_url:
                                photo_url = getattr(medium, 'media_url', None)

                            if photo_url:
                                image_urls_for_embed.append(photo_url)
                                logger.debug(f"    > Photo URL added for embed: {photo_url}")
                            else:
                                logger.warning(f"[User: {user_id}] Direct Photo URL not found for media {idx+1} in Tweet ID: {tweet.id}.")
                                # 画像URLが見つからない場合でも、expanded_urlがあればリンクとして追加
                                if media_url:
                                     media_links.append(f"[画像を見る {idx+1}]({media_url})")

                        elif medium_type in ['video', 'animated_gif']:
                            logger.debug(f"    > Video/GIF detected.")
                            # expanded_url があればリンクを追加
                            if media_url:
                                link_text = "動画" if medium_type == 'video' else "GIF"
                                media_links.append(f"[{link_text}を見る {idx+1}]({media_url})")
                            else:
                                logger.warning(f"[User: {user_id}] Expanded URL not found for {medium_type} media {idx+1} in Tweet ID: {tweet.id}.")
                                # expanded_url がない場合はテキストで示す
                                link_text = "動画あり" if medium_type == 'video' else "GIFあり"
                                media_links.append(f"({link_text} {idx+1})") # リンクなしのテキスト

                        else:
                             logger.warning(f"[User: {user_id}] Unknown media type '{medium_type}' for media {idx+1} in Tweet ID: {tweet.id}")
                             # 不明なタイプでも expanded_url があればリンクを追加
                             if media_url:
                                 media_links.append(f"[メディアを見る {idx+1}]({media_url})")

                # (Embed作成ロジックは変更なし、user_info を使う)
                embeds = []
                description_text = tweet.text
                if tweet.quote:
                    quote_text = tweet.quote.text[:200] + "..." if len(tweet.quote.text) > 200 else tweet.quote.text
                    quote_url = f"https://twitter.com/{tweet.quote.user.screen_name}/status/{tweet.quote.id}"
                    description_text += f"\n\n> **引用元:** [{quote_text}]({quote_url})"

                if media_links:
                    description_text += "\n\n" + "\n".join(media_links) # 各リンクを改行で結合

                first_embed = {
                    "url": tweet_url,
                    "color": 0x1DA1F2,
                    "timestamp": tweet.created_at_datetime.isoformat(),
                    "title": f"@{getattr(user_info, 'screen_name', '')} の新しいツイート",
                    "description": description_text, # メディアリンクを含んだ説明文
                    "footer": {"text": f"Twitter @{getattr(user_info, 'screen_name', '')}"},
                    "fields": [
                        {"name": "いいね", "value": str(tweet.favorite_count), "inline": True},
                        {"name": "リツイート", "value": str(tweet.retweet_count), "inline": True},
                    ],
                    "author": {
                        "name": getattr(user_info, 'name', 'Unknown User'),
                        "url": f"https://twitter.com/{getattr(user_info, 'screen_name', '')}",
                        "icon_url": getattr(user_info, 'profile_image_url', None)
                    }
                }

                # 最初の「画像」があれば1つ目のEmbedに追加
                if image_urls_for_embed:
                    first_embed["image"] = {"url": image_urls_for_embed[0]}
                embeds.append(first_embed)
                

                

                
                payload = {"embeds": embeds}

                response = requests.post(
                    webhook_url,
                    data=json.dumps(payload).encode('utf-8'), # UTF-8でエンコード
                    headers={"Content-Type": "application/json"}
                )

                # レートリミット対応 (429 Too Many Requests)
                if response.status_code == 429:
                    retry_after = response.json().get('retry_after', 5) # 待機秒数取得 (デフォルト5秒)
                    logger.warning(f"[User: {user_id}] Webhook Rate Limited. Waiting {retry_after} seconds...")
                    await asyncio.sleep(retry_after + 1) # 指定秒数+α待機
                    # 再度送信を試みる
                    response = requests.post(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={"Content-Type": "application/json"})

                # 送信結果の判定とログ出力
                if 200 <= response.status_code < 300: # 成功 (通常 204 No Content)
                    logger.info(f"[User: {user_id} Target: @{target_screen_name}] Webhook送信成功 (Tweet ID: {tweet.id}, Status: {response.status_code})")
                    newly_posted_ids.add(tweet.id) # 成功したIDを記録
                else: # 送信失敗
                    logger.error(f"[User: {user_id} Target: @{target_screen_name}] Webhook送信失敗 (Tweet ID: {tweet.id}) - Status: {response.status_code} - Response: {response.text}")
                    # 無効なWebhook URLなどの場合、ユーザーに通知して無効化
                    if response.status_code in [400, 401, 404]: # Bad Request, Unauthorized, Not Found
                         logger.error(f"Webhook URLが無効か権限がない可能性があります。URL: {webhook_url}")
                         await notify_user_and_disable(user_id, user_data, f"指定されたWebhook URLへの送信に失敗しました (コード: {response.status_code})。URLが正しいか確認してください。追跡は無効になりました。")
                         # このユーザーの残りのツイート処理は中断
                         return # check_tweets_for_user 関数を抜ける

                # API負荷軽減のための短い待機
                await asyncio.sleep(1)

            except Exception as e:
                # Embed作成/Webhook送信ループ中のエラーはログに残して次のツイートへ
                logger.error(f"[User: {user_id} Target: @{target_screen_name}] Embed作成/Webhook送信中エラー (Tweet ID: {getattr(tweet, 'id', 'N/A')}): {e}", exc_info=True)
                continue
        # --- Webhook通知処理ループここまで ---


        # --- 既読ツイートIDリストの更新 ---
        # 今回の検索範囲で見つかった全非RTツイートIDと、これまでの既読IDを結合
        combined_seen_ids = all_current_ids_in_window.union(seen_tweet_ids)
        # 保存するID数の上限
        MAX_SEEN_IDS = 200
        # IDを数値としてソートし、新しい方から上限数だけ残す
        # (IDが数値でない可能性も考慮)
        updated_seen_ids_list = sorted(
            list(combined_seen_ids),
            key=lambda x: int(x) if x.isdigit() else 0,
            reverse=True
        )[:MAX_SEEN_IDS]

        # 既読リストに変更があった場合のみファイルを更新
        if set(user_data.get('seen_tweet_ids', [])) != set(updated_seen_ids_list):
             user_data['seen_tweet_ids'] = updated_seen_ids_list
             save_user_data(user_id, user_data)
             logger.info(f"[User: {user_id} Target: @{target_screen_name}] 既読ツイートIDリスト更新。新規投稿: {len(newly_posted_ids)}件, 更新後既読数: {len(updated_seen_ids_list)}件")
        else:
             logger.debug(f"[User: {user_id} Target: @{target_screen_name}] 既読ツイートIDリストに変更なし。新規投稿: {len(newly_posted_ids)}件")


    except Exception as e:
        # check_tweets_for_user 関数全体の予期せぬエラー
        logger.error(f"ユーザー {user_id} (Target: @{target_screen_name}) のチェック処理全体で予期せぬエラー: {e}", exc_info=True)
    finally:
        # twikitクライアントの後処理 (必要なら)
        # if client and hasattr(client, 'close'): await client.close()
        pass


# --- 追跡無効化とユーザー通知用のヘルパー関数 ---
async def notify_user_and_disable(user_id, user_data, message):
    """ エラー発生時にユーザーにDMを送り、追跡を無効化する共通関数 """
    target_screen_name = user_data.get('target_screen_name', 'N/A') # 通知メッセージ用に取得
    logger.warning(f"ユーザー {user_id} (Target: @{target_screen_name}) の追跡を無効化します。理由: {message}")
    try:
        user = await bot.fetch_user(user_id) # Discordユーザーオブジェクト取得
        # エラーメッセージをDMで送信
        await user.send(f"【Twitter追跡エラー通知 (@{target_screen_name})】\n{message}")
    except (discord.Forbidden, discord.NotFound) as e:
        # DM送信に失敗した場合 (ブロックされている、ユーザーが存在しないなど)
        logger.warning(f"ユーザー {user_id} へのエラーDM送信失敗: {e}")

    # ユーザーデータ内の 'enabled' フラグを False に設定
    user_data['enabled'] = False
    save_user_data(user_id, user_data) # 変更をファイルに保存


# --- Bot実行 ---
if __name__ == "__main__":
    # 起動前に必須の環境変数が設定されているか最終チェック
    if not DISCORD_BOT_TOKEN:
        print("致命的エラー: DISCORD_BOT_TOKENが設定されていません。")
    elif not ENCRYPTION_KEY:
         print("致命的エラー: ENCRYPTION_KEYが設定されていません。")
    else:
        try:
             # Botを起動
             keep_alive()
             bot.run(DISCORD_BOT_TOKEN)
        except discord.LoginFailure:
             print("致命的エラー: 不正なDiscord Botトークンです。トークンを確認してください。")
        except discord.errors.PrivilegedIntentsRequired:
              print("致命的エラー: 必要な特権インテントがDiscord Developer Portalで有効になっていません。")
              print("=> Message Content Intent と Server Members Intent を有効にしてください。")
        except Exception as e:
             # その他の予期せぬエラーでBotが起動できない場合
             print(f"Bot実行中に予期せぬエラーが発生しました: {e}")
             # traceback.print_exc() # 詳細なトレースバックが必要な場合
