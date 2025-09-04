import logging
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from telegram import Update, Bot
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

# 自建 Bot API Server（例如 http://localhost:8081 或 http://tg-bot-api:8081）
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

# 鉴权白名单
ALLOWED_USER_IDS: set[int] = _env_id_set("ALLOWED_USER_IDS")   # 用户白名单
ALLOWED_CHAT_IDS: set[int] = _env_id_set("ALLOWED_CHAT_IDS")   # 聊天白名单（群/超群/频道）

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


def _suggest_filename(base: Optional[str], default_stem: str, ext: str) -> str:
    try:
        stem = Path(base).stem if base else default_stem
        safe_stem = "".join(c for c in stem if c.isalnum() or c in (" ", "-", "_")).strip() or default_stem
        return f"{safe_stem}.{ext.lstrip('.')}"
    except Exception:
        return f"{default_stem}.{ext.lstrip('.')}"


def _human_size(n_bytes: int) -> str:
    if n_bytes is None:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n_bytes)
    for u in units:
        if size < 1024 or u == units[-1]:
            if u in ("MB", "GB", "TB"):
                return f"{size:.1f} {u}"
            return f"{int(size)} {u}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _fmt_speed(bytes_per_sec: float) -> str:
    return f"{_human_size(bytes_per_sec)}/s"


async def _download_with_progress(url: str, dest: Path, status_msg):
    """
    仅发送与编辑一个消息：
    - 初始：下载中…
    - 过程中：下载中… 进度/大小/速度
    - 完成：编辑为“转换中…”
    """
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT)
    limits = httpx.Limits(max_keepalive_connections=MAX_KEEPALIVE, max_connections=MAX_CONNECTIONS)

    last_edit = 0.0
    last_bytes = 0
    start = time.monotonic()

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)

            # 初始提示
            try:
                if total > 0:
                    await status_msg.edit_text(f"下载中… 0% (0 / {_human_size(total)}) 0 B/s")
                else:
                    await status_msg.edit_text("下载中… (大小未知)")
            except Exception:
                pass

            with open(dest, "wb") as f:
                downloaded = 0
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.monotonic()
                    # 节流：每秒最多编辑一次，且至少前进 1%
                    need_update = False
                    if now - last_edit >= 1.0:
                        need_update = True
                    elif total > 0:
                        prev_pct = int((last_bytes / total) * 100)
                        curr_pct = int((downloaded / total) * 100)
                        if curr_pct > prev_pct:
                            need_update = True

                    if need_update:
                        elapsed = max(now - start, 1e-6)
                        speed = (downloaded / elapsed)
                        try:
                            if total > 0:
                                pct = int(downloaded * 100 / total)
                                await status_msg.edit_text(
                                    f"下载中… {pct}% ({_human_size(downloaded)} / {_human_size(total)}) {_fmt_speed(speed)}"
                                )
                            else:
                                await status_msg.edit_text(
                                    f"下载中… {_human_size(downloaded)} {_fmt_speed(speed)}"
                                )
                        except Exception:
                            pass
                        last_edit = now
                        last_bytes = downloaded

    # 下载完成 → 转换中
    try:
        await status_msg.edit_text("转换中…")
    except Exception:
        pass


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

    if BOT_API_LOCAL_ROOT in file_path:
        idx = file_path.find(BOT_API_LOCAL_ROOT)
        candidate = file_path[idx:]
        if os.path.exists(candidate):
            return candidate

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

    if not local_src.startswith(BOT_API_LOCAL_ROOT):
        logger.warning("Skip deleting local source outside BOT_API_LOCAL_ROOT: %s", local_src)
        return

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
    # 只维护一个“进度/状态”消息：下载中…（带进度/速度）→ 转换中… → 成功后删除；失败则将其改为错误信息
    if not _has_ffmpeg():
        try:
            await update.effective_message.reply_text("转换失败：服务器未安装 ffmpeg。")
        except Exception:
            pass
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
        return  # 不多发消息

    status_msg = None

    try:
        logger.info("Calling get_file for file_id=%s", video.file_id)
        file = await context.bot.get_file(video.file_id)
        fpath = str(getattr(file, "file_path", "") or "")
        fsize = getattr(file, "file_size", None)
        logger.info("get_file ok: file_size=%s, file_path=%s", fsize, fpath)

        local_source = _pick_local_source(fpath)

        with tempfile.TemporaryDirectory(prefix="tg_v2a_") as td:
            td_path = Path(td)
            temp_dl = td_path / "input_video"
            out_name = _suggest_filename(filename, "audio", AUDIO_EXT)
            out_path = td_path / out_name

            # 下载阶段
            if local_source:
                # 本地直读：直接进入转换阶段，仅发“转换中…”
                status_msg = await msg.reply_text("转换中…")
                input_path = Path(local_source)
                logger.info("Using local source: %s", input_path)
            else:
                # 优先直链下载（可显示进度）；否则回退 PTB 下载（无法显示进度）
                direct_url = _build_direct_file_url(fpath)
                if direct_url:
                    status_msg = await msg.reply_text("下载中…")
                    try:
                        await _download_with_progress(direct_url, temp_dl, status_msg)
                        input_path = temp_dl
                    except Exception as dl_err:
                        logger.error("Direct download failed: %s", dl_err)
                        try:
                            await status_msg.edit_text("下载失败，请稍后重试。")
                        except Exception:
                            pass
                        return
                else:
                    # 回退：无直链，只能下载完后再进入转换
                    status_msg = await msg.reply_text("下载中…")
                    try:
                        await file.download_to_drive(custom_path=str(temp_dl))
                        input_path = temp_dl
                        try:
                            await status_msg.edit_text("转换中…")
                        except Exception:
                            pass
                    except Exception as dl_err:
                        logger.error("PTB download failed: %s", dl_err)
                        try:
                            await status_msg.edit_text("下载失败，请稍后重试。")
                        except Exception:
                            pass
                        return

            # 转换阶段
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
                FFMPEG_BIN, "-y",
                "-i", str(input_path),
                "-vn",
                "-acodec", acodec,
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
                try:
                    if status_msg:
                        await status_msg.edit_text("转换失败，请稍后重试。")
                    else:
                        await msg.reply_text("转换失败，请稍后重试。")
                except Exception:
                    pass
                _safe_remove_local_source(local_source)
                return

            # 发送音频（无 caption）
            send_ok = False
            try:
                with open(out_path, "rb") as f:
                    await msg.reply_audio(
                        audio=f,
                        filename=out_name,
                    )
                send_ok = True
            except Exception as send_err:
                logger.warning("send_audio failed, fallback to send_document: %s", send_err)
                try:
                    with open(out_path, "rb") as f:
                        await msg.reply_document(
                            document=f,
                            filename=out_name,
                        )
                    send_ok = True
                except Exception as send_err2:
                    logger.error("send_document also failed: %s", send_err2)
                    send_ok = False

            # 清理与收尾
            if CLEANUP_OUTPUT:
                _safe_unlink(out_path)
            _safe_remove_local_source(local_source)

            if send_ok and status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            elif not send_ok:
                try:
                    if status_msg:
                        await status_msg.edit_text("发送失败，请稍后重试。")
                    else:
                        await msg.reply_text("发送失败，请稍后重试。")
                except Exception:
                    pass

    except Exception as e:
        logger.exception("Processing error: %s", e)
        try:
            if status_msg:
                await status_msg.edit_text("处理失败，请稍后重试。")
            else:
                await msg.reply_text("处理失败，请稍后重试。")
        except Exception:
            pass


# 全局错误处理
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("发生错误，请稍后重试。")
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

    global FILE_URL_PREFIX
    if file_base:
        FILE_URL_PREFIX = f"{file_base}{BOT_TOKEN}"
        logger.info("Direct file URL prefix prepared.")
    else:
        # 未设置自建 file_base 时，默认使用官方直链前缀，便于显示下载进度
        FILE_URL_PREFIX = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

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

    # 授权过滤器：组合用户与聊天白名单
    user_filter = filters.User(user_id=list(ALLOWED_USER_IDS)) if ALLOWED_USER_IDS else None
    chat_filter = filters.Chat(chat_id=list(ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else None

    if user_filter and chat_filter:
        allowed_filter = user_filter | chat_filter
        logger.info(
            "Authorization enabled. Allowed users: %s | Allowed chats: %s",
            sorted(ALLOWED_USER_IDS), sorted(ALLOWED_CHAT_IDS)
        )
    elif user_filter:
        allowed_filter = user_filter
        logger.info("Authorization enabled. Allowed users: %s", sorted(ALLOWED_USER_IDS))
    elif chat_filter:
        allowed_filter = chat_filter
        logger.info("Authorization enabled. Allowed chats: %s", sorted(ALLOWED_CHAT_IDS))
    else:
        allowed_filter = filters.ALL
        logger.info("Authorization disabled (ALLOWED_USER_IDS & ALLOWED_CHAT_IDS empty). Bot is open to all chats/users.")

    # Handlers（均附带授权过滤器）
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("发送视频即可，我会返回音频。"), filters=allowed_filter))
    app.add_handler(CommandHandler("help", lambda u, c: u.message.reply_text("发送视频，我会抽取音频并返回。"), filters=allowed_filter))
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
