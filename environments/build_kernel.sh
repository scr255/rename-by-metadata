#!/bin/bash
set -euo pipefail

error() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || error "Usage: $0 <env-folder> [--path|--remove|--rebuild]"
ENV_NAME="$1"
[[ "$ENV_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || error "Environment must be a single folder name (letters, digits, underscores, hyphens)."
ACTION="${2:-build}"
case "$ACTION" in build|--path|--remove|--rebuild) ;; *) error "Unknown option: $ACTION" ;; esac
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_DIR="$SCRIPT_DIR/$ENV_NAME"
VENVS_DIR="$PROJECT_ROOT/.venvs"
VENV_DIR="$VENVS_DIR/$ENV_NAME"
VENV_LINK="$PROJECT_ROOT/.venv"
PROJECT_ID_FILE="$VENVS_DIR/.project-id"
IPYKERNEL_LAYER='RUN uv pip install --no-cache --python "$(command -v python)" ipykernel'

if [ "$ACTION" = --path ]; then
  [ -x "$VENV_DIR/bin/python" ] || error "Environment not built. Run: $0 $ENV_NAME"
  printf '%s\n' "$VENV_DIR/bin/python"
  exit 0
fi
command -v docker >/dev/null 2>&1 || error "Docker is required."
docker info >/dev/null 2>&1 || error "Docker is not running or accessible."

# A random per-clone ID keeps checkouts apart and survives moving the repo.
if [ "$ACTION" != --remove ] && [ ! -e "$PROJECT_ID_FILE" ]; then
  mkdir -p -- "$VENVS_DIR"
  od -An -N6 -tx1 /dev/urandom | tr -d ' \t\n' > "$PROJECT_ID_FILE"
fi
[ -f "$PROJECT_ID_FILE" ] || error "Missing project ID file (nothing built yet?): $PROJECT_ID_FILE"
PROJECT_ID="$(cat "$PROJECT_ID_FILE")"
[[ "$PROJECT_ID" =~ ^[0-9a-f]{12}$ ]] || error "Malformed project ID in $PROJECT_ID_FILE (expected 12 lowercase hex characters)."
RESOURCE_ID="$(printf '%s' "$ENV_NAME" | tr '[:upper:]' '[:lower:]')-$PROJECT_ID"
DOCKER_REPOSITORY_RE='^[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*$'
[[ "$RESOURCE_ID" =~ $DOCKER_REPOSITORY_RE ]] || error "Invalid Docker image name '$RESOURCE_ID'; rename the environment folder (no leading, trailing, or repeated separators)."

# Only touch environment folders this script created for this project.
require_owned_venv() {
  if [ -e "$VENV_DIR" ] || [ -L "$VENV_DIR" ]; then
    [ ! -L "$VENV_DIR" ] && [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/.planetary_helpers" ] \
      && [ "$(cat "$VENV_DIR/.planetary_helpers")" = "$RESOURCE_ID" ] || error "Refusing to $1 unowned directory: $VENV_DIR"
  fi
}
# Ours: a link into .venvs/ (possibly dangling).
venv_link_is_ours() { [ -L "$VENV_LINK" ] && [[ "$(readlink "$VENV_LINK")" == .venvs/* ]]; }

if [ "$ACTION" = --remove ]; then
  require_owned_venv remove
  # Labels survive image rebuilds; never remove containers by image ancestry.
  while IFS= read -r container; do
    [ -z "$container" ] || docker rm -f "$container" >/dev/null
  done < <(docker ps -aq --filter "label=planetary_helpers.environment=$RESOURCE_ID")
  while read -r image_id image_ref; do
    [ "$image_ref" != "<none>:<none>" ] || image_ref="$image_id"
    docker image rm "$image_ref" >/dev/null || printf 'Warning: could not remove image %s\n' "$image_ref" >&2
  done < <(docker images --no-trunc --filter "label=planetary_helpers.environment=$RESOURCE_ID" --format '{{.ID}} {{.Repository}}:{{.Tag}}')
  rm -rf -- "$VENV_DIR"
  if [ -L "$VENV_LINK" ] && [ "$(readlink "$VENV_LINK")" = ".venvs/$ENV_NAME" ]; then
    rm -- "$VENV_LINK"
  fi
  printf 'Removed %s.\n' "$RESOURCE_ID"
  exit 0
fi

[ -f "$ENV_DIR/Dockerfile" ] && [ -f "$ENV_DIR/requirements.txt" ] || error "Environment needs Dockerfile and requirements.txt: $ENV_DIR"
if [ -e "$VENV_LINK" ] || [ -L "$VENV_LINK" ]; then
  venv_link_is_ours || error "Existing .venv is not owned by this environment; move it explicitly before building."
fi
require_owned_venv overwrite

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 | awk '{print $1}'; }
else
  error "sha256sum or shasum is required."
fi
# The tag changes whenever the environment folder or an appended layer changes.
CONTENT_HASH="$( (
  cd -- "$ENV_DIR"
  find . -type f ! -name .DS_Store ! -name '.azure-environment.*' | LC_ALL=C sort | while IFS= read -r file; do
    printf '%s %s\n' "$(sha256 < "$file")" "$file"
  done
  printf '%s\n' "$IPYKERNEL_LAYER"
) | sha256 | cut -c1-12)"
[[ "$CONTENT_HASH" =~ ^[0-9a-f]{12}$ ]] || error "Could not hash $ENV_DIR."
IMAGE_NAME="$RESOURCE_ID:$CONTENT_HASH"

BUILD_TAG=""
STAGING=""
cleanup() {
  if [ -n "$BUILD_TAG" ]; then docker image rm "$BUILD_TAG" >/dev/null 2>&1 || true; fi
  if [ -n "$STAGING" ]; then rm -rf -- "$STAGING"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$ACTION" = --rebuild ] || ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  # Build under a temporary tag so a failed build never looks complete.
  BUILD_TAG="$RESOURCE_ID:building-$$"
  printf 'Building %s...\n' "$IMAGE_NAME"
  if [ "$ACTION" = --rebuild ]; then
    docker build --pull --no-cache -t "$BUILD_TAG" "$ENV_DIR"
  else
    docker build -t "$BUILD_TAG" "$ENV_DIR"
  fi
  docker run --rm "$BUILD_TAG" python --version >/dev/null 2>&1 || error "Environment image does not provide python."
  printf 'Adding ipykernel to %s...\n' "$IMAGE_NAME"
  printf 'FROM %s\n%s\n' "$BUILD_TAG" "$IPYKERNEL_LAYER" \
    | docker build --label "planetary_helpers.environment=$RESOURCE_ID" -t "$BUILD_TAG" -f - "$ENV_DIR"
  docker tag "$BUILD_TAG" "$IMAGE_NAME"
  docker image rm "$BUILD_TAG" >/dev/null
  BUILD_TAG=""
  # Reclaim earlier builds; images still used by a kernel are kept.
  NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME")"
  while read -r image_id image_ref; do
    [ "$image_id" != "$NEW_IMAGE_ID" ] && [ "$image_ref" != "$IMAGE_NAME" ] || continue
    [ "$image_ref" != "<none>:<none>" ] || image_ref="$image_id"
    docker image rm "$image_ref" >/dev/null 2>&1 || true
  done < <(docker images --no-trunc --filter "label=planetary_helpers.environment=$RESOURCE_ID" --format '{{.ID}} {{.Repository}}:{{.Tag}}')
else
  printf 'Image %s is up to date; skipping Docker build (use --rebuild to force).\n' "$IMAGE_NAME"
fi
PYTHON_VERSION="$(docker run --rm "$IMAGE_NAME" python -c 'import ipykernel, sys; print(sys.version.split()[0])')" \
  || error "Image $IMAGE_NAME lacks python or ipykernel. Run: $0 $ENV_NAME --rebuild"
printf 'Python %s\n' "$PYTHON_VERSION"

# Stage on the same filesystem so the final swap is a rename.
STAGING="$(mktemp -d "$VENVS_DIR/.staging.XXXXXX")"
chmod 755 "$STAGING"
mkdir "$STAGING/bin"
# Only location-independent settings are embedded; the shim finds the project at runtime.
{
  printf '#!/bin/bash\n'
  printf 'IMAGE_NAME=%q\nRESOURCE_ID=%q\n' "$IMAGE_NAME" "$RESOURCE_ID"
  cat "$SCRIPT_DIR/python.sh"
} > "$STAGING/bin/python"
chmod +x "$STAGING/bin/python"
ln -s python "$STAGING/bin/python3"

# Activation helper for bash and zsh terminals; Python still runs through Docker.
cat > "$STAGING/bin/activate" <<'ACTIVATE'
# Source from bash or zsh. Paths are resolved from this file, so the repo can move.
deactivate () {
    if [ -n "${_OLD_VIRTUAL_PATH:-}" ]; then
        PATH="${_OLD_VIRTUAL_PATH:-}"
        export PATH
        unset _OLD_VIRTUAL_PATH
    fi
    if [ -n "${_OLD_VIRTUAL_PS1:-}" ]; then
        PS1="${_OLD_VIRTUAL_PS1:-}"
        export PS1
        unset _OLD_VIRTUAL_PS1
    fi
    unset VIRTUAL_ENV
    unset VIRTUAL_ENV_PROMPT
    if [ "${1:-}" != "nondestructive" ]; then
        unset -f deactivate
    fi
}
deactivate nondestructive
if [ -n "${BASH_VERSION:-}" ]; then
    _PH_ACTIVATE="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _PH_ACTIVATE="${(%):-%x}"
else
    printf 'Source this file from bash or zsh.\n' >&2
    return 1
fi
VIRTUAL_ENV="$(CDPATH='' cd -- "$(dirname -- "$_PH_ACTIVATE")/.." && pwd -P)"
export VIRTUAL_ENV
_PH_ROOT="${VIRTUAL_ENV%/*/*}"
_PH_PROMPT="${_PH_ROOT##*/}/${VIRTUAL_ENV##*/}"
_OLD_VIRTUAL_PATH="$PATH"
PATH="$VIRTUAL_ENV/bin:$PATH"
export PATH
_OLD_VIRTUAL_PS1="${PS1:-}"
PS1="($_PH_PROMPT) ${PS1:-}"
export PS1
VIRTUAL_ENV_PROMPT="($_PH_PROMPT) "
export VIRTUAL_ENV_PROMPT
unset _PH_ACTIVATE _PH_ROOT _PH_PROMPT
ACTIVATE

printf 'include-system-site-packages = false\nversion = %s\nprompt = %s\n' "$PYTHON_VERSION" "$(basename "$PROJECT_ROOT")/$ENV_NAME" > "$STAGING/pyvenv.cfg"
printf '%s\n' "$RESOURCE_ID" > "$STAGING/.planetary_helpers"

# Swap by rename: running shims keep reading their original (unlinked) file.
rm -rf -- "$VENV_DIR"
mv -- "$STAGING" "$VENV_DIR"
STAGING=""
rm -f -- "$VENV_LINK"
ln -s ".venvs/$ENV_NAME" "$VENV_LINK"
printf 'Ready: %s (active as .venv)\nImage: %s\nRun: .venv/bin/python your_script.py\nZed: select .venv/bin/python as the toolchain (toolchain: select); the REPL uses the active toolchain automatically.\nDependencies: edit %s/requirements.txt and rebuild.\n' "$VENV_DIR/bin/python" "$IMAGE_NAME" "$ENV_DIR"
