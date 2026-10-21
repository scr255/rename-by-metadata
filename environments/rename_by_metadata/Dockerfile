# Ubuntu base image
FROM ubuntu:24.04

# uv package manager (single static binary)
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv

# exiftool and the system Python (3.12)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl=12.76+dfsg-1 \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Build the environment virtualenv at /opt/venv:
#   - install requirements.txt with uv
#   - build_kernel.sh adds ipykernel on top (the Jupyter kernel; enough for Zed,
#     which talks the kernel protocol itself and needs no notebook server)
RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    uv venv --python 3.12 --python-preference only-system /opt/venv \
    && uv pip install --no-cache --python /opt/venv/bin/python \
    -r /tmp/requirements.txt

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /workspace
