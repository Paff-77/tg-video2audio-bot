import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import httpx

try:
    from telegram.request import HTTPXRequest  # python-telegram-bot v20/v21
except Exception:
    HTTPXRequest = None

# ============ Config ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192k")
AUDIO_EXT = os.getenv("AUDIO_EXT", "mp3")

# 自建 Bot API Server（例如 http://localhost:8081 或 http://bot-api:8081）
TG_BASE_URL = os.getenv("TG_BASE_URL", "").strip().rstrip("/")
TG_FILE_BASE_URL = os.getenv("TG_FILE_BASE_URL", "").strip().rstrip("/")

def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _env_id_set(name: str) -> set[int]:
    """
    从环境变量解析 ID 白名单，支持逗号/空白分隔。
    为空表示不限制（允许所有）。
    """
    raw = os.getenv(name, "")
    ids: set[int] = set()
    for part in raw.replace(",", " ").split():
        try:
            ids.add(int(part))
        except Exception:
            pass
    return ids

# HTTP 超时/连接池（大文件友好）
CONNECT_TIMEOUT = _env_float("TG_CONNECT_TIMEOUT", 30.0)
READ_TIMEOUT = _env_float("TG_READ_TIMEOUT", 600.0)
WRITE_TIMEOUT = _env_float("TG_WRITE_TIMEOUT", 600.0)
POOL_TIMEOUT = _env_float("TG_POOL_TIMEOUT", 60.0)
MAX_CONNECTIONS = _env_int("TG_MAX_CONNECTIONS", 100)
MAX_KEEPALIVE = _env_int("TG_MAX_KEEPALIVE", 20)

# 清理策略
CLEANUP_OUTPUT = _env_bool("CLEANUP_OUTPUT", True)
CLEANUP_LOCAL_SOURCE = _env_bool("CLEANUP_LOCAL_SOURCE", True)

# 授权白名单：允许使用的 Telegram user id 集合（为空则不限制）
ALLOWED_USER_IDS: set[int] = _env_id_set("ALLOWED_USER_IDS")

# 手动直链下载前缀（初始化于 _build_application）
FILE_URL_PREFIX = ""  # http://host:port/file/bot<token>

# Bot API 本地缓存目录（固定值；要求两容器路径一致）
BOT_API_LOCAL_ROOT = "/var/lib/telegram-bot-api/"

# ============ Logging ============
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tg-video2audio-bot")


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


async def _send_chat_action(update: Update, action: ChatAction, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)
    except Exception:
        pass


def _suggest_filename(base: Optional[str], default_stem: str, ext: str) -> str:
    try:
        stem = Path(base).stem if base else default_stem
        safe_stem = "".join(c for c in stem if c.isalnum() or c in (" ", "-", "_")).strip() or default_stem
        return f"{safe_stem}.{ext.lstrip('.')}"
    except Exception:
        return f"{default_stem}.{ext.lstrip('.')}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "发送一个视频给我，我会把它转换成音频并返回。\n"
        f"- 输出格式: {AUDIO_EXT}\n"
        f"- 比特率: {AUDIO_BITRATE}\n\n"
        "服务端可用 AUDIO_EXT / AUDIO_BITRATE 修改。\n"
        "若配置自建 Bot API Server，可突破官方 50MB 文件限制。"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("直接给我发视频即可，我会抽取音频并返回。也可以用 /start 查看当前配置。")


async def _httpx_stream_download(url: str, dest: Path):
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT)
    limits = httpx.Limits(max_keepalive_connections=MAX_KEEPALIVE, max_connections=MAX_CONNECTIONS)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        f.write(chunk)


def _build_direct_file_url(file_path: str) -> Optional[str]:
    global FILE_URL_PREFIX
    if not FILE_URL_PREFIX:
        return None
    fp = (file_path or "").strip()
    if fp.startswith("http://") or fp.startswith("https://"):
        return fp
    if not fp.startswith("/"):
        fp = "/" + fp
    return FILE_URL_PREFIX + fp


def _pick_local_source(file_path: str) -> Optional[str]:
    """
    若 file_path 包含 Bot API 的本地缓存绝对路径，尝试直接读取该本地文件。
    需要 bot 容器挂载 /var/lib/telegram-bot-api 同路径卷（读写）。
    """
    if not file_path:
        return None

    # file_path 可能是完整 URL，提取绝对路径子串
    if BOT_API_LOCAL_ROOT in file_path:
        idx = file_path.find(BOT_API_LOCAL_ROOT)
        candidate = file_path[idx:]
        if os.path.exists(candidate):
            return candidate

    # 或者 file_path 本身就是绝对路径
    if file_path.startswith("/") and os.path.exists(file_path):
        return file_path

    return None


def _safe_unlink(path: Path):
    try:
        if path.exists():
            path.unlink(missing_ok=True)
            logger.info("Deleted file: %s", path)
    except Exception as e:
        logger.warning("Failed to delete file %s: %s", path, e)


def _safe_remove_local_source(local_src: Optional[str]):
    """
    安全删除 bot-api 缓存中的源视频：
    - 仅在 CLEANUP_LOCAL_SOURCE 开启时
    - 仅删除位于 /var/lib/telegram-bot-api/<token>/... 下的文件
    """
    if not local_src or not CLEANUP_LOCAL_SOURCE:
        return

    # 必须是绝对路径且处于 BOT_API_LOCAL_ROOT 下
    if not local_src.startswith(BOT_API_LOCAL_ROOT):
        logger.warning("Skip deleting local source outside BOT_API_LOCAL_ROOT: %s", local_src)
        return

    # 仅删除属于当前 bot token 的目录里的文件，避免误删其他 bot 的缓存
    token_prefix = os.path.join(BOT_API_LOCAL_ROOT, BOT_TOKEN)
    if not local_src.startswith(token_prefix):
        logger.warning("Skip deleting local source not under this bot token dir: %s", local_src)
        return

    try:
        if os.path.isfile(local_src):
            os.remove(local_src)
            logger.info("Deleted local source video: %s", local_src)
    except Exception as e:
        logger.warning("Failed to delete local source %s: %s", local_src, e)


async def handle_video_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _has_ffmpeg():
        await update.effective_message.reply_text("服务器未安装 ffmpeg 或 ffmpeg 不可用。请安装后重试。")
        return

    msg = update.effective_message
    video = None
    filename = None

    if msg.video:
        video = msg.video
        filename = getattr(video, "file_name", None)
    elif msg.video_note:
        video = msg.video_note
        filename = None
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
        video = msg.document
        filename = msg.document.file_name

    if not video:
        await msg.reply_text("请发送视频文件。")
        return

    status = await msg.reply_text("已收到视频，正在准备并转换音频，请稍候…")
    await _send_chat_action(update, ChatAction.TYPING, context)

    try:
        logger.info("Calling get_file for file_id=%s", video.file_id)
        file = await context.bot.get_file(video.file_id)
        fpath = str(getattr(file, "file_path", "") or "")
        fsize = getattr(file, "file_size", None)
        logger.info("get_file ok: file_size=%s, file_path=%s", fsize, fpath)

        local_source = _pick_local_source(fpath)

        with tempfile.TemporaryDirectory(prefix="tg_v2a_") as td:
            td_path = Path(td)
            temp_dl = td_path / "input_video"  # 若需要下载，则下载到此处
            out_name = _suggest_filename(filename, "audio", AUDIO_EXT)
            out_path = td_path / out_name

            # 选择输入源
            if local_source:
                input_path = Path(local_source)
                logger.info("Using local source: %s", input_path)
            else:
                # 先尝试 PTB 下载；失败再直链下载
                try:
                    logger.info("Downloading via PTB to %s", temp_dl)
                    await file.download_to_drive(custom_path=str(temp_dl))
                    input_path = temp_dl
                    logger.info("PTB download completed: %s bytes", temp_dl.stat().st_size if temp_dl.exists() else "unknown")
                except Exception as dl_err:
                    logger.warning("PTB download failed: %s. Will try direct URL fallback.", dl_err)
                    direct_url = _build_direct_file_url(fpath)
                    if not direct_url:
                        raise
                    logger.info("Direct downloading from %s", direct_url.replace(BOT_TOKEN, "<token>"))
                    await _httpx_stream_download(direct_url, temp_dl)
                    input_path = temp_dl
                    logger.info("Direct download completed: %s bytes", temp_dl.stat().st_size if temp_dl.exists() else "unknown")

            # ffmpeg 转码
            codec_map = {
                "mp3": "libmp3lame",
                "m4a": "aac",
                "aac": "aac",
                "opus": "libopus",
                "ogg": "libopus",
                "oga": "libopus",
                "flac": "flac",
                "wav": "pcm_s16le",
            }
            acodec = codec_map.get(AUDIO_EXT.lower(), "libmp3lame")
            cmd = [
                FFMPEG_BIN,
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-acodec",
                acodec,
            ]
            if AUDIO_EXT.lower() in {"mp3", "m4a", "aac", "opus", "ogg"} and AUDIO_BITRATE:
                cmd += ["-b:a", AUDIO_BITRATE]
            if AUDIO_EXT.lower() in {"opus", "ogg"}:
                cmd += ["-vbr", "on"]
            cmd.append(str(out_path))

            logger.info("Running ffmpeg: %s", shlex.join(cmd))
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0 or not out_path.exists():
                tail = (proc.stderr or "")[-2000:]
                logger.error("ffmpeg failed: %s", tail)
                await status.edit_text("转换失败：ffmpeg 处理出错。请确认视频编码有效或稍后重试。")
                # 即使失败，也尝试根据策略清理源视频（本地直读时）
                _safe_remove_local_source(local_source)
                return

            await status.edit_text("转换完成，正在发送音频…")
            await _send_chat_action(update, ChatAction.UPLOAD_DOCUMENT, context)

            # 发送音频（优先 send_audio，失败回退 send_document）
            send_ok = False
            try:
                with open(out_path, "rb") as f:
                    await msg.reply_audio(
                        audio=f,
                        filename=out_name,
                        caption=f"已从视频提取音频（{AUDIO_EXT.upper()}）",
                    )
                send_ok = True
            except Exception as send_err:
                logger.warning("send_audio failed, fallback to send_document: %s", send_err)
                try:
                    with open(out_path, "rb") as f:
                        await msg.reply_document(
                            document=f,
                            filename=out_name,
                            caption=f"已从视频提取音频（{AUDIO_EXT.upper()}）",
                        )
                    send_ok = True
                except Exception as send_err2:
                    logger.error("send_document also failed: %s", send_err2)
                    send_ok = False

            # 发送后清理：输出音频 +（可选）本地源视频
            if CLEANUP_OUTPUT:
                _safe_unlink(out_path)
            _safe_remove_local_source(local_source)

            if send_ok:
                await status.delete()
            else:
                await status.edit_text("发送失败，请稍后重试。")

    except Exception as e:
        logger.exception("Processing error: %s", e)
        try:
            await status.edit_text("处理失败，可能是文件过大或网络或路径权限问题。请稍后重试，并检查容器卷挂载。")
        except Exception:
            pass


# 全局错误处理
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("发生错误，我已记录日志。请稍后重试。")
    except Exception:
        pass


def _build_request_safe():
    if HTTPXRequest is None:
        logger.warning("HTTPXRequest not available; using default PTB request.")
        return None

    # 分级降级，适配不同 ptb 版本
    try:
        limits = httpx.Limits(max_keepalive_connections=MAX_KEEPALIVE, max_connections=MAX_CONNECTIONS)
        req = HTTPXRequest(
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
            pool_limits=limits,
        )
        logger.info("Using HTTPXRequest with pool_limits.")
        return req
    except TypeError as te:
        logger.info("HTTPXRequest(pool_limits=...) unsupported in this PTB version: %s", te)
    except Exception as e:
        logger.info("HTTPXRequest with pool_limits failed: %s", e)

    try:
        req = HTTPXRequest(
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
            pool_timeout=POOL_TIMEOUT,
        )
        logger.info("Using HTTPXRequest with pool_timeout.")
        return req
    except TypeError as te:
        logger.info("HTTPXRequest(pool_timeout=...) unsupported in this PTB version: %s", te)
    except Exception as e:
        logger.info("HTTPXRequest with pool_timeout failed: %s", e)

    try:
        req = HTTPXRequest(
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
        )
        logger.info("Using HTTPXRequest with basic timeouts only.")
        return req
    except Exception as e:
        logger.warning("HTTPXRequest basic construction failed: %s. Will fallback to PTB default request.", e)
        return None


def _build_application() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("请设置环境变量 BOT_TOKEN=你的TelegramBotToken")

    request = _build_request_safe()

    def _normalize_urls():
        base_url = TG_BASE_URL
        file_base = TG_FILE_BASE_URL or TG_BASE_URL
        if base_url and not base_url.endswith("/bot"):
            base_url = base_url.rstrip("/") + "/bot"
        if file_base and not file_base.endswith("/file/bot"):
            file_base = file_base.rstrip("/") + "/file/bot"
        return base_url, file_base

    base_url, file_base = _normalize_urls()

    # 设置直链前缀
    global FILE_URL_PREFIX
    if file_base:
        FILE_URL_PREFIX = f"{file_base}{BOT_TOKEN}"
        logger.info("Direct file URL prefix prepared.")

    if base_url:
        logger.info("Using self-hosted Bot API server: base_url=%s | base_file_url=%s", base_url, file_base)
        if request is not None:
            bot = Bot(token=BOT_TOKEN, base_url=base_url, base_file_url=file_base, request=request)
            app_builder = Application.builder().bot(bot)
        else:
            bot = Bot(token=BOT_TOKEN, base_url=base_url, base_file_url=file_base)
            app_builder = Application.builder().bot(bot)
    else:
        if request is not None:
            app_builder = Application.builder().token(BOT_TOKEN).request(request)
        else:
            app_builder = Application.builder().token(BOT_TOKEN)

    app = app_builder.build()

    # 授权过滤器：若 ALLOWED_USER_IDS 非空，则仅放行这些用户
    if ALLOWED_USER_IDS:
        allowed_filter = filters.User(user_id=list(ALLOWED_USER_IDS))
        logger.info("Authorization enabled. Allowed user IDs: %s", sorted(ALLOWED_USER_IDS))
    else:
        allowed_filter = filters.ALL
        logger.info("Authorization disabled (ALLOWED_USER_IDS empty). Bot is open to all users.")

    # Handlers（均附带授权过滤器）
    app.add_handler(CommandHandler("start", start, filters=allowed_filter))
    app.add_handler(CommandHandler("help", help_cmd, filters=allowed_filter))
    app.add_handler(MessageHandler((filters.VIDEO | filters.VIDEO_NOTE) & allowed_filter, handle_video_like))
    app.add_handler(MessageHandler(filters.Document.MimeType("video/") & allowed_filter, handle_video_like))
    app.add_error_handler(error_handler)

    return app


def main():
    app = _build_application()
    logger.info("Bot started. Waiting for messages...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()