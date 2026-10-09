FROM nousresearch/hermes-agent@sha256:3f37990271dee44b9dec6a927268533e3a900a26bd0075f6c36bd548cdd23ff0 AS runtime
USER root
COPY src /opt/qq-tmux/src
COPY vendor/hermes_qq_adapter.py /opt/hermes/gateway/platforms/qqbot/adapter.py
COPY vendor/HERMES_LICENSE /opt/qq-tmux/vendor/HERMES_LICENSE
WORKDIR /opt/qq-tmux
ENV PYTHONPATH=/opt/qq-tmux/src:/opt/hermes \
    HERMES_HOME=/opt/data \
    HOME=/opt/data \
    XDG_STATE_HOME=/opt/data/.local/state \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "-B", "-m", "tmux_bot.app"]

FROM runtime AS test
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*
COPY tests /opt/qq-tmux/tests
COPY scripts /opt/qq-tmux/scripts
COPY README.md LICENSE NOTICE SECURITY.md pyproject.toml /opt/qq-tmux/
COPY deploy /opt/qq-tmux/deploy
ENV HOME=/tmp XDG_CONFIG_HOME=/tmp/.config
USER 1000:1000
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
