# TG Bot: 视频转音频（自建 Bot API，ptb v21.x，含清理与授权白名单）

接收视频，使用 ffmpeg 抽取音频并回传。适配 python-telegram-bot 21.x；支持自建 Bot API（突破 50MB）；大文件优化（长超时、直链回退、本地直读）；发送完成后清理音频与源视频；新增授权白名单，仅允许指定用户使用。

## 授权白名单（鉴权）
- 通过环境变量 `ALLOWED_USER_IDS` 指定允许使用机器人的 Telegram 用户 ID（整数）。
- 支持逗号或空格分隔；留空表示不限制（对所有用户开放）。
- 例：
  - `ALLOWED_USER_IDS=12345678, 987654321`
  - `ALLOWED_USER_IDS=12345678 987654321`
- 实现方式：在所有命令与消息处理器上附加 `filters.User(user_id=...)`；不在白名单的用户消息将被忽略（不回应）。

## 清理策略
- 输出音频：发送完成后立即删除（`CLEANUP_OUTPUT=1`，默认开启），并且临时目录会自动销毁。
- 源视频（当来自自建 API 本地缓存）：默认删除（`CLEANUP_LOCAL_SOURCE=1`）。为安全起见，仅删除位于 `/var/lib/telegram-bot-api/<BOT_TOKEN>/...` 目录下的文件，避免误删其他 Bot 的缓存。若不希望删除缓存，设为 `0`。

## 运行（docker-compose，推荐）
1) 准备 `.env`：
   - `BOT_TOKEN`，`TELEGRAM_API_ID`，`TELEGRAM_API_HASH`
   - 可选：`ALLOWED_USER_IDS`、清理策略、超时参数
2) 启动：
   ```bash
   docker compose up -d
   docker compose logs -f
   ```
