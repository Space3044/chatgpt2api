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

# The pure-Python ASR client (services/cometix_asr.py) caches its
# device id in ~/.cache/cometix-asr/did.json. Point HOME at the
# runtime root so the cache lands in the runtime volume (and is
# re-registered when a new VERSION reseeds /app), exactly as the
# removed Go sidecar had it.
export HOME="${runtime_root}"

exec "$@"
