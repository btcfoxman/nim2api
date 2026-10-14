# NIM2API

NIM 网页协议视频服务。管理页与账号浏览器管理沿用 pip2api 的操作方式；每个账号拥有独立 Chromium profile 和账号级代理，网页验证后浏览器持续在线。服务通过登录浏览器发出 NIM 同源请求，任务记录持久化在 SQLite。

## 运行

复制 `.env.example` 为 `.env`，填写不同的 `NIM_API_KEY`、`NIM_ADMIN_TOKEN`，执行 `docker compose up -d`。默认端口为 **8809**；`GET /health` 无需登录。账号代理可配置为 `socks5://host.docker.internal:20001`，浏览器访问、上传、提交、轮询和积分查询均使用该浏览器代理。管理页可在线添加账号、网页登录和检查账号；已有登录浏览器也可按 CDP 端口添加。

`POST /v1/videos` 和 `GET /v1/videos/{task_id}` 使用 `Authorization: Bearer <NIM_API_KEY>`。请求示例：

```json
{
  "model": "sd-2-0-mini-480p",
  "duration": 4,
  "ratio": "16:9",
  "prompt": "参考 @图片1 @视频1 @音频1 生成一条短视频",
  "image_urls": ["https://example.com/image.jpg"],
  "video_urls": ["https://example.com/reference.mp4"],
  "audio_urls": ["https://example.com/reference.mp3"]
}
```

视频任务 ID 形如 `nim_...`。提交后可轮询状态；成功时 `data[].url` 为 `static.nim.video` 结果链接。`/v1/videos/{task_id}/content` 重定向到视频。素材支持 HTTP(S) URL 与 base64 data URL；已由同一账号上传的 NIM 文件可用 `nim:wf-input/...` 引用。提示词中的 `图片1|图1|img1|Image1`、`视频1|视1|video1|v1`、`音频1|音1|audio1` 会归一为 NIM 的 `@image1`、`@video1`、`@audio1`。

模型别名及规格见 `GET /v1/models`。未在别名中指定分辨率时默认 720p；`minimax-h3-768p` 固定 768p。素材上限：Seedance 2.0 为 9 图/3 视频/3 音频，Seedance 2.5 为 30/10/10，Wan 3.0 为 10/5/5，Minimax H3 为 9/3/3。账号选取使用同规格历史实耗，缺少样本时采用网页观察值估算；上游提交响应的 `creditsCharged` 与余额差可继续校正消耗参考。

部署工作流是 `.github/workflows/deploy-test.yml`：推送 `pre` 后构建 GHCR 镜像，经 3.5 pre 的自托管 runner 部署到 `/home/btcfoxman/docker/nim2api`，维护 cloudflared `nim2api.aiid.edu.kg` 入口。服务 `.env` 只留在服务器，不入仓库。

## 协议证据

`research/session-2026-10-14.md` 记录了本机 CDP 的上传、提交、轮询、计费和未确认项。原始结构化日志仅保存在本机 `research/captures/`，不包含提示词、素材内容、Cookie 或 Token，也不推送到公开仓库。
