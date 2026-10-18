FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app ./app
COPY config ./config
COPY web ./web
COPY scripts ./scripts

# 字体：封面生成需要中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

ENV LLCV_OUT=/app/out LLCV_DB=/app/data/llcv.db
VOLUME ["/app/out", "/app/data"]
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
