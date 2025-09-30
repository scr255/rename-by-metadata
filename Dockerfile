# Consume image
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl=12.76+dfsg-1 && rm -rf /var/lib/apt/lists/*
USER ${USERNAME}

WORKDIR /opt/consume

# Bring in your inputs (optional files)
# - requirements.txt: Python packages
# - extensions.txt: Open VSX extensions (one extension id per line)
# - settings.json: VS Code settings
COPY --chown=${NB_UID}:${NB_GID} requirements.tx[t] extensions.tx[t] settings.jso[n] ./

# Install Python packages into the default venv
RUN set -eux; \
  if [ -f requirements.txt ] && [ -s requirements.txt ]; then \
  echo "Installing Python packages..."; \
  uv pip install -r requirements.txt -p /opt/venv/bin/python; \
  /opt/venv/bin/python -c "import sys; print('Python packages installed successfully')"; \
  else \
  echo "No requirements.txt found or file is empty, skipping Python package installation"; \
  fi

# Install Open VSX extensions
RUN set -eux; \
  OVS_BIN="${OPENVSCODE_SERVER_DIR}/bin/openvscode-server"; \
  if [ -f extensions.txt ] && [ -s extensions.txt ]; then \
  echo "Installing VS Code extensions..."; \
  grep -v '^\s*$\|^\s*#' extensions.txt | while IFS= read -r ext; do \
  echo "Installing extension: $ext"; \
  if ! timeout 60 "${OVS_BIN}" --install-extension "$ext" --extensions-dir "${OVS_EXTENSIONS_DIR}"; then \
  echo "Failed to install extension: $ext"; \
  fi; \
  done; \
  echo "--- Final Extension List ---"; \
  "${OVS_BIN}" --extensions-dir "${OVS_EXTENSIONS_DIR}" --list-extensions 2>/dev/null || echo "No extensions installed"; \
  else \
  echo "No extensions.txt found or file is empty, skipping extension installation"; \
  fi

# Place VS Code settings into the user-data location
RUN set -eux; \
  if [ -f settings.json ] && [ -s settings.json ]; then \
  echo "Applying VS Code settings..."; \
  if command -v jq >/dev/null && ! jq empty settings.json; then \
  echo "Invalid JSON in settings.json, skipping"; \
  else \
  mkdir -p "${OVS_USER_DATA_DIR}/Machine"; \
  cp settings.json "${OVS_USER_DATA_DIR}/Machine/settings.json"; \
  echo "VS Code settings applied"; \
  fi; \
  else \
  echo "No settings.json found or file is empty, using defaults"; \
  fi

WORKDIR /workspace