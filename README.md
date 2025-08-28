# TG Bot: 视频转音频（自建 Bot API，ptb v21.x，含清理与授权白名单）

接收视频，使用 ffmpeg 抽取音频并回传。支持自建 Bot API（突破 50MB）；大文件优化（长超时、直链回退、本地直读）；发送完成后清理音频与源视频；授权白名单，仅允许指定用户使用。

# 部署

## 运行（docker-compose，推荐）
1) 准备 `.env`：
   - `BOT_TOKEN`，`TELEGRAM_API_ID`，`TELEGRAM_API_HASH`
   - 可选：`ALLOWED_USER_IDS`、清理策略、超时参数
2) 启动：
   ```bash
   docker compose up -d
   docker compose logs -f
   ```
# 环境变量

| 变量名 | 适用容器 | 说明 | 示例值 | 默认值 | 获取方式/来源 | 备注 |
|---|---|---|---|---|---|---|
| BOT_TOKEN | bot | 你的 Telegram 机器人令牌 | 123456:ABC… | 无（必填） | 在 Telegram 与 @BotFather 创建 Bot 后获得 | 必须配置，程序会校验 |
| TELEGRAM_API_ID | bot-api | 自建 Bot API Server 所需的 api_id | 1234567 | 无（必填） | 登录 https://my.telegram.org → API development tools → 创建应用 | 仅 bot-api 容器需要 |
| TELEGRAM_API_HASH | bot-api | 自建 Bot API Server 所需的 api_hash | abcdef123456… | 无（必填） | 同上，与 API_ID 配套获取 | 仅 bot-api 容器需要 |
| TG_BASE_URL | bot | 自建 Bot API 的基础 URL（程序会自动补 /bot） | http://bot-api:8081 | 空 | 自行填写服务地址（容器内用服务名，主机上用主机名/IP） | 为空则走官方 Bot API |
| TG_FILE_BASE_URL | bot | 自建 Bot API 的文件 URL（程序会自动补 /file/bot） | http://bot-api:8081 | 空 | 同上 | 留空时与 TG_BASE_URL 相同 |
| TG_CONNECT_TIMEOUT | bot | HTTP 连接超时（秒） | 30 | 30 | 自行设置 | 大文件/弱网建议保守 |
| TG_READ_TIMEOUT | bot | HTTP 读取超时（秒） | 600 | 600 | 自行设置 | getFile/下载大文件需较长超时 |
| TG_WRITE_TIMEOUT | bot | HTTP 写入超时（秒） | 600 | 600 | 自行设置 | 上传/发送大文件需较长超时 |
| TG_POOL_TIMEOUT | bot | HTTP 连接池等待超时（秒） | 60 | 60 | 自行设置 | 某些 ptb 版本支持；程序会自动降级 |
| TG_MAX_CONNECTIONS | bot | HTTPX 最大连接数 | 100 | 100 | 自行设置 | 影响并发请求能力 |
| TG_MAX_KEEPALIVE | bot | HTTPX 保持活跃连接数 | 20 | 20 | 自行设置 | 影响连接复用 |
| FFMPEG_BIN | bot | ffmpeg 可执行文件路径 | /usr/bin/ffmpeg | ffmpeg | 系统安装 ffmpeg 后可通过 which ffmpeg 确认 | Docker 镜像已预装 |
| AUDIO_EXT | bot | 输出音频格式 | mp3 / m4a / aac / opus / ogg / flac / wav | mp3 | 自行设置 | 程序会根据格式选择合适编码器 |
| AUDIO_BITRATE | bot | 输出音频码率（有损格式有效） | 192k | 192k | 自行设置 | 例如 96k/128k/192k 等 |
| CLEANUP_OUTPUT | bot | 发送后删除生成的音频文件 | 1 / true | 1 | 自行设置 | 支持 1/0/true/false/yes/no/on |
| CLEANUP_LOCAL_SOURCE | bot | 发送后删除源视频（当源视频来自 bot-api 本地缓存） | 1 / true | 1 | 自行设置 | 需 bot 容器对共享卷有写权限；仅删除本 Bot 的目录，安全校验严格 |
| ALLOWED_USER_IDS | bot | 允许使用机器人的用户 ID 白名单（逗号/空格分隔） | 12345678, 987654321 | 空（不限制） | 获取方式：1) 给 Bot 发消息看日志里的 effective_user.id；2) 用 @userinfobot/@getidsbot；3) 临时代码打印 user_id | 非空时仅这些用户可用 |
| TELEGRAM_LOCAL | bot-api | 让 bot-api 将文件缓存到本地磁盘以加速大文件访问 | 1 | 无 | 自行设置 | 推荐设为 1，配合共享卷可本地直读 |
| TELEGRAM_VERBOSITY | bot-api | bot-api 日志冗余等级 | 1 | 无 | 自行设置 | 调试时可提高以便排错 |

## 授权白名单
- 通过环境变量 `ALLOWED_USER_IDS` 指定允许使用机器人的 Telegram 用户 ID。
- 支持逗号或空格分隔；留空表示不限制（对所有用户开放）。
- 例：
  - `ALLOWED_USER_IDS=12345678, 987654321`
  - `ALLOWED_USER_IDS=12345678 987654321`
- 实现方式：在所有命令与消息处理器上附加 `filters.User(user_id=...)`；不在白名单的用户消息将被忽略（不回应）。

## 清理策略
- 输出音频：发送完成后立即删除（`CLEANUP_OUTPUT=1`，默认开启），并且临时目录会自动销毁。
- 源视频（当来自自建 API 本地缓存）：默认删除（`CLEANUP_LOCAL_SOURCE=1`）。为安全起见，仅删除位于 `/var/lib/telegram-bot-api/<BOT_TOKEN>/...` 目录下的文件，避免误删其他 Bot 的缓存。若不希望删除缓存，设为 `0`。
