FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENV CEREBRO_HOME=/data
VOLUME ["/data"]

ENTRYPOINT ["cerebro-node"]
CMD ["start"]
