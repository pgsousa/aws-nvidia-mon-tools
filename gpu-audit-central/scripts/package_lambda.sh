#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$1"
BUILD_DIR="$2"
PYTHON_RUNTIME="${3:-python3.12}"
REQUIREMENTS_FILE="${SOURCE_DIR}/requirements.txt"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

cp "${SOURCE_DIR}/lambda_function.py" "${BUILD_DIR}/lambda_function.py"

if [[ -f "${REQUIREMENTS_FILE}" ]]; then
  python3 -m pip install \
    --upgrade \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version "${PYTHON_RUNTIME#python}" \
    --target "${BUILD_DIR}" \
    -r "${REQUIREMENTS_FILE}"
fi
