FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# CI build 蓋上的版本碼（YYYYMMDDHHMM），由 /version 端點回傳供前端比對是否更新
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}
CMD ["python", "main.py"]
