# TG Bot: 视频转音频（支持自建 Telegram Bot API Server，ptb v21.x，含清理机制）

接收视频，使用 ffmpeg 抽取音频并回传。支持自建 Bot API Server（`aiogram/telegram-bot-api`）绕过官方 50MB 限制。适配 python-telegram-bot 21.x。大文件优化包括：
- HTTPXRequest 超时配置与版本兼容降级
- ptb 下载失败时直链下载回退
- 本地直读（共享 bot-api 缓存目录）以避免 HTTP 下载
- 发送完成后清理音频与源视频（可配置）

## 目录结构
```
.
├─ app.py
├─ requirements.txt
├─ Dockerfile
├─ docker-compose.yml
├─ .env.example
├─ README.md
├─ .gitignore
└─ .dockerignore
```

## 快速开始（docker-compose，推荐）
1) 准备环境
```bash
cp .env.example .env
# 编辑 .env，填入：
# - BOT_TOKEN
# - TELEGRAM_API_ID / TELEGRAM_API_HASH（供 bot-api 容器）
```

2) 启动
```bash
docker compose up -d
docker compose logs -f
```

说明：
- bot-api 容器启用 `TELEGRAM_LOCAL=1`，会把文件缓存到 `/var/lib/telegram-bot-api/...`
- bot 容器通过共享卷读写该目录，并在发送完成后根据策略删除音频与缓存视频
- bot 容器通过 `TG_BASE_URL/TG_FILE_BASE_URL` 直连自建 API
- 已配置更长的 HTTP 超时，适合大文件

## 清理策略
- 输出音频：发送完成后立即删除（`CLEANUP_OUTPUT=1`，默认开启）；此外临时目录也会在任务结束时自动删除。
- 源视频（当来自自建 API 本地缓存）：默认删除（`CLEANUP_LOCAL_SOURCE=1`）。为安全起见，仅当路径位于 `/var/lib/telegram-bot-api/<BOT_TOKEN>/...` 时才会删除，避免误删其他 bot 的缓存。
- 如需保留缓存以便复用，将 `CLEANUP_LOCAL_SOURCE=0`。

注意：为删除缓存视频，bot 容器需要对共享卷有写权限。docker-compose.yml 已移除 `:ro`，保证可写。

## 本地运行（非容器）
```bash
pip install -r requirements.txt
export BOT_TOKEN=你的TelegramBotToken
# 指向自建 API（可选）
export TG_BASE_URL=http://localhost:8081
export TG_FILE_BASE_URL=http://localhost:8081
# 超时（可选）
export TG_CONNECT_TIMEOUT=30
export TG_READ_TIMEOUT=600
export TG_WRITE_TIMEOUT=600
python app.py
```

## 故障排查
- Timed out：提高 `TG_READ_TIMEOUT`（如 1200），检查 bot-api 容器日志网络连通性。
- 404/InvalidToken：已内置直链回退；确认 `TG_FILE_BASE_URL` 正确，或使用本地直读（共享卷）。
- 本地直读未生效：确认 bot 服务挂载了 `bot_api_data:/var/lib/telegram-bot-api`（无 `:ro`），且日志里 `file_path` 含该根目录。
- 删除失败：确认共享卷为读写；检查日志中安全校验提示是否路径不在当前 token 目录下。

## 兼容性说明
- ChatAction 使用 `TYPING` 与 `UPLOAD_DOCUMENT`，兼容 ptb 21.x。
- HTTPXRequest 构造按版本自动降级：优先 `pool_limits`，不支持则 `pool_timeout`，再不支持仅超时参数，最后回退默认请求器。