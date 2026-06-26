FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl git ffmpeg ripgrep build-essential ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN git clone --depth=1 https://github.com/NousResearch/hermes-agent.git /hermes-agent

WORKDIR /hermes-agent

RUN uv pip install --system -e ".[all]" --no-cache

RUN mkdir -p /root/.hermes

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 7860

CMD ["/start.sh"]
