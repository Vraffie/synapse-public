"""Static checks of the Arch package files: no makepkg, no network, no Arch tooling needed."""
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "packaging" / "arch"
PKGBUILD = (ARCH / "PKGBUILD").read_text()
INSTALL = (ARCH / "synapse-agent-control.install").read_text()
PACKAGED_UNIT = (ARCH / "synapse-daemon.service").read_text()
USER_UNIT = (ROOT / "packaging" / "systemd" / "synapse-daemon.service").read_text()


def assignment(name: str) -> str:
    m = re.search(r"^%s=(.*)$" % name, PKGBUILD, re.M)
    assert m, name
    return m.group(1)


def function_body(text: str, name: str) -> str:
    m = re.search(r"^%s\(\) \{\n(.*?)^\}" % re.escape(name), text, re.M | re.S)
    assert m, name
    return m.group(1)


def directives(unit: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in unit.splitlines():
        if line.strip() and not line.lstrip().startswith(("#", "[")) and "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(key, []).append(value)
    return out


def test_pkgbuild_identity_and_dependencies():
    assert assignment("pkgname") == "synapse-agent-control"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'name = "synapse-agent-control"' in pyproject
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert assignment("pkgver") == version
    depends = shlex.split(assignment("depends").strip("()"))
    assert "python" in depends
    for dep in ("python-fastapi", "python-pydantic", "uvicorn", "python-websockets"):
        assert dep in depends
    makedepends = shlex.split(assignment("makedepends").strip("()"))
    assert {"python-build", "python-installer", "python-setuptools"} <= set(makedepends)
    # mcp 2.x breaks the MCP gateway (documented), so it must not be suggested.
    assert "python-mcp" not in assignment("optdepends")
    assert "pyside6" in assignment("optdepends")
    assert assignment("install") == "synapse-agent-control.install"


def test_checksum_skip_is_only_the_documented_local_tarball_placeholder():
    assert assignment("sha256sums") == "('SKIP')"
    assert assignment("source") == '("$pkgname-$pkgver.tar.gz")'
    assert "build.sh" in PKGBUILD and "SKIP" in PKGBUILD.split("pkgname=")[0]
    script = (ARCH / "build.sh").read_text()
    assert "sha256sum" in script and "sha256sums=" in script  # the real digest replaces SKIP before makepkg


def test_build_and_package_use_no_network_privilege_or_pip():
    code = function_body(PKGBUILD, "build") + function_body(PKGBUILD, "package")
    for forbidden in ("sudo", "curl", "wget", "pip ", "pip3", "git clone", "git+", "http://", "https://", "| sh", "| bash"):
        assert forbidden not in code, forbidden
    assert "--no-isolation" in code  # an isolated build would download setuptools
    assert 'install -Dm644 packaging/arch/synapse-daemon.service' in code
    assert "/usr/lib/systemd/user/synapse-daemon.service" in code


def test_packaged_unit_differs_from_the_user_unit_only_in_exec_start_and_documentation():
    packaged, user = directives(PACKAGED_UNIT), directives(USER_UNIT)
    assert set(packaged) == set(user)
    differing = {k for k in user if packaged[k] != user[k]}
    assert differing == {"Documentation", "ExecStart"}
    (exec_start,) = packaged["ExecStart"]
    assert exec_start.startswith("/usr/bin/python -m synapse.supervisor --state-dir %S/synapse ")
    assert exec_start.split(" ", 1)[1] == user["ExecStart"][0].split(" ", 1)[1]  # same supervisor arguments


def test_hardening_directives_are_all_still_present_and_unweakened():
    packaged = directives(PACKAGED_UNIT)
    required = {
        "NoNewPrivileges": "yes", "PrivateTmp": "yes", "PrivateDevices": "yes", "ProtectSystem": "strict",
        "ProtectHome": "read-only", "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes", "ProtectClock": "yes",
        "ProtectHostname": "yes", "RestrictAddressFamilies": "AF_UNIX", "RestrictNamespaces": "yes",
        "RestrictRealtime": "yes", "RestrictSUIDSGID": "yes", "LockPersonality": "yes",
        "MemoryDenyWriteExecute": "yes", "SystemCallArchitectures": "native", "CapabilityBoundingSet": "",
        "IPAddressDeny": "any", "UMask": "0077", "StateDirectory": "synapse", "StateDirectoryMode": "0700",
    }
    for key, value in required.items():
        assert packaged.get(key) == [value], key
    assert "AF_INET" not in PACKAGED_UNIT and "IPAddressAllow" not in PACKAGED_UNIT


def test_install_script_only_prints_and_never_starts_or_touches_user_data():
    for hook in ("post_install", "pre_upgrade", "post_upgrade", "pre_remove"):
        assert re.search(r"^%s\(\) \{" % hook, INSTALL, re.M), hook
    executable = "\n".join(line for line in INSTALL.splitlines() if not line.lstrip().startswith("#"))
    # Strip the quoted message heredocs: the instructions legitimately mention systemctl/agentctl.
    code = re.sub(r"cat <<'MSG'\n.*?\nMSG\n", "", executable, flags=re.S)
    for forbidden in ("systemctl", "rm ", "mv ", "cp ", "install -", "mkdir", "chmod", "chown", "sudo", "agentctl", "pacman"):
        assert forbidden not in code, forbidden
    assert "NOT touched" in INSTALL  # uninstall promises not to delete user data


def test_shell_scripts_parse():
    for path in (ARCH / "build.sh", ARCH / "PKGBUILD", ARCH / "synapse-agent-control.install"):
        done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, (path.name, done.stderr)


def test_pyproject_pins_are_consistent_with_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert pyproject.count('"mcp>=1.20,<2"') == 2  # test + mcp extras; Arch's python-mcp 2.x is unsupported
    assert '"pydantic>=2,<3"' in pyproject and '"websockets>=13,<18"' in pyproject
