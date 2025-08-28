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
3) bot-api 将缓存文件到 `/var/lib/telegram-bot-api/...`；bot 容器挂载同一路径（读写）。

## 本地运行（非容器）
```bash
pip install -r requirements.txt
export BOT_TOKEN=你的TelegramBotToken
# 指向自建 API（可选）
export TG_BASE_URL=http://localhost:8081
export TG_FILE_BASE_URL=http://localhost:8081
# 授权白名单（可选）
export ALLOWED_USER_IDS="12345678 987654321"
python app.py
```

## 故障排查
- 未授权用户无响应：属正常行为。若想提示“私有 Bot”，可自行在代码中添加未授权提示逻辑。
- Timed out/404：已内置直链回退与本地直读；检查网络与卷挂载。
- 删除失败：确保 bot 服务挂载卷为读写（不要加 `:ro`）。
