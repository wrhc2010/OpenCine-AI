# OpenCine-AI

OpenCine-AI 是一个本地运行的 AI 视频制作工作台。你输入一条创作提示词，系统会先询问会影响成片的问题，再生成镜头计划，制作视频，按验收标准检查结果，最后在独立页面预览和下载成片。

项目适合需要反复修改、管理多个项目或同一项目多个交付版本的本地工作流。

## 当前界面

正式 WebUI 只有两个视觉主题：

- **Studio Grid**：亮色模式，白底、细网格和蓝色强调色。
- **Night Console**：暗色模式，黑色工作台和绿色状态信号。

右上角的月亮/太阳按钮可以切换主题。选择会保存在当前浏览器中，第一次打开时默认跟随系统主题。

## 使用流程

1. 打开“新建项目”，输入原始创作提示词。
2. 在“需求澄清”页回答 LLM 提出的高影响问题。每轮最多 3 个问题。
3. 在“计划审核”页检查镜头和验收标准。
4. 开始制作，等待视频生成、关键帧验收和成片组装。
5. 进入“成片预览”页播放视频，确认后下载 MP4 或完成交付。
6. 已交付项目可以创建新版本，原版本和审计记录会保留。

顶部阶段条支持点击回看。真正回退会弹出确认框；回退只会让下游结果失效，不会回滚已经产生的费用。

## Docker 启动

需要安装 Docker Desktop。第一次启动：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

打开：

- WebUI：<http://localhost:3000>
- 后端健康检查：<http://localhost:8000/healthz>

Compose 只启动三个服务：

- `web`：Nginx 和 React 静态文件，同时代理 `/v1`。
- `backend`：FastAPI、任务队列、后台 Worker、视频下载和媒体接口。
- `database`：PostgreSQL。

后端的队列数据库和媒体文件保存在 `backend-data` 卷，业务数据保存在 `postgres-data` 卷。删除容器不会自动删除这两个卷。

停止服务：

```powershell
docker compose down
```

## 模型配置

默认配置使用 Mock Provider，不需要密钥即可验证项目、计划、队列和状态流转。Mock 不会生成真实可播放的视频；要测试成片预览和下载，请配置 Agnes、Fal 或其他能返回真实 MP4 地址的 Provider。要使用 Agnes：

```dotenv
VIDEO_PROVIDER=agnes
VIDEO_MODEL=agnes-video-v2.0
AGNES_API_KEY=请填入你的密钥
DIRECTOR_LLM_MODEL=agnes-2.5-flash
DIRECTOR_VLM_MODEL=agnes-2.5-flash
OPENAI_BASE_URL=https://api.agnes-ai.cn/v1
LLM_PROVIDER=openai-compatible-llm
VLM_PROVIDER=openai-compatible-vlm-judge
```

也可以在 WebUI 的“设置”页填写模型和密钥。密钥会保存在数据库中，但 API 和页面只返回脱敏结果；编辑时留空表示保留原密钥。新项目和新重试会读取最新的普通设置。

自定义 Provider 支持：

- 请求方法、URL、Headers 和 Body 模板
- Bearer API Key 自动注入
- 异步轮询
- JSONPath 提取任务 ID、状态和结果 URL
- Provider 测试和密钥脱敏

## 限流和失败处理

每个密钥默认按保守策略限流，每分钟最多发起一次模型请求。服务会遵守 Provider 返回的 `Retry-After`。连续收到两次 429 后，当前密钥会冷却 15 分钟并尝试备用密钥；认证错误会切换密钥。未知的视频提交错误不会盲目重发，避免重复创建任务和重复计费。

LLM 澄清要求严格 JSON。配置了真实 LLM 后，如果请求失败或返回格式不正确，项目会停在错误状态，不会用关键词规则伪造澄清结果。

VLM 验收会从视频抽取关键帧，转成 `data:image/...;base64,...` 后发送。Agnes 不接受这种图片输入、返回错误或证据不完整时，验收会失败并进入人工处理，不会默认放行。

## 本地开发

后端：

```powershell
python -m pip install -e ".[api,dev]"
uvicorn video_director.api:app --reload --port 8000
```

前端：

```powershell
cd web
npm ci
npm run dev
```

前端开发服务器默认使用 <http://127.0.0.1:3000>，并把 `/v1` 代理到本地后端。

运行测试：

```powershell
pytest -q
cd web
npm run build
```

## 已知限制

- Agnes 视频已经接入异步创建和轮询，真实验收仍需要可用的模型账号、额度和网络。
- LLM/VLM 使用 OpenAI 兼容接口；不同兼容服务对 JSON Schema 和图片输入的支持可能不同。
- 当前本地内嵌队列适合单机部署。多副本部署前需要单独设计共享队列和媒体存储。
- 自动验收只依据模型返回的证据。证据不足会失败，需人工处理。
- 不要把 API Key 提交到 Git，也不要把真实密钥写进测试、日志、截图或 issue。
