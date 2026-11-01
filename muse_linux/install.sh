#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${APP_DIR}/" in
  "${HOME}/"*) ;;
  *) echo "请先把压缩包解压到家目录下（例如 ~/vidpick/），再运行安装。" >&2; exit 1 ;;
esac

export PIP_CACHE_DIR="${APP_DIR}/.cache/pip"
export XDG_CACHE_HOME="${APP_DIR}/.cache"
export PLAYWRIGHT_BROWSERS_PATH="${APP_DIR}/.cache/ms-playwright"
export TMPDIR="${APP_DIR}/.cache/tmp"
mkdir -p "${PIP_CACHE_DIR}" "${PLAYWRIGHT_BROWSERS_PATH}" "${TMPDIR}" "${APP_DIR}/output" "${APP_DIR}/logs"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请确认 Muse 的 Python 3.10～3.12 已安装。" >&2
  exit 1
fi
python3 -c 'import sys; assert (3, 10) <= sys.version_info[:2] <= (3, 12)' || {
  echo "仅支持 Python 3.10～3.12。" >&2
  exit 1
}

if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
  python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check -r "${APP_DIR}/requirements.txt"

if [ ! -x /opt/meta-chromium/chrome ]; then
  "${APP_DIR}/.venv/bin/python" -m playwright install chromium
fi

echo "Vidpick Muse Linux 安装成功。运行：${APP_DIR}/.venv/bin/python ${APP_DIR}/vidpick_cli.py '抖音链接或分享文案'"
