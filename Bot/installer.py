"""Auto-installer for standalone GitHub CLI (gh) binary."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import tarfile
import urllib.request

log = logging.getLogger("codespace-keeper")


def ensure_user_in_passwd() -> None:
    """Ensure current UID exists in /etc/passwd so ssh and ssh-keygen can resolve the user."""
    try:
        import pwd
        uid = os.getuid()
        try:
            pwd.getpwuid(uid)
        except KeyError:
            uname = os.environ.get("USER") or os.environ.get("LOGNAME") or f"user{uid}"
            entry = f"{uname}:x:{uid}:{os.getgid()}:{uname}:/tmp:/bin/bash\n"
            try:
                with open("/etc/passwd", "a", encoding="utf-8") as f:
                    f.write(entry)
            except Exception:
                pass
    except Exception:
        pass


def ensure_fake_getpwuid() -> str | None:
    """Ensure libfake_getpwuid.so exists to intercept missing container UID in /etc/passwd."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    so_path = os.path.join(base_dir, "libfake_getpwuid.so")
    c_path = os.path.join(base_dir, "libfake_getpwuid.c")
    if os.path.exists(so_path):
        return so_path
    if os.path.exists(c_path):
        try:
            import subprocess
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-O2", "-o", so_path, c_path, "-ldl"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            if os.path.exists(so_path):
                return so_path
        except Exception:
            pass
    return None


def ensure_ssh_config() -> None:
    """Ensure system /etc/ssh/ssh_config exists if directory is writable."""
    ensure_user_in_passwd()
    ensure_fake_getpwuid()
    try:
        os.makedirs("/etc/ssh", exist_ok=True)
        config_path = "/etc/ssh/ssh_config"
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("Host *\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n")
            os.chmod(config_path, 0o644)
    except Exception:
        pass


def ensure_gh_installed() -> str:
    """Ensure GitHub CLI (gh) binary is available on PATH.
    
    If not installed on the system, downloads the official static binary into ./bin/gh.
    """
    ensure_user_in_passwd()
    ensure_fake_getpwuid()
    ensure_ssh_config()
    # 1. Check if gh is already on PATH
    gh_path = shutil.which("gh")
    if gh_path:
        return gh_path

    # 2. Check local ./bin/gh
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bin_dir = os.path.join(base_dir, "bin")
    local_gh = os.path.join(bin_dir, "gh")

    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    gh_path = shutil.which("gh")
    if gh_path:
        return gh_path

    if os.path.exists(local_gh) and os.access(local_gh, os.X_OK):
        return local_gh

    # 3. Download static gh release binary
    log.info("GitHub CLI (gh) not found in PATH. Auto-downloading static binary for non-root host...")
    os.makedirs(bin_dir, exist_ok=True)

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("arm"):
        arch = "armv6"
    elif machine in ("i386", "i686"):
        arch = "386"
    else:
        arch = "amd64"

    version = "2.67.0"
    url = f"https://github.com/cli/cli/releases/download/v{version}/gh_{version}_linux_{arch}.tar.gz"
    tar_path = os.path.join(bin_dir, "gh.tar.gz")

    try:
        urllib.request.urlretrieve(url, tar_path)
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/bin/gh") or member.name == "gh":
                    member.name = "gh"
                    tar.extract(member, path=bin_dir)
                    break
        if os.path.exists(tar_path):
            os.remove(tar_path)
        os.chmod(local_gh, 0o755)
        log.info("Successfully downloaded gh binary to %s", local_gh)
        return local_gh
    except Exception as exc:
        log.error("Failed to auto-download gh binary: %s", exc)
        raise SystemExit(
            f"GitHub CLI (gh) is not installed and auto-download failed: {exc}\n"
            "Please install gh manually or place the gh binary in ./bin/gh"
        )
