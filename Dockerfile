FROM python:3.11-slim

# 安装 ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源代码
COPY app.py /app/app.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]