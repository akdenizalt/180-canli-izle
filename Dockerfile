FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY app.py /app/app.py
RUN mkdir -p /tmp/release-hls && chown -R app:app /app /tmp/release-hls
USER app
ENV PYTHONUNBUFFERED=1 PORT=10000 DATA_DIR=/tmp/release-hls
EXPOSE 10000
CMD ["python", "-u", "/app/app.py"]
