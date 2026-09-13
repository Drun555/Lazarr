FROM debian:trixie-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LAZARR_DATA_DIR=/data LAZARR_PLUGIN_DIR=/plugins TZ=UTC \
    LAZARR_MOVIE_PATH=/downloads/movies LAZARR_SERIES_PATH=/downloads/series \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-libtorrent ffmpeg ca-certificates tzdata tini \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/venv
WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && useradd --uid 1000 --create-home lazarr \
    && mkdir -p /data /plugins /downloads/movies /downloads/series \
    && chown -R lazarr:lazarr /data /plugins /downloads
USER lazarr
VOLUME ["/data", "/plugins", "/downloads"]
EXPOSE 8000 6881/tcp 6881/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--", "lazarr"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
