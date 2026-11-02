ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS bank
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY deploy/r4/requirements-bank.lock.txt /tmp/bank.lock
RUN python -m pip install --no-cache-dir -r /tmp/bank.lock
RUN groupadd --gid 10001 banking && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/banking --shell /usr/sbin/nologin banking
ENV HOME=/home/banking XDG_CACHE_HOME=/tmp/cache
COPY backend /app/backend
COPY tools /app/tools
COPY frontend/dist /app/frontend/dist
COPY manual/bill_vision_review.html /app/manual/bill_vision_review.html
COPY deploy/server /app/deploy/server
COPY deploy/r4/model_gateway.py /app/deploy/r4/model_gateway.py
COPY SERVER_VERSION.json /app/SERVER_VERSION.json
USER 10001:10001
ENTRYPOINT ["python","-X","utf8","tools/server_entry.py"]

FROM ${PYTHON_IMAGE} AS memory
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libgomp1 libpq5 tzdata && rm -rf /var/lib/apt/lists/*
COPY deploy/r4/requirements-native-linux.lock.txt /tmp/memory.lock
COPY deploy/r4/requirements-bank.lock.txt /tmp/bank.lock
# The existing environments use different FastAPI stacks; do not merge locks.
RUN python -m pip install --no-cache-dir -r /tmp/memory.lock psycopg[binary]==3.3.3
RUN groupadd --gid 10001 banking && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/banking --shell /usr/sbin/nologin banking
ENV HOME=/home/banking XDG_CACHE_HOME=/state/cache TORCH_HOME=/state/torch
COPY tools/server_entry.py /app/tools/server_entry.py
COPY deploy/server /app/deploy/server
USER 10001:10001
ENTRYPOINT ["python","-X","utf8","tools/server_entry.py"]
