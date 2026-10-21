# shellcheck shell=bash
# Appended to the generated interpreter after its quoted image settings.
set -euo pipefail
# Derive the project from this file's real location (.venv -> .venvs/<env>/),
# so moving or renaming the repository never requires a rebuild.
SHIM_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(CDPATH='' cd -- "$SHIM_DIR/../../.." && pwd -P)"
# Docker bind-mount syntax cannot represent these path delimiters reliably.
case "$PROJECT_ROOT" in *:*|*,*|*$'\n'*) printf 'Project paths containing colons, commas, or newlines are unsupported: %s\n' "$PROJECT_ROOT" >&2; exit 1 ;; esac
# Zed may probe an interpreter from its own working directory. Use the project
# root in that case; never try to mount an unrelated working directory.
HOST_WORKDIR="$(pwd -P)"
case "$HOST_WORKDIR/" in "$PROJECT_ROOT/"*) ;; *) HOST_WORKDIR="$PROJECT_ROOT" ;; esac

DOCKER_ARGS=(--rm --init --label "planetary_helpers.environment=$RESOURCE_ID"
  --user "$(id -u):$(id -g)" --env HOME=/tmp --env MPLCONFIGDIR=/tmp/matplotlib
  --mount "type=bind,source=$PROJECT_ROOT,target=$PROJECT_ROOT"
  --workdir "$HOST_WORKDIR")
KERNEL=false
# A nonempty command array also works with macOS Bash 3.2 under set -u.
PYTHON_COMMAND=(python)
ARGS=("$@")
index=0
while [ "$index" -lt "$#" ]; do
  arg="${ARGS[index]}"
  case "$arg" in
    -m)
      if [ "${ARGS[index+1]:-}" = ipykernel_launcher ]; then
        KERNEL=true
        MODULE_ARGS=("${ARGS[@]:index+2}")
      fi
      break ;;
    -u|-B|-E|-I|-s|-S|-O|-OO|-q) PYTHON_COMMAND+=("$arg") ;;
    -X|-W)
      PYTHON_COMMAND+=("$arg" "${ARGS[index+1]:-}")
      index=$((index + 1)) ;;
    *) break ;;
  esac
  index=$((index + 1))
done
if ! "$KERNEL"; then
  DOCKER_ARGS+=(-i)
  if [ -t 0 ] && [ -t 1 ]; then DOCKER_ARGS+=(-t); fi
  exec docker run "${DOCKER_ARGS[@]}" "$IMAGE_NAME" python "$@"
fi

set -- "${MODULE_ARGS[@]}"
CONNECTION_FILE=""
for ((index=1; index <= $#; index++)); do
  arg="${!index}"
  case "$arg" in
    -f|--connection-file)
      next=$((index + 1)); CONNECTION_FILE="${!next:-}" ;;
    --connection-file=*) CONNECTION_FILE="${arg#*=}" ;;
  esac
done
[ -f "$CONNECTION_FILE" ] || { printf 'Kernel requires -f <existing connection file>.\n' >&2; exit 1; }
CONNECTION_FILE="$(cd "$(dirname "$CONNECTION_FILE")" && pwd -P)/$(basename "$CONNECTION_FILE")"
case "$CONNECTION_FILE" in *:*|*,*|*$'\n'*) printf 'Unsupported connection file path.\n' >&2; exit 1 ;; esac
# Zed's WSL kernel launcher connects after only two seconds. When available,
# jq avoids a second Docker startup while validating the connection file.
if command -v jq >/dev/null 2>&1; then
  PORTS="$(jq -er '
    . as $c
    | [$c.stdin_port, $c.control_port, $c.hb_port, $c.shell_port, $c.iopub_port] as $ports
    | if $c.transport == "tcp" and $c.ip == "127.0.0.1"
        and ($c.key | type == "string" and length > 0)
        and $c.signature_scheme == "hmac-sha256"
        and ($ports | all(.[]; type == "number" and . == floor and . >= 1 and . <= 65535))
        and ($ports | unique | length) == 5
      then $ports[]
      else error("Invalid kernel connection file")
      end
  ' "$CONNECTION_FILE")"
else
  # Docker Python remains the fallback; no extra host dependency is required.
  PORTS=$(docker run --rm -i "$IMAGE_NAME" python -c '
import json, sys
try:
    c = json.load(sys.stdin)
    if c.get("transport") != "tcp" or c.get("ip") != "127.0.0.1":
        raise ValueError("only TCP on IPv4 loopback is supported")
    if not c.get("key") or c.get("signature_scheme") != "hmac-sha256":
        raise ValueError("a nonempty HMAC-SHA256 key is required")
    ports = [c[k + "_port"] for k in ("stdin", "control", "hb", "shell", "iopub")]
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 5:
        raise ValueError("five distinct ports in 1..65535 are required")
    print("\n".join(map(str, ports)))
except (ValueError, KeyError, TypeError) as e:
    sys.exit("Invalid kernel connection file: " + str(e))
' < "$CONNECTION_FILE")
fi
# Native Docker in WSL shares the distro's network namespace with --network host.
# Avoid five Docker port proxies: Zed connects from Windows to WSL localhost
# shortly after launch, and the legacy host-network kernel used this path.
KERNEL_BIND_IP=0.0.0.0
if [ -r /proc/sys/kernel/osrelease ] && grep -qi microsoft /proc/sys/kernel/osrelease \
    && [ "$(docker info --format '{{.Name}}')" = "$(hostname)" ]; then
  DOCKER_ARGS+=(--network host)
  KERNEL_BIND_IP=127.0.0.1
else
  while IFS= read -r port; do
    DOCKER_ARGS+=(--publish "127.0.0.1:$port:$port")
  done <<< "$PORTS"
fi
DOCKER_ARGS+=(--mount "type=bind,source=$CONNECTION_FILE,target=/tmp/host-connection.json,readonly")
CONTAINER_NAME="$RESOURCE_ID-kernel-$$-$RANDOM"
CLIENT_PID=""
WATCHER_PID=""
cleanup() {
  trap - EXIT INT TERM HUP
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [ -n "$CLIENT_PID" ]; then kill "$CLIENT_PID" 2>/dev/null || true; wait "$CLIENT_PID" 2>/dev/null || true; fi
  if [ -n "$WATCHER_PID" ]; then kill "$WATCHER_PID" 2>/dev/null || true; wait "$WATCHER_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
# Zed can forcibly kill the launcher. An independent watcher removes its exact
# container even when SIGKILL prevents the launcher's EXIT trap from running.
LAUNCHER_PID=$$
(
  trap - EXIT INT TERM HUP
  while kill -0 "$LAUNCHER_PID" 2>/dev/null; do sleep 1; done
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
) </dev/null >/dev/null 2>&1 &
WATCHER_PID=$!
# Keep the original connection file unchanged for the client. Bind to host
# loopback in WSL; with bridge networking, bind to container interfaces and
# publish only on host loopback, never the LAN.
docker run "${DOCKER_ARGS[@]}" --name "$CONTAINER_NAME" "$IMAGE_NAME" "${PYTHON_COMMAND[@]}" -c '
import json, os, runpy, sys
with open("/tmp/host-connection.json") as f:
    c = json.load(f)
c["ip"] = sys.argv[1]
p = "/tmp/kernel-connection.json"
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(c, f)
sys.argv = ["ipykernel_launcher", *sys.argv[2:]]
for i, arg in enumerate(sys.argv):
    if arg in ("-f", "--connection-file"):
        sys.argv[i + 1] = p
    elif arg.startswith("--connection-file="):
        sys.argv[i] = "--connection-file=" + p
runpy.run_module("ipykernel_launcher", run_name="__main__")
' "$KERNEL_BIND_IP" "$@" &
CLIENT_PID=$!
wait "$CLIENT_PID"
