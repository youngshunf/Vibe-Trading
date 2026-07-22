#!/usr/bin/env python3
"""生成并校验 Vibe-Trading 受信发布清单与逐文件清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_ARTIFACT_ID = "app.engine.finance"
_FILE_MANIFEST_NAME = "file-manifest.json"
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "artifact_id",
    "version",
    "release_sequence",
    "channel",
    "issued_at",
    "expires_at",
    "minimum_daemon_version",
    "packages",
    "revocations",
    "key_id",
    "signature",
}
_PACKAGE_FIELDS = {
    "url",
    "sha256",
    "compressed_size",
    "installed_size_limit",
    "file_manifest_sha256",
}
_REVOCATION_FIELDS = {
    "version",
    "platform",
    "sha256",
    "revoked_at",
    "reason",
    "critical",
}


class ManifestError(ValueError):
    """发布清单或归档违反安全契约。"""


# 单个路径成分的最大字节数（与 daemon hasn-local-runtime-artifact::archive 口径一致）。
_MAX_PATH_COMPONENT_BYTES = 128


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    details: list[str] = []
    if unknown:
        details.append(f"未知字段={unknown}")
    if missing:
        details.append(f"缺少字段={missing}")
    raise ManifestError(f"{context} 字段不合法：{'; '.join(details)}")


def _safe_relative_path(raw: str) -> str:
    """与 daemon hasn-local-runtime-artifact::archive 的口径逐条对齐。

    目标「打得出 = 装得上」：打包期就拒绝 daemon 会拒的路径（fail fast），
    而不是发布出去后被整包 unsafe_archive 拒装。daemon 口径：成分 ≤128 字节、
    无 C0/C1 控制字符（Unicode category Cc）、无路径分隔符与冒号、非 . / ..。
    """
    if not raw or "\\" in raw:
        raise ManifestError(f"不安全相对路径：{raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError(f"不安全相对路径：{raw!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise ManifestError(f"非规范相对路径：{raw!r}")
    for part in path.parts:
        if len(part.encode("utf-8")) > _MAX_PATH_COMPONENT_BYTES:
            raise ManifestError(f"路径成分超过 {_MAX_PATH_COMPONENT_BYTES} 字节：{part!r}")
        for ch in part:
            if unicodedata.category(ch) == "Cc" or ch == ":":
                raise ManifestError(f"路径成分含控制字符或冒号：{part!r}")
    return normalized


def _iter_payload_files(root: Path) -> list[Path]:
    root_metadata = root.lstat()
    if stat.S_ISLNK(root_metadata.st_mode):
        raise ManifestError("staging 根不得是符号链接")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ManifestError("staging 根必须是目录")

    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            path = directory_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ManifestError(f"包内禁止符号链接：{path.relative_to(root)}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ManifestError(f"包内禁止特殊文件：{path.relative_to(root)}")
        for name in file_names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if relative == _FILE_MANIFEST_NAME:
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ManifestError(f"包内禁止符号链接：{relative}")
            if not stat.S_ISREG(metadata.st_mode):
                raise ManifestError(f"包内禁止特殊文件：{relative}")
            if metadata.st_nlink != 1:
                raise ManifestError(f"包内禁止硬链接：{relative}")
            _safe_relative_path(relative)
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def build_file_manifest(root: Path) -> dict[str, Any]:
    """扫描 staging，拒绝危险文件并写入确定性的逐文件清单。"""
    root = Path(os.path.abspath(root))
    output = root / _FILE_MANIFEST_NAME
    if output.exists() or output.is_symlink():
        output.unlink()

    entries: list[dict[str, Any]] = []
    payload_size = 0
    for path in _iter_payload_files(root):
        metadata = path.stat()
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "size": metadata.st_size,
                "sha256": _sha256_file(path),
                "executable": bool(metadata.st_mode & 0o111),
            }
        )
        payload_size += metadata.st_size

    manifest = {"schema_version": 1, "files": entries}
    encoded = _canonical_json(manifest) + b"\n"
    output.write_bytes(encoded)
    return {
        "file_manifest_sha256": _sha256_bytes(encoded),
        "installed_size": payload_size + len(encoded),
        "payload_files": len(entries),
    }


def build_archive(root: Path, archive: Path) -> None:
    """用确定性 UTF-8 ZIP 打包，避免平台 zip 工具改写非 ASCII 路径。"""
    root = Path(os.path.abspath(root))
    file_manifest = root / _FILE_MANIFEST_NAME
    if not file_manifest.is_file() or file_manifest.is_symlink():
        raise ManifestError("打包前必须先生成普通文件 file-manifest.json")
    files = [*_iter_payload_files(root), file_manifest]
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as output:
            for path in files:
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | (metadata.st_mode & 0o777)) << 16
                with path.open("rb") as source, output.open(info, mode="w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        os.replace(temporary, archive)
    except (OSError, zipfile.BadZipFile) as error:
        temporary.unlink(missing_ok=True)
        raise ManifestError(f"引擎归档写入失败：{error}") from error


def _zip_entry_type(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    if info.is_dir():
        return "directory"
    if mode and stat.S_ISLNK(mode):
        return "symlink"
    file_type = stat.S_IFMT(mode)
    if file_type in (0, stat.S_IFREG):
        return "regular"
    return "special"


def _parse_file_manifest(payload: bytes) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("逐文件清单不是合法 UTF-8 JSON") from error
    if not isinstance(manifest, dict):
        raise ManifestError("逐文件清单根必须是对象")
    _require_exact_fields(manifest, {"schema_version", "files"}, "逐文件清单")
    if manifest["schema_version"] != 1:
        raise ManifestError("逐文件清单 schema_version 必须为 1")
    if not isinstance(manifest["files"], list):
        raise ManifestError("逐文件清单 files 必须是数组")

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(manifest["files"]):
        if not isinstance(raw, dict):
            raise ManifestError(f"逐文件清单 files[{index}] 必须是对象")
        _require_exact_fields(raw, {"path", "size", "sha256", "executable"}, f"files[{index}]")
        path = _safe_relative_path(raw["path"]) if isinstance(raw["path"], str) else ""
        if not path:
            raise ManifestError(f"files[{index}].path 必须是字符串")
        if path == _FILE_MANIFEST_NAME:
            raise ManifestError("逐文件清单不得递归声明自身")
        if path in seen:
            raise ManifestError(f"逐文件清单重复路径：{path}")
        seen.add(path)
        if not isinstance(raw["size"], int) or isinstance(raw["size"], bool) or raw["size"] < 0:
            raise ManifestError(f"files[{index}].size 不合法")
        _validate_sha256(raw["sha256"], f"files[{index}].sha256")
        if not isinstance(raw["executable"], bool):
            raise ManifestError(f"files[{index}].executable 必须是布尔值")
        entries.append(raw)
    return entries


def verify_archive(archive: Path, expected_file_manifest_sha256: str) -> dict[str, int]:
    """按逐文件清单核验 zip，拒绝未声明、缺失、链接、特殊文件和内容漂移。"""
    _validate_sha256(expected_file_manifest_sha256, "file_manifest_sha256")
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        raise ManifestError("引擎归档不是合法 zip") from error

    with bundle:
        normalized: dict[str, zipfile.ZipInfo] = {}
        for info in bundle.infolist():
            path = _safe_relative_path(info.filename.rstrip("/"))
            if path in normalized:
                raise ManifestError(f"归档含重复规范化路径：{path}")
            normalized[path] = info
            entry_type = _zip_entry_type(info)
            if entry_type == "symlink":
                raise ManifestError(f"归档含符号链接：{path}")
            if entry_type == "special":
                raise ManifestError(f"归档含特殊文件：{path}")

        file_manifest_info = normalized.get(_FILE_MANIFEST_NAME)
        if file_manifest_info is None or _zip_entry_type(file_manifest_info) != "regular":
            raise ManifestError("归档缺少普通文件 file-manifest.json")
        file_manifest_payload = bundle.read(file_manifest_info)
        if _sha256_bytes(file_manifest_payload) != expected_file_manifest_sha256:
            raise ManifestError("逐文件清单摘要不匹配")
        declared_entries = _parse_file_manifest(file_manifest_payload)
        declared = {item["path"]: item for item in declared_entries}
        actual = {
            path: info
            for path, info in normalized.items()
            if _zip_entry_type(info) == "regular" and path != _FILE_MANIFEST_NAME
        }
        undeclared = sorted(set(actual) - set(declared))
        if undeclared:
            raise ManifestError(f"归档含未声明文件：{undeclared}")
        missing = sorted(set(declared) - set(actual))
        if missing:
            raise ManifestError(f"归档缺少声明文件：{missing}")

        for path, expected in declared.items():
            info = actual[path]
            if info.file_size != expected["size"]:
                raise ManifestError(f"文件大小不匹配：{path}")
            payload = bundle.read(info)
            if _sha256_bytes(payload) != expected["sha256"]:
                raise ManifestError(f"文件摘要不匹配：{path}")
            mode = info.external_attr >> 16
            executable = bool(mode & 0o111)
            if executable != expected["executable"]:
                raise ManifestError(f"文件执行权限不匹配：{path}")

        installed_size = sum(info.file_size for info in normalized.values() if _zip_entry_type(info) == "regular")
        return {"installed_size": installed_size, "files": len(actual) + 1}


def canonical_manifest_payload(manifest: Mapping[str, Any]) -> bytes:
    """返回 Ed25519 覆盖的规范 JSON；只排除 signature 本身。"""
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    return _canonical_json(unsigned)


def _validate_sha256(value: Any, context: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ManifestError(f"{context} 必须是小写 SHA-256")


def _parse_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ManifestError(f"{context} 必须是 UTC RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ManifestError(f"{context} 不是合法时间") from error
    if parsed.tzinfo != UTC:
        raise ManifestError(f"{context} 必须使用 UTC")
    return parsed


def _validate_url(value: Any) -> None:
    if not isinstance(value, str):
        raise ManifestError("package.url 必须是字符串")
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if not loopback:
        raise ManifestError("package.url 只允许 https 或 loopback http")


def _validate_manifest_shape(manifest: Mapping[str, Any], *, signed: bool) -> None:
    expected_fields = _TOP_LEVEL_FIELDS if signed else _TOP_LEVEL_FIELDS - {"signature"}
    _require_exact_fields(manifest, expected_fields, "发布清单")
    if manifest["schema_version"] != 2:
        raise ManifestError("发布清单 schema_version 必须为 2")
    if manifest["artifact_id"] != _ARTIFACT_ID:
        raise ManifestError(f"artifact_id 必须为 {_ARTIFACT_ID}")
    if signed:
        signature = manifest["signature"]
        if (
            not isinstance(signature, str)
            or len(signature) != 128
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            raise ManifestError("signature 必须是 128 位小写十六进制")
    for name in ("version", "channel", "minimum_daemon_version", "key_id"):
        if not isinstance(manifest[name], str) or not manifest[name].strip():
            raise ManifestError(f"{name} 必须是非空字符串")
    sequence = manifest["release_sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise ManifestError("release_sequence 必须是正整数")
    issued_at = _parse_timestamp(manifest["issued_at"], "issued_at")
    expires_at = _parse_timestamp(manifest["expires_at"], "expires_at")
    if expires_at <= issued_at:
        raise ManifestError("expires_at 必须晚于 issued_at")

    packages = manifest["packages"]
    if not isinstance(packages, dict) or not packages:
        raise ManifestError("packages 必须是非空对象")
    for platform, package in packages.items():
        _safe_relative_path(platform)
        if not isinstance(package, dict):
            raise ManifestError(f"packages.{platform} 必须是对象")
        _require_exact_fields(package, _PACKAGE_FIELDS, f"packages.{platform}")
        _validate_url(package["url"])
        _validate_sha256(package["sha256"], f"packages.{platform}.sha256")
        _validate_sha256(
            package["file_manifest_sha256"],
            f"packages.{platform}.file_manifest_sha256",
        )
        for field in ("compressed_size", "installed_size_limit"):
            value = package[field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ManifestError(f"packages.{platform}.{field} 必须是正整数")

    revocations = manifest["revocations"]
    if not isinstance(revocations, list):
        raise ManifestError("revocations 必须是数组")
    for index, revocation in enumerate(revocations):
        if not isinstance(revocation, dict):
            raise ManifestError(f"revocations[{index}] 必须是对象")
        _require_exact_fields(revocation, _REVOCATION_FIELDS, f"revocations[{index}]")
        for name in ("version", "platform", "reason"):
            if not isinstance(revocation[name], str) or not revocation[name].strip():
                raise ManifestError(f"revocations[{index}].{name} 必须是非空字符串")
        _validate_sha256(revocation["sha256"], f"revocations[{index}].sha256")
        _parse_timestamp(revocation["revoked_at"], f"revocations[{index}].revoked_at")
        if not isinstance(revocation["critical"], bool):
            raise ManifestError(f"revocations[{index}].critical 必须是布尔值")


def sign_release_manifest(
    unsigned_manifest: Mapping[str, Any],
    private_key_pem: bytes,
) -> dict[str, Any]:
    """用真实 Ed25519 私钥签名全部安全相关字段。"""
    _validate_manifest_shape(unsigned_manifest, signed=False)
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, ValueError) as error:
        raise ManifestError("发布签名私钥不是合法 PEM") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestError("发布签名私钥必须是 Ed25519")
    signed = dict(unsigned_manifest)
    signed["signature"] = key.sign(canonical_manifest_payload(unsigned_manifest)).hex()
    return signed


def verify_release_manifest(
    manifest: Mapping[str, Any],
    trusted_public_keys: Mapping[str, bytes],
    *,
    platform: str,
    now: datetime,
    accepted_release_sequence: int | None = None,
) -> dict[str, Any]:
    """验签并执行平台选择、有效期、防重放与撤销闸门。"""
    _validate_manifest_shape(manifest, signed=True)
    key_id = manifest["key_id"]
    public_key_pem = trusted_public_keys.get(key_id)
    if public_key_pem is None:
        raise ManifestError(f"未知签名密钥：{key_id}")
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as error:
        raise ManifestError("受信公钥不是合法 PEM") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ManifestError("受信公钥必须是 Ed25519")
    try:
        signature = bytes.fromhex(manifest["signature"])
        key.verify(signature, canonical_manifest_payload(manifest))
    except (ValueError, InvalidSignature) as error:
        raise ManifestError("发布清单签名验证失败") from error

    if now.tzinfo != UTC:
        raise ManifestError("校验时钟必须是 UTC")
    if now < _parse_timestamp(manifest["issued_at"], "issued_at"):
        raise ManifestError("发布清单尚未生效")
    if now >= _parse_timestamp(manifest["expires_at"], "expires_at"):
        raise ManifestError("发布清单已过期")
    if accepted_release_sequence is not None and manifest["release_sequence"] <= accepted_release_sequence:
        raise ManifestError("发布序列未单调递增")
    package = manifest["packages"].get(platform)
    if package is None:
        raise ManifestError(f"发布清单不支持当前平台：{platform}")
    for revocation in manifest["revocations"]:
        if (
            revocation["version"] == manifest["version"]
            and revocation["platform"] == platform
            and revocation["sha256"] == package["sha256"]
        ):
            raise ManifestError(f"当前 Revision 已撤销：{revocation['reason']}")
    return dict(package)


def verify_publish_response(
    manifest: Mapping[str, Any],
    response_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """确认云端业务成功且回写的签名清单与本地产物完全一致。"""
    _validate_manifest_shape(manifest, signed=True)
    if response_envelope.get("code") not in (0, 200):
        raise ManifestError(
            "云端业务失败 "
            f"code={response_envelope.get('code')} msg={response_envelope.get('msg')}"
        )
    published = response_envelope.get("data")
    if published != manifest:
        raise ManifestError("云端回写清单与本地签名清单不一致")
    return dict(manifest)


def upsert_release_package(
    manifest_path: Path,
    *,
    version: str,
    release_sequence: int,
    channel: str,
    issued_at: str,
    expires_at: str,
    minimum_daemon_version: str,
    key_id: str,
    signing_key_path: Path,
    platform: str,
    url: str,
    sha256: str,
    compressed_size: int,
    installed_size_limit: int,
    file_manifest_sha256: str,
    revocations: list[dict[str, Any]],
) -> dict[str, Any]:
    """幂等累积平台包，并在每次写入后覆盖签名。"""
    try:
        private_key_payload = signing_key_path.read_bytes()
    except OSError as error:
        raise ManifestError("无法读取发布签名私钥") from error
    try:
        private_key = serialization.load_pem_private_key(private_key_payload, password=None)
    except (TypeError, ValueError) as error:
        raise ManifestError("发布签名私钥不是合法 PEM") from error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ManifestError("发布签名私钥必须是 Ed25519")

    common = {
        "schema_version": 2,
        "artifact_id": _ARTIFACT_ID,
        "version": version,
        "release_sequence": release_sequence,
        "channel": channel,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "minimum_daemon_version": minimum_daemon_version,
        "revocations": revocations,
        "key_id": key_id,
    }
    unsigned: dict[str, Any] = {**common, "packages": {}}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestError("既有发布清单无法读取") from error
        if not isinstance(existing, dict):
            raise ManifestError("既有发布清单根必须是对象")
        _validate_manifest_shape(existing, signed=True)
        try:
            signature = bytes.fromhex(existing["signature"])
            private_key.public_key().verify(signature, canonical_manifest_payload(existing))
        except (ValueError, InvalidSignature) as error:
            raise ManifestError("既有发布清单签名与当前密钥不一致") from error
        for field, expected in common.items():
            if existing.get(field) != expected:
                raise ManifestError(f"多平台发布清单公共字段冲突：{field}={existing.get(field)!r}，本次={expected!r}")
        unsigned["packages"] = dict(existing["packages"])

    unsigned["packages"][platform] = {
        "url": url,
        "sha256": sha256,
        "compressed_size": compressed_size,
        "installed_size_limit": installed_size_limit,
        "file_manifest_sha256": file_manifest_sha256,
    }
    signed = sign_release_manifest(unsigned, private_key_payload)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(signed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, manifest_path)
    except OSError as error:
        raise ManifestError("发布清单原子写入失败") from error
    return signed


def _build_file_manifest_command(arguments: argparse.Namespace) -> None:
    metadata = build_file_manifest(Path(arguments.root))
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))


def _verify_archive_command(arguments: argparse.Namespace) -> None:
    metadata = verify_archive(
        Path(arguments.archive),
        arguments.file_manifest_sha256,
    )
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))


def _build_archive_command(arguments: argparse.Namespace) -> None:
    build_archive(Path(arguments.root), Path(arguments.archive))
    print(json.dumps({"archive": arguments.archive}, ensure_ascii=False, sort_keys=True))


def _load_json_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"{context}无法读取") from error
    if not isinstance(value, dict):
        raise ManifestError(f"{context}根必须是对象")
    return value


def _verify_publish_response_command(arguments: argparse.Namespace) -> None:
    manifest = _load_json_object(Path(arguments.manifest), "本地发布清单")
    response_envelope = _load_json_object(Path(arguments.response), "云端发布响应")
    published = verify_publish_response(manifest, response_envelope)
    print(
        json.dumps(
            {
                "artifact_id": published["artifact_id"],
                "release_sequence": published["release_sequence"],
                "version": published["version"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _load_revocations(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("撤销清单无法读取") from error
    if not isinstance(value, list):
        raise ManifestError("撤销清单根必须是数组")
    return value


def _upsert_package_command(arguments: argparse.Namespace) -> None:
    manifest = upsert_release_package(
        Path(arguments.manifest),
        version=arguments.version,
        release_sequence=arguments.release_sequence,
        channel=arguments.channel,
        issued_at=arguments.issued_at,
        expires_at=arguments.expires_at,
        minimum_daemon_version=arguments.minimum_daemon_version,
        key_id=arguments.key_id,
        signing_key_path=Path(arguments.signing_key),
        platform=arguments.platform,
        url=arguments.url,
        sha256=arguments.sha256,
        compressed_size=arguments.compressed_size,
        installed_size_limit=arguments.installed_size_limit,
        file_manifest_sha256=arguments.file_manifest_sha256,
        revocations=_load_revocations(arguments.revocations_file),
    )
    print(
        json.dumps(
            {
                "manifest": arguments.manifest,
                "platforms": sorted(manifest["packages"]),
                "release_sequence": manifest["release_sequence"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    file_manifest = subparsers.add_parser("build-file-manifest")
    file_manifest.add_argument("--root", required=True)
    file_manifest.set_defaults(handler=_build_file_manifest_command)

    verify = subparsers.add_parser("verify-archive")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--file-manifest-sha256", required=True)
    verify.set_defaults(handler=_verify_archive_command)

    archive = subparsers.add_parser("build-archive")
    archive.add_argument("--root", required=True)
    archive.add_argument("--archive", required=True)
    archive.set_defaults(handler=_build_archive_command)

    publish_response = subparsers.add_parser("verify-publish-response")
    publish_response.add_argument("--manifest", required=True)
    publish_response.add_argument("--response", required=True)
    publish_response.set_defaults(handler=_verify_publish_response_command)

    upsert = subparsers.add_parser("upsert-package")
    upsert.add_argument("--manifest", required=True)
    upsert.add_argument("--version", required=True)
    upsert.add_argument("--release-sequence", required=True, type=int)
    upsert.add_argument("--channel", required=True)
    upsert.add_argument("--issued-at", required=True)
    upsert.add_argument("--expires-at", required=True)
    upsert.add_argument("--minimum-daemon-version", required=True)
    upsert.add_argument("--key-id", required=True)
    upsert.add_argument("--signing-key", required=True)
    upsert.add_argument("--platform", required=True)
    upsert.add_argument("--url", required=True)
    upsert.add_argument("--sha256", required=True)
    upsert.add_argument("--compressed-size", required=True, type=int)
    upsert.add_argument("--installed-size-limit", required=True, type=int)
    upsert.add_argument("--file-manifest-sha256", required=True)
    upsert.add_argument("--revocations-file")
    upsert.set_defaults(handler=_upsert_package_command)
    return parser


def main() -> int:
    """命令行入口。"""
    arguments = _parser().parse_args()
    try:
        arguments.handler(arguments)
    except ManifestError as error:
        print(f"[vt-manifest] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
