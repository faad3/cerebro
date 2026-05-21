FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl bash procps \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# cerebro user for orchestrator (claude refuses root with --dangerously-skip-permissions)
RUN useradd -m -s /bin/bash cerebro

WORKDIR /app

# Install cerebro-server package (includes static + orchestrator data).
COPY pyproject.toml /app/
COPY cerebro_server /app/cerebro_server/
RUN pip install --no-cache-dir /app

# cerebro-ctl for orchestrator
COPY ctl /app/ctl/
RUN pip install --no-cache-dir -e /app/ctl/

# Entrypoint maps cerebro user's UID to match mounted ~/.claude owner
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["cerebro-server", "start", "--host", "0.0.0.0", "--port", "8000"]
