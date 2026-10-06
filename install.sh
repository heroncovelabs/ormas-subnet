#!/usr/bin/env bash

python_ok() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

main() {
    set -euo pipefail

    local venv="$HOME/.ormas-miner/venv"
    local local_bin="$HOME/.local/bin"
    local platform ready python spec shell_name bash_profile
    platform="$(uname -s)"

    if [[ "$platform" == Darwin ]] && ! xcode-select -p >/dev/null 2>&1; then
        printf 'Install the command-line tools with xcode-select --install, then retry.\n' >&2
        exit 1
    fi
    if ! command -v git >/dev/null 2>&1; then
        printf 'Install git, then run this installer again.\n' >&2
        exit 1
    fi

    mkdir -p "$HOME/.ormas-miner" "$local_bin"
    chmod 700 "$HOME/.ormas-miner"
    ready=0
    if [[ -x "$venv/bin/python" ]] && python_ok "$venv/bin/python"; then
        ready=1
    else
        if command -v uv >/dev/null 2>&1; then
            if uv venv --clear --python 3.12 --seed "$venv" && python_ok "$venv/bin/python"; then
                ready=1
            fi
        fi
        if [[ "$ready" -eq 0 ]]; then
            for python in python3.13 python3.12 python3.11 python3.10 python3; do
                if command -v "$python" >/dev/null 2>&1 && python_ok "$python"; then
                    if "$python" -m venv --clear "$venv"; then
                        ready=1
                        break
                    fi
                fi
            done
        fi
    fi

    if [[ "$ready" -eq 0 ]]; then
        rm -rf "$venv"
        printf 'Python >= 3.10 is required; install python3-venv (Debian/Ubuntu) or uv, then retry.\n' >&2
        exit 1
    fi

    if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
        "$venv/bin/python" -m ensurepip --upgrade
    fi
    spec="ormas-subnet @ git+https://github.com/heroncovelabs/ormas-subnet@${ORMAS_MINER_REF:-main}"
    "$venv/bin/python" -m pip install --upgrade --force-reinstall --no-deps "$spec"
    "$venv/bin/python" -m pip install "$spec"
    ln -sfn "$venv/bin/ormas-miner" "$local_bin/ormas-miner"

    case ":${PATH:-}:" in
        *":$local_bin:"*) ;;
        *)
            shell_name="${SHELL:-}"
            case "${shell_name##*/}" in
                fish)
                    printf 'Add this to ~/.config/fish/config.fish and run it in your shell:\n'
                    printf '%s\n' "set -gx PATH \"\$HOME/.local/bin\" \$PATH"
                    ;;
                zsh)
                    printf 'Add this to ~/.zshrc and run it in your shell:\n'
                    printf '%s\n' "export PATH=\"\$HOME/.local/bin:\$PATH\""
                    ;;
                *)
                    bash_profile='~/.bashrc'
                    if [[ "$platform" == Darwin ]]; then
                        bash_profile='~/.bash_profile'
                    fi
                    printf 'Add this to %s and run it in your shell:\n' "$bash_profile"
                    printf '%s\n' "export PATH=\"\$HOME/.local/bin:\$PATH\""
                    ;;
            esac
            ;;
    esac

    "$local_bin/ormas-miner" --version
    "$venv/bin/python" - <<'PYTHON'
import json
from importlib.metadata import distribution

metadata = json.loads(distribution("ormas-subnet").read_text("direct_url.json"))
print("Installed commit:", metadata["vcs_info"]["commit_id"])
PYTHON
    printf 'Next: ormas-miner login && ormas-miner doctor\n'
}

main "$@"
