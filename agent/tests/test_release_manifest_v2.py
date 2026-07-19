"""金融投研引擎受信发布清单 v2 的真实文件与签名契约测试。"""

from __future__ import annotations

import hashlib
import json
import platform
import stat
import subprocess
import sys
import zipfile

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from vibe_release_manifest import (  # noqa: E402
    ManifestError,
    build_archive,
    build_file_manifest,
    sign_release_manifest,
    verify_archive,
    verify_release_manifest,
)


def _key_material() -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _unsigned_manifest(now: datetime) -> dict:
    return {
        "schema_version": 2,
        "artifact_id": "app.engine.finance",
        "version": "0.1.4",
        "release_sequence": 42,
        "channel": "stable",
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        "minimum_daemon_version": "0.1.0",
        "packages": {
            "darwin-aarch64": {
                "url": "https://cdn.example/vibe-trading.zip",
                "sha256": "a" * 64,
                "compressed_size": 123,
                "installed_size_limit": 456,
                "file_manifest_sha256": "b" * 64,
            }
        },
        "revocations": [],
        "key_id": "finance-release-2026",
    }


def test_file_manifest_and_archive_verify_exact_regular_files(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "agent").mkdir(parents=True)
    (stage / "venv" / "bin").mkdir(parents=True)
    (stage / "agent" / "mcp_server.py").write_text("print('ok')\n", encoding="utf-8")
    (stage / "agent" / "中文说明.md").write_text("金融引擎\n", encoding="utf-8")
    python_bin = stage / "venv" / "bin" / "python"
    python_bin.write_bytes(b"python")
    python_bin.chmod(0o755)

    metadata = build_file_manifest(stage)
    file_manifest = json.loads((stage / "file-manifest.json").read_text(encoding="utf-8"))
    assert file_manifest["schema_version"] == 1
    assert {item["path"] for item in file_manifest["files"]} == {
        "agent/mcp_server.py",
        "agent/中文说明.md",
        "venv/bin/python",
    }
    assert next(item for item in file_manifest["files"] if item["path"] == "venv/bin/python")["executable"]

    archive = tmp_path / "package.zip"
    second_archive = tmp_path / "package-second.zip"
    build_archive(stage, archive)
    build_archive(stage, second_archive)
    assert hashlib.sha256(archive.read_bytes()).digest() == hashlib.sha256(second_archive.read_bytes()).digest()

    verified = verify_archive(archive, metadata["file_manifest_sha256"])
    assert verified["installed_size"] == metadata["installed_size"]
    assert verified["files"] == 4  # 三个 payload 文件 + file-manifest.json


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 测试环境不提供 POSIX symlink 语义")
def test_file_manifest_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    payload = stage / "payload"
    payload.write_bytes(b"payload")

    symlink = stage / "link"
    symlink.symlink_to(payload)
    with pytest.raises(ManifestError, match="符号链接"):
        build_file_manifest(stage)

    symlink.unlink()
    hardlink = stage / "hardlink"
    hardlink.hardlink_to(payload)
    with pytest.raises(ManifestError, match="硬链接"):
        build_file_manifest(stage)


def test_archive_rejects_undeclared_and_special_entries(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "payload").write_bytes(b"payload")
    metadata = build_file_manifest(stage)

    undeclared = tmp_path / "undeclared.zip"
    with zipfile.ZipFile(undeclared, "w") as output:
        output.write(stage / "payload", "payload")
        output.write(stage / "file-manifest.json", "file-manifest.json")
        output.writestr("extra", b"extra")
    with pytest.raises(ManifestError, match="未声明文件"):
        verify_archive(undeclared, metadata["file_manifest_sha256"])

    special = tmp_path / "special.zip"
    with zipfile.ZipFile(special, "w") as output:
        output.write(stage / "payload", "payload")
        output.write(stage / "file-manifest.json", "file-manifest.json")
        info = zipfile.ZipInfo("pipe")
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o600) << 16
        output.writestr(info, b"")
    with pytest.raises(ManifestError, match="特殊文件"):
        verify_archive(special, metadata["file_manifest_sha256"])


def test_archive_write_error_keeps_operating_system_reason(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "payload").write_bytes(b"payload")
    build_file_manifest(stage)
    archive = tmp_path / "package.zip"
    archive.mkdir()

    with pytest.raises(ManifestError, match=r"引擎归档写入失败：.+"):
        build_archive(stage, archive)


def test_signed_manifest_rejects_tamper_expiry_replay_and_revocation() -> None:
    now = datetime(2026, 7, 19, tzinfo=UTC)
    private_pem, public_pem = _key_material()
    signed = sign_release_manifest(_unsigned_manifest(now), private_pem)
    trusted_keys = {"finance-release-2026": public_pem}

    package = verify_release_manifest(
        signed,
        trusted_keys,
        platform="darwin-aarch64",
        now=now,
        accepted_release_sequence=41,
    )
    assert package["sha256"] == "a" * 64

    tampered = json.loads(json.dumps(signed))
    tampered["packages"]["darwin-aarch64"]["url"] = "https://attacker.example/payload.zip"
    with pytest.raises(ManifestError, match="签名"):
        verify_release_manifest(tampered, trusted_keys, platform="darwin-aarch64", now=now)

    with pytest.raises(ManifestError, match="未知签名密钥"):
        verify_release_manifest(signed, {}, platform="darwin-aarch64", now=now)

    with pytest.raises(ManifestError, match="过期"):
        verify_release_manifest(
            signed,
            trusted_keys,
            platform="darwin-aarch64",
            now=now + timedelta(days=30),
        )

    future = _unsigned_manifest(now + timedelta(seconds=1))
    future_signed = sign_release_manifest(future, private_pem)
    with pytest.raises(ManifestError, match="尚未生效"):
        verify_release_manifest(
            future_signed,
            trusted_keys,
            platform="darwin-aarch64",
            now=now,
        )

    with pytest.raises(ManifestError, match="发布序列"):
        verify_release_manifest(
            signed,
            trusted_keys,
            platform="darwin-aarch64",
            now=now,
            accepted_release_sequence=42,
        )

    revoked_unsigned = _unsigned_manifest(now)
    revoked_unsigned["revocations"] = [
        {
            "version": "0.1.4",
            "platform": "darwin-aarch64",
            "sha256": "a" * 64,
            "revoked_at": now.isoformat().replace("+00:00", "Z"),
            "reason": "供应链密钥泄露",
            "critical": True,
        }
    ]
    revoked = sign_release_manifest(revoked_unsigned, private_pem)
    with pytest.raises(ManifestError, match="已撤销"):
        verify_release_manifest(revoked, trusted_keys, platform="darwin-aarch64", now=now)


def test_signed_manifest_rejects_noncanonical_signature() -> None:
    now = datetime(2026, 7, 19, tzinfo=UTC)
    private_pem, public_pem = _key_material()
    signed = sign_release_manifest(_unsigned_manifest(now), private_pem)
    signed["signature"] = signed["signature"].upper()

    with pytest.raises(ManifestError, match="signature 必须是 128 位小写十六进制"):
        verify_release_manifest(
            signed,
            {"finance-release-2026": public_pem},
            platform="darwin-aarch64",
            now=now,
        )


def test_release_manifest_cli_accumulates_platforms_and_resigns(tmp_path: Path) -> None:
    now = datetime(2026, 7, 19, tzinfo=UTC)
    private_pem, public_pem = _key_material()
    private_key = tmp_path / "release-key.pem"
    private_key.write_bytes(private_pem)
    manifest_path = tmp_path / "manifest.json"

    common = [
        sys.executable,
        str(_SCRIPTS / "vibe_release_manifest.py"),
        "upsert-package",
        f"--manifest={manifest_path}",
        "--version=0.1.4",
        "--release-sequence=42",
        "--channel=stable",
        f"--issued-at={now.isoformat().replace('+00:00', 'Z')}",
        f"--expires-at={(now + timedelta(days=30)).isoformat().replace('+00:00', 'Z')}",
        "--minimum-daemon-version=0.1.0",
        "--key-id=finance-release-2026",
        f"--signing-key={private_key}",
        "--revocations-file=",
    ]
    for platform_key, digest in (("darwin-aarch64", "a" * 64), ("linux-x86_64", "c" * 64)):
        subprocess.run(
            [
                *common,
                f"--platform={platform_key}",
                f"--url=https://cdn.example/{platform_key}.zip",
                f"--sha256={digest}",
                "--compressed-size=123",
                "--installed-size-limit=456",
                f"--file-manifest-sha256={'b' * 64}",
            ],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["packages"]) == {"darwin-aarch64", "linux-x86_64"}
    for platform_key in manifest["packages"]:
        verify_release_manifest(
            manifest,
            {"finance-release-2026": public_pem},
            platform=platform_key,
            now=now,
            accepted_release_sequence=41,
        )


def test_no_venv_package_cannot_enter_publish_path() -> None:
    result = subprocess.run(
        [
            "bash",
            str(_SCRIPTS / "package-vibe-trading.sh"),
            "--no-venv",
            "--targets=aarch64",
            "--publish=http://127.0.0.1:8020",
            "--app-pk=1",
            "--admin-token=test-only",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--no-venv" in result.stderr
    assert "禁止发布" in result.stderr


def test_structure_only_build_uses_real_source_and_has_no_release_manifest(tmp_path: Path) -> None:
    machine = platform.machine().lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    os_key = {
        "darwin": "darwin",
        "linux": "linux",
        "win32": "win",
    }[sys.platform]
    output = tmp_path / "build"

    subprocess.run(
        [
            "bash",
            str(_SCRIPTS / "package-vibe-trading.sh"),
            "--no-venv",
            f"--targets={architecture}",
            f"--out={output}",
        ],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    packages = list(output.glob(f"vibe-trading-{os_key}-{architecture}-*.zip"))
    assert len(packages) == 1
    assert not (output / "manifest.json").exists()
    with zipfile.ZipFile(packages[0]) as bundle:
        file_manifest_payload = bundle.read("file-manifest.json")
    metadata = verify_archive(packages[0], hashlib.sha256(file_manifest_payload).hexdigest())
    assert metadata["files"] > 1
