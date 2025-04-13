# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
from twikit import Client, errors as twikit_errors
import datetime
from zoneinfo import ZoneInfo
import requests
import re
import logging
from keep_alive import keep_alive
import pymongo # pymongo をインポート

# --- 初期設定 ---
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
MONGODB_URI = os.getenv("MONGODB_URI") # MongoDB 接続文字列
DB_NAME = os.getenv("DB_NAME", "discordBotData") # データベース名 (デフォルト値設定)
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "userSettings") # コレクション名 (デフォルト値設定)

# --- 環境変数チェック ---
if not DISCORD_BOT_TOKEN or not ENCRYPTION_KEY:
    raise ValueError("DISCORD_BOT_TOKEN と ENCRYPTION_KEY を環境変数に設定してください。")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI を環境変数に設定してください。")

try:
    fernet = Fernet(ENCRYPTION_KEY.encode())
except ValueError:
    raise ValueError("ENCRYPTION_KEYが無効な形式です。")
# --- ログレベル設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
# --- MongoDB 接続 ---
try:
    mongo_client = pymongo.MongoClient(MONGODB_URI)
    # 接続テスト (オプションだが推奨)
    mongo_client.admin.command('ping')
    logger.info("MongoDB に正常に接続しました。")
    db = mongo_client[DB_NAME]
    user_collection = db[COLLECTION_NAME]
    # インデックス作成 (user_id での検索を高速化、存在しない場合のみ作成)
    user_collection.create_index([("enabled", pymongo.ASCENDING)])
    logger.info(f"MongoDB: Database='{DB_NAME}', Collection='{COLLECTION_NAME}' を使用します。")
except pymongo.errors.ConfigurationError as e:
    logger.error(f"MongoDB 接続文字列が無効です: {e}")
    raise
except pymongo.errors.ConnectionFailure as e:
    logger.error(f"MongoDB への接続に失敗しました: {e}")
    # 必要に応じてここで終了させるか、リトライ処理を入れる
    raise
except Exception as e: # その他の pymongo 関連エラー
    logger.error(f"MongoDB 設定中に予期せぬエラー: {e}")
    raise




# USER_DATA_DIR と os.makedirs は不要になったのでコメントアウトまたは削除
# USER_DATA_DIR = ".data"
# os.makedirs(USER_DATA_DIR, exist_ok=True)

# --- データ管理関数 (MongoDB版) ---
def load_user_data(user_id):
    """ MongoDB からユーザーIDに対応する設定を読み込む """
    try:
        # find_one は見つからない場合 None を返す
        # _id フィールドは自動で含まれるが、ここでは気にしない
        # user_id を _id として使用する
        user_data = user_collection.find_one({"_id": user_id})
        if user_data:
             logger.debug(f"MongoDBからユーザー {user_id} のデータを読み込みました。")
             return user_data
        else:
             logger.debug(f"MongoDBにユーザー {user_id} のデータは見つかりませんでした。")
             return None
    except Exception as e:
        logger.error(f"MongoDBからのデータ読み込み中にエラー (User ID: {user_id}): {e}")
        return None # エラー時はNoneを返す

def save_user_data(user_id, data):
    """ MongoDB にユーザーIDに対応する設定データを保存/更新する """
    try:
        # update_one の第三引数 upsert=True で、
        # filter ({'_id': user_id}) に一致するドキュメントがあれば更新 ($set で指定した内容)、
        # なければ新しいドキュメントとして挿入する。
        # ここでは data 辞書全体をセットする
        result = user_collection.update_one(
            {"_id": user_id}, # フィルター: この user_id のドキュメントを探す
            {"$set": data},   # 更新内容: data 辞書の内容でドキュメントを更新
            upsert=True       # オプション: ドキュメントがなければ挿入する
        )
        if result.upserted_id:
            logger.info(f"MongoDBにユーザー {user_id} のデータを新規保存しました。")
        elif result.modified_count > 0:
            logger.info(f"MongoDBのユーザー {user_id} のデータを更新しました。")
        else:
            # upsert=True なので通常ここには来ないはずだが、念のため
             logger.debug(f"MongoDBのユーザー {user_id} のデータ保存/更新操作が完了しましたが、変更はありませんでした。")
    except Exception as e:
        logger.error(f"MongoDBへのデータ保存中にエラー (User ID: {user_id}): {e}")

# --- 暗号化/復号化関数 (変更なし) ---
def encrypt_data(data):
    # ... (変更なし) ...
    if isinstance(data, dict):
        data_bytes = json.dumps(data).encode('utf-8')
    elif isinstance(data, str):
        data_bytes = data.encode('utf-8')
    else:
        raise TypeError("暗号化できるのは dict または str のみです。")
    return fernet.encrypt(data_bytes).decode('utf-8') # 保存用に文字列にする

def decrypt_data(encrypted_data):
    # ... (変更なし) ...
    try:
        decrypted_bytes = fernet.decrypt(encrypted_data.encode('utf-8'))
        try:
            return json.loads(decrypted_bytes.decode('utf-8'))
        except json.JSONDecodeError:
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
    # logger.info("ユーザーデータディレクトリ: " + USER_DATA_DIR) # 不要になった
    logger.info("ユーザーデータは MongoDB を使用します。")
    tweet_checker_loop.start() # 定期実行タスクを開始

# --- 許可ユーザーIDリストの読み込み (変更なし) ---
ALLOWED_INVITER_IDS_STR = os.getenv("ALLOWED_INVITER_IDS", "")
# ... (以降の ALLOWED_INVITER_IDS 関連のロジックは変更なし) ...
ALLOWED_INVITER_IDS = set()
if ALLOWED_INVITER_IDS_STR:
    try:
        ALLOWED_INVITER_IDS = {int(uid.strip()) for uid in ALLOWED_INVITER_IDS_STR.split(',') if uid.strip().isdigit()}
        logger.info(f"招待を許可されたユーザーIDリストをロードしました: {ALLOWED_INVITER_IDS}")
    except ValueError:
        logger.error("ALLOWED_INVITER_IDS の形式が無効です。カンマ区切りの数値で指定してください。")
        ALLOWED_INVITER_IDS = set()
else:
    logger.warning("招待許可ユーザーID (ALLOWED_INVITER_IDS) が .env に設定されていません。")
    ALLOWED_INVITER_IDS = set()

# --- on_guild_join イベントハンドラ (変更なし) ---
@bot.event
async def on_guild_join(guild: discord.Guild):
    # ... (変更なし) ...
    logger.info(f"新しいサーバーに参加しました: {guild.name} (ID: {guild.id})")

    if ALLOWED_INVITER_IDS is None:
         logger.info("招待ユーザーチェックはスキップされました (ALLOWED_INVITER_IDS is None)。")
         return
    if not ALLOWED_INVITER_IDS:
         logger.warning(f"招待許可ユーザーリストが空のため、サーバー {guild.name} (ID: {guild.id}) から退出します。")
         await leave_guild(guild, "招待許可ユーザーリストが空です。")
         return

    inviter = None
    try:
        await asyncio.sleep(2)
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
            if entry.target.id == bot.user.id:
                inviter = entry.user
                logger.info(f"サーバー {guild.name} にBotを追加したユーザーを特定しました: {inviter} (ID: {inviter.id})")
                break
        else:
             logger.warning(f"サーバー {guild.name} の監査ログからBotを追加したユーザーを特定できませんでした。")
             await leave_guild(guild, "Botを追加したユーザーを特定できませんでした（監査ログ権限不足または記録なし）。")
             return

    except discord.Forbidden:
        logger.error(f"サーバー {guild.name} (ID: {guild.id}) の監査ログを表示する権限がありません。招待ユーザーチェックを実行できません。")
        await leave_guild(guild, "Botに監査ログの表示権限がないため、招待者を確認できませんでした。")
        return
    except discord.HTTPException as e:
        logger.error(f"監査ログの取得中にAPIエラーが発生しました: {e}")
        await leave_guild(guild, f"監査ログの取得中にエラーが発生しました: {e}")
        return
    except Exception as e:
        logger.error(f"監査ログ処理中に予期せぬエラー: {e}", exc_info=True)
        await leave_guild(guild, f"招待者の確認中に予期せぬエラーが発生しました。")
        return

    if inviter and inviter.id not in ALLOWED_INVITER_IDS:
        logger.warning(f"ユーザー {inviter} (ID: {inviter.id}) は招待許可リストに含まれていません。サーバー {guild.name} から退出します。")
        await leave_guild(guild, f"招待ユーザー ({inviter}) が許可されていません。")
    elif inviter:
        logger.info(f"招待ユーザー {inviter} は許可リストに含まれています。サーバー {guild.name} に留まります。")

# --- leave_guild 関数 (変更なし) ---
async def leave_guild(guild: discord.Guild, reason: str):
    # ... (変更なし) ...
    try:
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
    # MongoDBから既存設定を読み込む
    user_data = load_user_data(user_id) or {}
    # user_data.pop('_id', None) # load_user_data が返す辞書に _id があっても通常は問題ないが、気になるなら削除

    temp_client = None

    # === ステップ1: Cookie情報の入力 (変更なし) ===
    # ... (変更なし) ...
    await ctx.send("1/3: Twitter Cookie の JSON ファイルを添付、またはJSON文字列を直接貼り付けてください。\n"
                   "(EditThisCookie拡張機能推奨)\n"
                   "**警告:** Cookie情報は機密情報です。信頼できる場合にのみ提供してください。")

    def check_cookie(message):
        return message.author == ctx.author and message.channel == ctx.channel and (message.attachments or message.content)

    try:
        cookie_msg = await bot.wait_for('message', check=check_cookie, timeout=300.0)
        cookie_data = None

        if cookie_msg.attachments:
            try:
                attached_file = cookie_msg.attachments[0]
                if not attached_file.filename.endswith('.json'):
                    await ctx.send("エラー: JSONファイル形式で添付してください。")
                    return
                cookie_bytes = await attached_file.read()
                raw_cookie_list = json.loads(cookie_bytes.decode('utf-8'))
                cookie_data = {item['name']: item['value'] for item in raw_cookie_list if 'name' in item and 'value' in item}
            except Exception as e:
                await ctx.send(f"エラー: Cookieファイルの処理中に問題が発生しました。\n```{e}```")
                logger.error(f"Cookieファイル処理エラー (ユーザー: {user_id}): {e}")
                return
        else:
            try:
                raw_cookie_list = json.loads(cookie_msg.content)
                cookie_data = {item['name']: item['value'] for item in raw_cookie_list if 'name' in item and 'value' in item}
            except json.JSONDecodeError:
                await ctx.send("エラー: 無効なJSON形式です。EditThisCookieからエクスポートした形式で貼り付けてください。")
                return
            except Exception as e:
                await ctx.send(f"エラー: Cookieデータの処理中に問題が発生しました。\n```{e}```")
                logger.error(f"Cookieデータ処理エラー (ユーザー: {user_id}): {e}")
                return

        if not cookie_data:
            await ctx.send("エラー: 有効なCookie情報が取得できませんでした。")
            return

        encrypted_cookies_temp = encrypt_data(cookie_data)
        await ctx.send("Cookie情報を受け取りました。")

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
    except Exception as e:
        await ctx.send(f"予期せぬエラーが発生しました (Cookie処理中): {e}")
        logger.error(f"ユーザー {user_id} のCookie処理中エラー: {e}", exc_info=True)
        return

    # === ステップ2: 追跡対象のTwitterスクリーンネーム入力 (変更なし) ===
    target_user_id = None
    target_screen_name = None
    if temp_client:
        # ... (変更なし) ...
        await ctx.send("2/3: 追跡したいTwitterユーザーの **スクリーンネーム** を入力してください。(例: `@twitter` または `twitter`)")

        def check_screen_name(message):
            return message.author == ctx.author and message.channel == ctx.channel and message.content

        try:
            target_name_msg = await bot.wait_for('message', check=check_screen_name, timeout=120.0)
            input_screen_name = target_name_msg.content.strip().lstrip('@')

            if not input_screen_name:
                await ctx.send("エラー: スクリーンネームが入力されていません。")
                return

            await ctx.send(f"`{input_screen_name}` の情報を検索します...")
            try:
                twitter_user = await temp_client.get_user_by_screen_name(input_screen_name)
                if twitter_user:
                    target_user_id = twitter_user.id
                    target_screen_name = twitter_user.screen_name
                    await ctx.send(f"ユーザーが見つかりました！\n"
                                   f"スクリーンネーム: `@{target_screen_name}`\n"
                                   f"ユーザーID: `{target_user_id}`\n"
                                   "このユーザーを追跡対象として設定します。")
                    user_data['target_twitter_id'] = target_user_id
                    user_data['target_screen_name'] = target_screen_name
                else:
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
        await ctx.send("エラー: Cookie情報の検証に失敗したため、処理を中断しました。")
        return

    # === ステップ3: Webhook URLの入力 (変更なし) ===
    if target_user_id:
        # ... (変更なし) ...
        await ctx.send("3/3: 通知を送りたいDiscord WebhookのURLを入力してください。")

        def check_webhook(message):
            is_correct_author_channel = message.author == ctx.author and message.channel == ctx.channel
            is_valid_url_format = message.content.startswith("https://discord.com/api/webhooks/") or \
                                  message.content.startswith("https://discordapp.com/api/webhooks/")
            return is_correct_author_channel and is_valid_url_format

        try:
            while True:
                webhook_msg = await bot.wait_for('message', check=lambda m: m.author == ctx.author and m.channel == ctx.channel, timeout=120.0)

                if check_webhook(webhook_msg):
                    user_data['webhook_url'] = webhook_msg.content
                    await ctx.send("Webhook URLを設定しました。")
                    break
                else:
                    await ctx.send("エラー: 無効なWebhook URL形式です。`https://discord.com/api/webhooks/...` で始まるURLを入力してください。")

        except asyncio.TimeoutError:
            await ctx.send("タイムアウトしました (2分)。もう一度 `!setup` を実行してください。")
            return
        except Exception as e:
             await ctx.send(f"予期せぬエラーが発生しました (Webhook URL処理中): {e}")
             return

    # === ステップ4: 全て成功したら設定をMongoDBに保存 ===
    if all(k in user_data for k in ['target_twitter_id', 'target_screen_name', 'webhook_url']):
        user_data['encrypted_cookies'] = encrypted_cookies_temp
        user_data['enabled'] = True
        user_data['seen_tweet_ids'] = [] # 常に初期化

        # MongoDB に保存
        save_user_data(user_id, user_data)

        await ctx.send(f"設定が完了しました！ `@ {target_screen_name}` のツイート追跡を開始します。\n"
                       f"追跡を一時停止/再開するには `!track_toggle` コマンドを使用してください。")
        logger.info(f"ユーザー {user_id} の設定を保存/更新しました (Target: @{target_screen_name}, ID: {target_user_id})。")
    else:
         await ctx.send("エラー: 設定の途中で必要な情報が不足したため、保存できませんでした。")
         logger.error(f"ユーザー {user_id} のセットアップフローが完了しませんでした（キー不足）。")


@bot.command(name="track_toggle", help="DM専用: Twitter追跡の有効/無効を切り替えます。")
@commands.dm_only()
async def track_toggle(ctx):
    """ ユーザーのTwitter追跡設定の有効/無効を切り替えるコマンド """
    user_id = ctx.author.id
    # MongoDBからデータを読み込む
    user_data = load_user_data(user_id)

    if not user_data:
        await ctx.send("設定がまだありません。`!setup` コマンドで設定してください。")
        return

    # 'enabled' フラグを反転
    user_data['enabled'] = not user_data.get('enabled', False)
    # MongoDB に変更を保存
    save_user_data(user_id, user_data)

    status = "有効" if user_data['enabled'] else "無効"
    target_info = f"(@{user_data.get('target_screen_name', 'N/A')})" if 'target_screen_name' in user_data else ""
    await ctx.send(f"Twitter追跡 {target_info} を **{status}** にしました。")
    logger.info(f"ユーザー {user_id} の追跡ステータスを {status} に変更しました。")


@bot.command(name="checknow", help="DM専用: 手動でツイートチェックを実行します。(Botオーナー用)")
@commands.dm_only()
@commands.is_owner()
async def check_now_command(ctx):
    """ 手動でツイートチェックを実行するコマンド (デバッグ用) """
    user_id = ctx.author.id
    # MongoDBからデータを読み込む
    user_data = load_user_data(user_id)

    if not user_data:
        await ctx.send("設定がありません。`!setup` で設定してください。")
        return
    if not user_data.get('enabled'):
         await ctx.send("追跡が無効になっています。`!track_toggle` で有効にしてください。")
         return

    await ctx.send("手動でツイートチェックを実行します...")
    try:
        # check_tweets_for_user に user_data を渡す
        await check_tweets_for_user(user_id, user_data)
        await ctx.send("チェックが完了しました。")
    except Exception as e:
        await ctx.send(f"チェック中にエラーが発生しました。\n```{e}```")
        logger.error(f"ユーザー {user_id} の手動チェック中にエラー", exc_info=True)


# --- 定期実行タスク (MongoDB版) ---
@tasks.loop(minutes=15)
async def tweet_checker_loop():
    """ 定期的に全ユーザーのツイートをチェックするタスク """
    logger.info("定期ツイートチェックを開始...")
    active_tasks = []
    try:
        # MongoDB から enabled が True のユーザーデータを取得
        enabled_users_cursor = user_collection.find({"enabled": True})
        # カーソルをリストに変換（DB負荷を考慮し、大量ユーザーの場合は注意）
        # enabled_users_list = list(enabled_users_cursor)
        # logger.info(f"{len(enabled_users_list)} 件の有効なユーザーが見つかりました。")

        # カーソルを直接ループする方がメモリ効率が良い場合がある
        user_count = 0
        for user_data in enabled_users_cursor:
            user_id = user_data['_id'] # _id フィールドがユーザーID
            try:
                logger.info(f"ユーザー {user_id} (Target: @{user_data.get('target_screen_name', 'N/A')}) のチェックタスクを作成します...")
                # check_tweets_for_user に user_id と取得した user_data を渡す
                active_tasks.append(asyncio.create_task(check_tweets_for_user(user_id, user_data)))
                user_count += 1
            except Exception as e:
                 logger.error(f"ユーザー {user_id} のタスク作成中にエラー: {e}", exc_info=True)

        logger.info(f"{user_count} 件の有効なユーザーのチェックタスクを作成しました。")

    except Exception as e:
         logger.error(f"MongoDBからの有効ユーザー取得中にエラー: {e}", exc_info=True)
         # エラーが発生した場合、今回はタスクを実行しない
         active_tasks = []

    # --- 以降のタスク実行ロジックは変更なし ---
    if active_tasks:
        logger.info(f"{len(active_tasks)} 件のチェックタスクを実行します。")
        results = await asyncio.gather(*active_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # エラーの詳細情報は check_tweets_for_user 内でログ出力されるはず
                logger.error(f"チェックタスク {i} でエラーが発生しました: {result.__class__.__name__}")
        logger.info("全チェックタスク完了。")
    else:
        logger.info("チェック対象の有効なユーザーがいません。")

    logger.info("定期ツイートチェック完了。")


@tweet_checker_loop.before_loop
async def before_tweet_checker_loop():
    """ 定期実行タスクが開始される前に一度だけ呼ばれる """
    await bot.wait_until_ready()
    logger.info("ツイートチェッカーループの準備完了。")


# --- ツイートチェック処理 (メインロジック) ---
# 引数 user_data を受け取るように変更（すでにそうなっていたが明確化）
async def check_tweets_for_user(user_id, user_data):
    """ 特定のユーザーの追跡対象ツイートをチェックし、Webhookに通知する """
    # 関数開始時に user_data が渡される前提なので、load_user_data は不要

    # 必要な設定キーが揃っているか確認 (user_data を直接チェック)
    required_keys = ['encrypted_cookies', 'target_twitter_id', 'webhook_url', 'target_screen_name']
    if not all(k in user_data for k in required_keys):
        logger.warning(f"ユーザー {user_id} の設定データが不完全です。スキップします。")
        # 設定不備の場合は追跡を無効にする (DB更新)
        user_data['enabled'] = False
        save_user_data(user_id, user_data) # DBに保存
        logger.info(f"ユーザー {user_id} の設定不備のため追跡を無効にしました。")
        return

    # 設定値を変数に格納
    webhook_url = user_data['webhook_url']
    target_twitter_id = user_data['target_twitter_id']
    target_screen_name = user_data['target_screen_name']
    # seen_tweet_ids はリストとして保存されている想定
    seen_tweet_ids = set(user_data.get('seen_tweet_ids', []))
    encrypted_cookies = user_data['encrypted_cookies']

    # --- Cookie復号化 (変更なし) ---
    decrypted_cookies = decrypt_data(encrypted_cookies)
    if not decrypted_cookies or not isinstance(decrypted_cookies, dict):
        logger.error(f"ユーザー {user_id} のCookieの復号化に失敗しました。")
        # ユーザーに通知し、追跡を無効化 (DB更新も含むヘルパー関数呼び出し)
        await notify_user_and_disable(user_id, user_data,
                                      f"Twitter追跡 (@{target_screen_name}) の認証情報(Cookie)の読み込みに失敗しました。"
                                      "`!setup` で再設定してください。追跡は無効になりました。")
        return

    client = None
    try:
        # --- twikit クライアント初期化 (変更なし) ---
        client = Client('ja')
        client.set_cookies(decrypted_cookies)
        logger.info(f"ユーザー {user_id} の twikit クライアントを初期化しました。")

        # --- 検索範囲設定 (変更なし) ---
        MINUTES_THRESHOLD = 30
        current_time = datetime.datetime.now(ZoneInfo("UTC"))
        time_threshold = current_time - datetime.timedelta(minutes=MINUTES_THRESHOLD)
        logger.info(f"[User: {user_id} Target: @{target_screen_name}] 検索範囲: {time_threshold} から {current_time} (UTC)")

        # --- Twitter APIからの情報取得 (エラー時の notify_user_and_disable 呼び出し確認) ---
        user_info = None
        tweets = None
        try:
            user_info = await client.get_user_by_id(target_twitter_id)
            if not user_info:
                 logger.warning(f"[User: {user_id}] ターゲットユーザーID {target_twitter_id} (@{target_screen_name}) の情報取得に失敗しました。")
                 # user_data を渡す
                 await notify_user_and_disable(user_id, user_data, f"追跡対象 @{target_screen_name} が見つかりません。アカウントが存在しないか、アクセス制限されている可能性があります。")
                 return

            TWEET_COUNT = 150
            tweets = await client.get_user_tweets(target_twitter_id, 'Tweets', count=TWEET_COUNT)
            logger.debug(f"[User: {user_id}] Got {len(tweets) if tweets else 0} tweets from API.")

        except twikit_errors.UserNotFound:
             logger.error(f"[User: {user_id}] ターゲットユーザーID {target_twitter_id} (@{target_screen_name}) が見つかりませんでした (UserNotFound)。")
             # user_data を渡す
             await notify_user_and_disable(user_id, user_data, f"追跡対象 @{target_screen_name} が見つかりません。")
             return
        except twikit_errors.HTTPException as e:
             logger.error(f"[User: {user_id} Target: @{target_screen_name}] Twitter API HTTPエラー: {e}")
             if "authenticate" in str(e).lower() or "401" in str(e) or "403" in str(e):
                 logger.error(f"ユーザー {user_id} の認証に失敗した可能性があります。Cookieを確認してください。")
                 # user_data を渡す
                 await notify_user_and_disable(user_id, user_data, f"Twitter追跡 (@{target_screen_name}) の認証に失敗しました。Cookieが無効になった可能性があります。`!setup` で再設定してください。")
             return
        except Exception as e:
             logger.error(f"[User: {user_id} Target: @{target_screen_name}] Twitterデータ取得中に予期せぬエラー: {e}", exc_info=True)
             return

        # --- 新規ツイートのフィルタリング (変更なし) ---
        # ... (変更なし) ...
        recent_tweets = []
        all_current_ids_in_window = set()

        if not tweets:
             logger.info(f"[User: {user_id} Target: @{target_screen_name}] ターゲットユーザーからツイートを取得できませんでした (API応答が空)。")
             # 既読リストは更新しないのでここで return
             return

        for tweet in tweets:
            try:
                tweet_time = tweet.created_at_datetime
                if tweet_time >= time_threshold:
                    if re.match(r'RT @.*', tweet.text, re.IGNORECASE) is None:
                        all_current_ids_in_window.add(tweet.id)
                        if tweet.id not in seen_tweet_ids:
                            recent_tweets.append(tweet)
                            logger.info(f"[User: {user_id} Target: @{target_screen_name}] 新規ツイート検出: ID={tweet.id}")
                else:
                    logger.debug(f"[User: {user_id} Target: @{target_screen_name}] 古いツイートのためチェック終了: ID={tweet.id}")
                    break
            except Exception as e:
                 logger.error(f"[User: {user_id} Target: @{target_screen_name}] 個別ツイート処理中にエラー (ID: {getattr(tweet, 'id', 'N/A')}): {e}")
                 continue

        # --- Webhook通知処理 (エラー時の notify_user_and_disable 呼び出し確認) ---
        recent_tweets.sort(key=lambda x: x.created_at_datetime)
        newly_posted_ids = set()

        for tweet in recent_tweets:
            try:
                # --- Embed作成 (変更なし) ---
                # ... (変更なし) ...
                tweet_url = f"https://twitter.com/{getattr(user_info, 'screen_name', '')}/status/{tweet.id}"
                image_urls_for_embed = []
                media_links = []
                if tweet.media:
                    # ...(メディア処理ロジック変更なし)...
                    logger.debug(f"[User: {user_id}] Processing {len(tweet.media)} media items for Tweet ID: {tweet.id}")
                    for idx, medium in enumerate(tweet.media):
                        medium_type = getattr(medium, 'type', 'unknown')
                        media_url = getattr(medium, 'media_url', None)
                        logger.debug(f"  - Media {idx+1}: Type='{medium_type}', Media URL: {media_url}")
                        if medium_type == 'photo':
                            photo_url = getattr(medium, media_url, None)
                            if not photo_url: photo_url = getattr(medium, 'media_url', None)
                            if photo_url:
                                image_urls_for_embed.append(photo_url)
                                logger.debug(f"    > Photo URL added for embed: {photo_url}")
                            elif media_url: media_links.append(f"[画像を見る {idx+1}]({media_url})")
                        elif medium_type in ['video', 'animated_gif']:
                            logger.debug(f"    > Video/GIF detected.")
                            if media_url:
                                link_text = "動画" if medium_type == 'video' else "GIF"
                                media_links.append(f"[{link_text}を見る {idx+1}]({media_url})")
                            else:
                                logger.warning(f"[User: {user_id}] Expanded URL not found for {medium_type} media {idx+1} in Tweet ID: {tweet.id}.")
                                link_text = "動画あり" if medium_type == 'video' else "GIFあり"
                                media_links.append(f"({link_text} {idx+1})")
                        else:
                             logger.warning(f"[User: {user_id}] Unknown media type '{medium_type}' for media {idx+1} in Tweet ID: {tweet.id}")
                             if media_url: media_links.append(f"[メディアを見る {idx+1}]({media_url})")

                embeds = []
                description_text = tweet.text
                if tweet.quote:
                    quote_text = tweet.quote.text[:200] + "..." if len(tweet.quote.text) > 200 else tweet.quote.text
                    quote_url = f"https://twitter.com/{tweet.quote.user.screen_name}/status/{tweet.quote.id}"
                    description_text += f"\n\n> **引用元:** [{quote_text}]({quote_url})"
                if media_links:
                    description_text += "\n\n" + "\n".join(media_links)

                first_embed = {
                    "url": tweet_url, "color": 0x1DA1F2, "timestamp": tweet.created_at_datetime.isoformat(),
                    "title": f"@{getattr(user_info, 'screen_name', '')} の新しいツイート", "description": description_text,
                    "footer": {"text": f"Twitter @{getattr(user_info, 'screen_name', '')}"},
                    "fields": [{"name": "いいね", "value": str(tweet.favorite_count), "inline": True}, {"name": "リツイート", "value": str(tweet.retweet_count), "inline": True}],
                    "author": {"name": getattr(user_info, 'name', 'Unknown User'), "url": f"https://twitter.com/{getattr(user_info, 'screen_name', '')}", "icon_url": getattr(user_info, 'profile_image_url', None)}
                }
                if image_urls_for_embed: first_embed["image"] = {"url": image_urls_for_embed[0]}
                embeds.append(first_embed)
                payload = {"embeds": embeds}

                # --- Webhook送信 (変更なし、エラー時の処理確認) ---
                response = requests.post(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={"Content-Type": "application/json"})

                if response.status_code == 429:
                    retry_after = response.json().get('retry_after', 5)
                    logger.warning(f"[User: {user_id}] Webhook Rate Limited. Waiting {retry_after} seconds...")
                    await asyncio.sleep(retry_after + 1)
                    response = requests.post(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={"Content-Type": "application/json"})

                if 200 <= response.status_code < 300:
                    logger.info(f"[User: {user_id} Target: @{target_screen_name}] Webhook送信成功 (Tweet ID: {tweet.id}, Status: {response.status_code})")
                    newly_posted_ids.add(tweet.id)
                else:
                    logger.error(f"[User: {user_id} Target: @{target_screen_name}] Webhook送信失敗 (Tweet ID: {tweet.id}) - Status: {response.status_code} - Response: {response.text}")
                    if response.status_code in [400, 401, 404]:
                         logger.error(f"Webhook URLが無効か権限がない可能性があります。URL: {webhook_url}")
                         # user_data を渡す
                         await notify_user_and_disable(user_id, user_data, f"指定されたWebhook URLへの送信に失敗しました (コード: {response.status_code})。URLが正しいか確認してください。追跡は無効になりました。")
                         return # このユーザーの処理を中断

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"[User: {user_id} Target: @{target_screen_name}] Embed作成/Webhook送信中エラー (Tweet ID: {getattr(tweet, 'id', 'N/A')}): {e}", exc_info=True)
                continue
        # --- Webhook通知処理ループここまで ---

        # --- 既読ツイートIDリストの更新 (MongoDBへ保存) ---
        combined_seen_ids = all_current_ids_in_window.union(seen_tweet_ids)
        MAX_SEEN_IDS = 200
        # MongoDB にはリストとして保存する
        updated_seen_ids_list = sorted(
            [str(tid) for tid in combined_seen_ids], # MongoDB互換性のため文字列に統一推奨
             key=lambda x: int(x) if x.isdigit() else 0,
             reverse=True
        )[:MAX_SEEN_IDS]

        # user_data を直接変更し、最後にまとめて保存する
        current_seen_list = user_data.get('seen_tweet_ids', [])
        if set(current_seen_list) != set(updated_seen_ids_list):
             user_data['seen_tweet_ids'] = updated_seen_ids_list
             # ここで save_user_data を呼ぶ
             save_user_data(user_id, user_data)
             logger.info(f"[User: {user_id} Target: @{target_screen_name}] 既読ツイートIDリスト更新 (MongoDB)。新規投稿: {len(newly_posted_ids)}件, 更新後既読数: {len(updated_seen_ids_list)}件")
        else:
             logger.debug(f"[User: {user_id} Target: @{target_screen_name}] 既読ツイートIDリストに変更なし。新規投稿: {len(newly_posted_ids)}件")


    except Exception as e:
        logger.error(f"ユーザー {user_id} (Target: @{target_screen_name}) のチェック処理全体で予期せぬエラー: {e}", exc_info=True)
    finally:
        # twikitクライアントの後処理
        pass


# --- 追跡無効化とユーザー通知用のヘルパー関数 (MongoDB版) ---
# 引数に user_data を追加して、DB保存も行うようにする
async def notify_user_and_disable(user_id, user_data, message):
    """ エラー発生時にユーザーにDMを送り、追跡を無効化してMongoDBに保存する """
    target_screen_name = user_data.get('target_screen_name', 'N/A')
    logger.warning(f"ユーザー {user_id} (Target: @{target_screen_name}) の追跡を無効化します。理由: {message}")
    try:
        user = await bot.fetch_user(user_id)
        await user.send(f"【Twitter追跡エラー通知 (@{target_screen_name})】\n{message}")
    except (discord.Forbidden, discord.NotFound) as e:
        logger.warning(f"ユーザー {user_id} へのエラーDM送信失敗: {e}")

    # ユーザーデータ内の 'enabled' フラグを False に設定
    user_data['enabled'] = False
    # 変更を MongoDB に保存
    save_user_data(user_id, user_data)


# --- Bot実行 (変更なし) ---
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("致命的エラー: DISCORD_BOT_TOKENが設定されていません。")
    elif not ENCRYPTION_KEY:
         print("致命的エラー: ENCRYPTION_KEYが設定されていません。")
    elif not MONGODB_URI: # MongoDB URIのチェックも追加
        print("致命的エラー: MONGODB_URIが設定されていません。")
    else:
        try:
             keep_alive() # keep_alive は Koyeb では必須ではないが、害はない
             bot.run(DISCORD_BOT_TOKEN)
        except discord.LoginFailure:
             print("致命的エラー: 不正なDiscord Botトークンです。")
        except discord.errors.PrivilegedIntentsRequired:
              print("致命的エラー: 必要な特権インテントが有効になっていません。")
              print("=> Message Content Intent と Server Members Intent を有効にしてください。")
        except pymongo.errors.ConnectionFailure: # MongoDB接続失敗時のエラーをキャッチ
            print("致命的エラー: MongoDB への接続に失敗しました。接続文字列やネットワークアクセス設定を確認してください。")
            # MongoDBに接続できないと Bot を起動しても意味がないので終了する
        except Exception as e:
             print(f"Bot実行中に予期せぬエラーが発生しました: {e}")
