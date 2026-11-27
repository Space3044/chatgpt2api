#!/bin/sh
set -eu

seed_root=/opt/chatgpt2api
runtime_root=/app
marker_name=.chatgpt2api-image-version
seed_version="$(tr -d '\r\n' < "${seed_root}/VERSION")"
installed_image_version=""

if [ -f "${runtime_root}/${marker_name}" ]; then
  installed_image_version="$(tr -d '\r\n' < "${runtime_root}/${marker_name}")"
fi

if [ ! -f "${runtime_root}/VERSION" ] || [ "${installed_image_version}" != "${seed_version}" ]; then
  mkdir -p "${runtime_root}"
  find "${runtime_root}" -mindepth 1 -maxdepth 1 \
    ! -name data \
    ! -name config.json \
    ! -name .venv \
    ! -name "${marker_name}" \
    -exec rm -rf -- {} +

  for source in "${seed_root}"/* "${seed_root}"/.[!.]* "${seed_root}"/..?*; do
    [ -e "${source}" ] || continue
    [ "$(basename "${source}")" = ".venv" ] && continue
    cp -a "${source}" "${runtime_root}/"
  done

  marker_tmp="${runtime_root}/${marker_name}.tmp"
  printf '%s\n' "${seed_version}" > "${marker_tmp}"
  mv "${marker_tmp}" "${runtime_root}/${marker_name}"
fi

cd "${runtime_root}"
uv sync --frozen --no-dev --no-install-project

# cometix-asr sidecar: runs on loopback (127.0.0.1:3101) for the Python
# backend only. HOME is set to the runtime root so the device-id cache
# (~/.cache/cometix-asr/did.json) persists in the runtime volume.
# It is started in the background and the Python process replaces this
# shell via exec below; the sidecar keeps running as a child of PID 1.
# If COMETIX_APP_KEY is unset, live transcription still works; only
# the standalone RunPostAsr (fmt + NER) fails, with a clear error.
if [ -x "${runtime_root}/asr-server" ]; then
  HOME="${runtime_root}" "${runtime_root}/asr-server" >> "${runtime_root}/asr-sidecar.log" 2>&1 &
fi

exec "$@"
