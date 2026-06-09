FROM claude-term:local
WORKDIR /app
# NOTE: this container mounts the host docker.sock to orchestrate session containers, so the web
# process is host-root-equivalent BY DESIGN — the app process itself is the trust boundary.
# docker CLI (static binary) — version + sha256 pinned and verified (supply chain).
ARG DOCKER_VERSION=27.3.1
ARG DOCKER_SHA256=9b4f6fe406e50f9085ee474c451e2bb5adb119a03591f467922d3b4e2ddf31d3
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" -o /tmp/d.tgz \
    && echo "${DOCKER_SHA256}  /tmp/d.tgz" | sha256sum -c - \
    && tar xzf /tmp/d.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/d.tgz /tmp/docker \
    && docker --version
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/
COPY static/  /app/static/
ENV STATIC_DIR=/app/static HOME=/root
EXPOSE 3000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "3000"]
