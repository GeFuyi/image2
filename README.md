# Image2 Studio

一个极薄的 FastAPI + HTML 图片生成代理，前端填写自己的 API key，可上传图片和提示词，也可以只写提示词；后端用 OpenAI 兼容接口转发到服务器上的 Sub2API。

## 本地/服务器启动

```bash
cp .env.example .env
# 正常情况下不用在 .env 写 OPENAI_API_KEY，用户会在前端页面填写自己的 key。
# 如果 FastAPI 容器访问的是宿主机 8080，保留：
# OPENAI_BASE_URL=http://host.docker.internal:8080/v1

docker compose up -d --build
```

打开：

```text
http://服务器IP:8000
```

## 环境变量

```env
OPENAI_BASE_URL=http://host.docker.internal:8080/v1
# 可选：如果留空，用户必须在前端页面填写自己的 key。
OPENAI_API_KEY=
MODEL=gpt-image-2
UPSTREAM_API=responses
MAX_CONCURRENT=10
MAX_UPLOAD_BYTES=52428800
PORT=8000
LOG_LEVEL=INFO
```

## Docker 网络说明

如果 Sub2API 已经在服务器 Docker 里通过 `-p 8080:8080` 暴露到宿主机，这个项目默认用 `host.docker.internal:8080` 访问它。

如果你把 Sub2API 和本项目放到同一个 compose 网络，`OPENAI_BASE_URL` 可以改成：

```env
OPENAI_BASE_URL=http://sub2api:8080/v1
```

其中 `sub2api` 是 Sub2API 容器的服务名。
