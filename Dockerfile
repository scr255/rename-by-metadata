ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY requirements.txt ./requirements.txt

USER root
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl=12.76+dfsg-1 && rm -rf /var/lib/apt/lists/*
USER ${USERNAME}

RUN set -eux; \
    if [ -s requirements.txt ]; then \
    uv pip install -r requirements.txt -p /opt/venv/bin/python; \
    fi