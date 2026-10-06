"""Installer syntax and offline installation flows."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ormas_subnet import __version__

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
COMMIT = "a" * 40


def test_installer_bash_syntax():
    assert INSTALLER.is_file(), "public installer is missing"
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_installer_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if not shellcheck:
        pytest.skip("shellcheck is unavailable")
    subprocess.run([shellcheck, str(INSTALLER)], check=True)


def test_miner_version(capsys):
    from ormas_subnet.miner_cli import main

    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"ormas-miner {__version__}"


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls"
    env = dict(os.environ, HOME=str(home), PATH=f"{commands}:/usr/bin:/bin",
               INSTALL_LOG=str(log), SHELL="/bin/bash")
    env.pop("ORMAS_MINER_REF", None)
    executable(commands / "uname", "printf 'Linux\\n'\n")
    executable(commands / "git", "exit 0\n")
    return home, commands, log, env


def executable(path, content):
    path.write_text("#!/bin/bash\nset -eu\n" + content)
    path.chmod(0o755)


def python_command(commands, name, *, valid=True, venv_ok=True):
    path = commands / name
    executable(path, f'''printf '%s\\n' '{name}' "$*" >> "$INSTALL_LOG"
if [ "$1" = '-c' ]; then
    exit {0 if valid else 1}
fi
if [ "$1" = '-' ]; then
    cat >/dev/null
    printf 'Installed commit: {COMMIT}\\n'
    exit 0
fi
if [ "$1" = '-m' ] && [ "$2" = 'venv' ]; then
    for last; do :; done
    if [ "$3" = '--clear' ]; then rm -rf "$last"; fi
    mkdir -p "$last/bin"
    cp "$0" "$last/bin/python"
    exit {0 if venv_ok else 1}
fi
if [ "$1" = '-m' ] && [ "$2" = 'pip' ]; then
    cat > "$(dirname "$0")/ormas-miner" <<'COMMAND'
#!/bin/bash
printf 'ormas-miner 0.0.1\\n'
COMMAND
    chmod +x "$(dirname "$0")/ormas-miner"
    exit 0
fi
exit 1
''')
    return path


def uv_command(commands, env):
    python = python_command(commands, "seed-python")
    env["TEST_PYTHON"] = str(python)
    executable(commands / "uv", '''printf 'uv %s\\n' "$*" >> "$INSTALL_LOG"
for last; do :; done
if [ -d "$last" ]; then
    case " $* " in
        *' --clear '*) rm -rf "$last" ;;
        *) exit 1 ;;
    esac
fi
mkdir -p "$last/bin"
cp "$TEST_PYTHON" "$last/bin/python"
''')


def install(env):
    return subprocess.run(["bash", str(INSTALLER)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("shell,export", [
    ("/bin/bash", 'export PATH="$HOME/.local/bin:$PATH"'),
    ("/bin/zsh", 'export PATH="$HOME/.local/bin:$PATH"'),
    ("/usr/bin/fish", 'set -gx PATH "$HOME/.local/bin" $PATH'),
])
def test_python_fallback_upgrade_and_shell_path(setup, shell, export):
    home, commands, log, env = setup
    python_command(commands, "python3.13", valid=False)
    python_command(commands, "python3.12")
    env["SHELL"] = shell
    env["ORMAS_MINER_REF"] = "test-release"
    first = install(env)
    assert first.returncode == 0, first.stderr
    assert export in first.stdout
    assert f"Installed commit: {COMMIT}" in first.stdout
    assert first.stdout.endswith("Next: ormas-miner login && ormas-miner doctor\n")
    link = home / ".local/bin/ormas-miner"
    assert link.is_symlink()
    assert link.resolve() == home / ".ormas-miner/venv/bin/ormas-miner"
    calls = log.read_text()
    spec = "ormas-subnet @ git+https://github.com/heroncovelabs/ormas-subnet@test-release"
    forced = f"pip install --upgrade --force-reinstall --no-deps {spec}"
    dependencies = f"pip install {spec}"
    assert forced in calls and dependencies in calls
    assert calls.index(forced) < calls.index(dependencies)
    assert calls.index("python3.13") < calls.index("python3.12")
    second = install(env)
    assert second.returncode == 0, second.stderr
    assert log.read_text().count(forced) == 2
    assert log.read_text().count(dependencies) == 2


def test_uv_is_first_choice(setup):
    _, commands, log, env = setup
    uv_command(commands, env)
    python_command(commands, "python3.13")
    result = install(env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert calls.startswith("uv venv --clear --python 3.12")
    assert "python3.13" not in calls
    assert "ormas-subnet@main" in calls


@pytest.mark.parametrize("use_uv", [False, True])
def test_missing_venv_interpreter_is_recreated(setup, use_uv):
    home, commands, log, env = setup
    venv = home / ".ormas-miner/venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(home / "removed-python")
    if use_uv:
        uv_command(commands, env)
    else:
        python_command(commands, "python3.13")
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert (venv / "bin/python").is_file()
    assert "--clear" in log.read_text()


def test_uv_failure_uses_python(setup):
    _, commands, log, env = setup
    executable(commands / "uv", "exit 1\n")
    python_command(commands, "python3.13")
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert "python3.13" in log.read_text()


def test_failed_venv_creation_tries_next_interpreter(setup):
    _, commands, log, env = setup
    python_command(commands, "python3.13", venv_ok=False)
    python_command(commands, "python3.12")
    result = install(env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "python3.13\n-m venv --clear" in calls
    assert "python3.12\n-m venv --clear" in calls


def test_all_venv_failures_remove_partial_environment(setup):
    home, commands, _, env = setup
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
        python_command(commands, name, venv_ok=False)
    for _ in range(2):
        result = install(env)
        assert result.returncode != 0
        assert "install python3-venv (Debian/Ubuntu) or uv" in result.stderr
        assert not (home / ".ormas-miner/venv").exists()


def test_missing_supported_python_is_clear(setup):
    _, commands, _, env = setup
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
        python_command(commands, name, valid=False)
    result = install(env)
    assert result.returncode != 0
    assert "Python >= 3.10" in result.stderr


def test_path_already_set_has_no_export(setup):
    home, commands, _, env = setup
    python_command(commands, "python3.13")
    env["PATH"] = str(home / ".local/bin") + ":" + env["PATH"]
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert "export PATH=" not in result.stdout


def test_darwin_bash_uses_login_profile(setup):
    _, commands, _, env = setup
    executable(commands / "uname", "printf 'Darwin\\n'\n")
    executable(commands / "xcode-select", "exit 0\n")
    python_command(commands, "python3.13")
    result = install(env)
    assert result.returncode == 0, result.stderr
    assert "~/.bash_profile" in result.stdout
    assert "~/.bashrc" not in result.stdout


def test_missing_git_stops_before_pip(setup, tmp_path):
    _, commands, log, env = setup
    (commands / "git").unlink()
    python_command(commands, "python3.13")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    for name in ("bash", "mkdir", "ln", "dirname", "cat", "chmod", "cp", "rm"):
        (isolated / name).symlink_to(shutil.which(name))
    env["PATH"] = f"{commands}:{isolated}"
    result = install(env)
    assert result.returncode != 0
    assert "git" in result.stderr and "Install" in result.stderr
    assert not log.exists() or "pip" not in log.read_text()


def test_missing_darwin_tools_stops_before_pip(setup):
    _, commands, log, env = setup
    executable(commands / "uname", "printf 'Darwin\\n'\n")
    executable(commands / "xcode-select", "exit 1\n")
    python_command(commands, "python3.13")
    result = install(env)
    assert result.returncode != 0
    assert "xcode-select --install" in result.stderr
    assert not log.exists() or "pip" not in log.read_text()


def test_truncated_download_has_no_install_side_effects(setup):
    home, commands, log, env = setup
    python_command(commands, "python3.13")
    body = INSTALLER.read_text()
    truncated = body[:body.index('ln -sfn')]
    result = subprocess.run(["bash"], input=truncated, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (home / ".ormas-miner").exists()
    assert not log.exists()


def test_real_pip_upgrade_of_fixed_version_offline(tmp_path):
    env = dict(os.environ, PIP_NO_INDEX="1", PIP_NO_BUILD_ISOLATION="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
    env["PATH"] = "/Library/Developer/CommandLineTools/usr/bin:" + env.get("PATH", os.defpath)
    git = shutil.which("git", path=env["PATH"])
    if not git:
        pytest.skip("git is unavailable")
    probe = subprocess.run([sys.executable, "-c", "import pip, setuptools, wheel"],
                           env=env, capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("offline pip build tools are unavailable")
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "setup.py").write_text(
        'from setuptools import setup\n'
        'setup(name="ormas-subnet", version="0.0.1", py_modules=["installed_marker"],\n'
        '      entry_points={"console_scripts": ["ormas-miner=installed_marker:main"]})\n'
    )

    def git_run(*args):
        result = subprocess.run([git, *args], cwd=repo, env=env, check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()

    git_run("init", "-b", "main")

    def commit(marker):
        (repo / "installed_marker.py").write_text(f'def main():\n    print("{marker}")\n')
        git_run("add", ".")
        git_run("-c", "user.email=miner@example.test", "-c", "user.name=Test Miner",
                "commit", "-m", marker)
        return git_run("rev-parse", "HEAD")

    first_commit = commit("first")
    home = tmp_path / "home"
    env["HOME"] = str(home)
    env["ORMAS_MINER_REF"] = "main"
    venv = home / ".ormas-miner/venv"
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
                   env=env, check=True, capture_output=True, text=True)
    script = tmp_path / "install-local.sh"
    script.write_text(INSTALLER.read_text().replace(
        "https://github.com/heroncovelabs/ormas-subnet", repo.as_uri()))
    first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert f"Installed commit: {first_commit}" in first.stdout
    second_commit = commit("second")
    second = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert f"Installed commit: {second_commit}" in second.stdout
    result = subprocess.run([str(home / ".local/bin/ormas-miner"), "--version"],
                            env=env, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "second"
