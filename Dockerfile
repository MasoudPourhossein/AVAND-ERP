ARG BASE=python:3.12-slim-bookworm
FROM ${BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Tehran AVAND_DATA=/data HOME=/data
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig tzdata postgresql-client curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 10001 -r -d /data -s /usr/sbin/nologin avand && mkdir -p /data && chown avand:avand /data
WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt
COPY src/ ./
USER avand
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://127.0.0.1:8000/health || exit 1
CMD ["gunicorn", "-w", "2", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "120", "--access-logfile", "-", "app:app"]
