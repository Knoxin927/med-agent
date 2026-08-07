# 使用与当前本机 Python 主版本一致的精简 Linux Python 基础镜像。
FROM python:3.12-slim

# 设置后续命令和应用文件在容器内使用的工作目录。
WORKDIR /app

# 先单独复制依赖清单，使源码变化时可复用依赖安装缓存。
COPY requirements.txt ./

# 安装 API 运行依赖；--no-cache-dir 避免把 pip 下载缓存留在镜像中。
RUN python -m pip install --no-cache-dir -r requirements.txt

# 复制 FastAPI 应用源码到容器内的 /app/app 目录。
COPY app ./app

# 说明容器会监听 8000 端口，便于阅读和容器工具识别。
EXPOSE 8000

# 用 Uvicorn 启动现有 app.main:app；0.0.0.0 允许 Docker 端口映射转发请求。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
