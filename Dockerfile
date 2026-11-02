# Fin-Agent 的运行镜像。
#
# 构建：docker build -t fin-agent .
# 运行：docker run --rm -p 8000:8000 -v "$HOME/.finagent:/data" fin-agent
#       然后浏览器打开 http://127.0.0.1:8000
#
# 想接真实行情/大模型，把配置从环境变量传进来即可（不写进镜像）：
#   docker run --rm -p 8000:8000 \
#     -e FINAGENT_PROVIDER=tushare \
#     -e FINAGENT_TUSHARE_TOKEN=xxxx \
#     -e FINAGENT_LLM_PROVIDER=deepseek \
#     -e FINAGENT_LLM_API_KEY=sk-xxxx \
#     fin-agent

FROM python:3.11-slim AS base

# 时区跟着 A 股走，日志里的时间才和交易日对得上
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FINAGENT_DATA_DIR=/data \
    FINAGENT_PROVIDER=offline \
    FINAGENT_LOG_LEVEL=INFO

WORKDIR /app

# 先装依赖，利用镜像层缓存：只改代码时不会重装依赖
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# 再拷代码并做「无依赖」的可编辑安装
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install -e . --no-deps

# 非 root 运行；/data 用来放 checkpoint、持仓、预警与会话记录
RUN useradd --create-home --uid 10001 finagent \
    && mkdir -p /data \
    && chown -R finagent:finagent /data /app
USER finagent
VOLUME ["/data"]

EXPOSE 8000

# 容器里没有浏览器，所以要 --no-browser；绑定 0.0.0.0 才能从宿主机访问
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/info', timeout=4)"

CMD ["finagent", "web", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
