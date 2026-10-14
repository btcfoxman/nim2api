# NIM2API 协议与验收

网页 CDP 抓包分析见 `../research/session-2026-10-14.md`。服务与 NIM 的所有交互由每个账号已登录的 Chromium 发出；浏览器配置该账号代理。调用者提供的外部素材由账号代理下载，再经 NIM 浏览器同源 multipart 上传。

## 请求链路

1. `GET /api/credits/getUserCreditsData` 验证登录并读取四类余额。
2. 参考素材下载后，`POST /api/files/upload`，保存返回的 `wf-input/...` 路径。视频与音频时长由本地 `ffprobe` 读取。
3. 根据模型别名映射 `externalModel`、`meta.advancedMode`、分辨率与毫秒时长，`POST /api/workflows/start`；保存 `workflow.id`，提交结果不明时不自动重试以避免重复扣费。
4. `GET /api/workflows/recentWorkflows`，以 `workflow.id` 对照状态。完成后将 `wf-output/...` 拼成 `https://static.nim.video/output/...`。余额变化和 `creditsCharged` 写入消耗参考。

## 已验证

- 本机 `nim-01` 的账号积分与项目 ID 可读取。
- 浏览器 multipart 上传 320×320 图片返回 HTTP 201 和 `filePath`。
- 捕获的已完成 Mini 任务可由服务轮询得到结果视频，结果静态链接 Range 请求 HTTP 206。
- 模型与提示词标记的协议测试通过；测试涵盖 9 图、3 音频、3 视频的字段组装。

## 3.5 pre 验收

部署后需按真实服务路径提交 `sd-2-0-mini-480p`、4 秒、9 图/3 音频/3 视频，并确认完成结果及积分扣除。该项以实际线上任务状态为准。
