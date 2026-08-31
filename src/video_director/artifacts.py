"""Content-addressed artifact storage and optional ffmpeg assembly."""
from __future__ import annotations

import hashlib
import io
import mimetypes
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .schemas import ArtifactRef, AssetKind


class LocalArtifactStore:
    """Filesystem object store for local development and deterministic tests."""

    def __init__(self, root: str | Path = ".data/artifacts") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, *, kind: AssetKind = AssetKind.OTHER, mime_type: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        extension = mimetypes.guess_extension(mime_type or "") or ".bin"
        path = self.root / digest[:2] / f"{digest}{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return ArtifactRef(kind, str(path), sha256=digest, mime_type=mime_type, metadata=dict(metadata or {}))

    def put_file(self, source: str | Path, *, kind: AssetKind = AssetKind.OTHER, mime_type: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        path = Path(source)
        data = path.read_bytes()
        if mime_type is None:
            mime_type = mimetypes.guess_type(path.name)[0]
        artifact = self.put_bytes(data, kind=kind, mime_type=mime_type, metadata=metadata)
        artifact.metadata.setdefault("source_name", path.name)
        return artifact

    def get_bytes(self, artifact: ArtifactRef | str) -> bytes:
        """Read an artifact previously written by this store."""
        uri = artifact.uri if isinstance(artifact, ArtifactRef) else str(artifact)
        path = Path(uri)
        try:
            path = path.resolve()
            root = self.root.resolve()
            path.relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError("artifact does not belong to this local store") from error
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.read_bytes()


class S3ArtifactStore:
    """S3-compatible object store for MinIO and cloud deployments.

    The client is injected so tests and deployments can use boto3, MinIO's
    client, or a small replay double without coupling the domain to an SDK.
    """

    def __init__(self, bucket: str, *, client=None, endpoint_url: str | None = None, prefix: str = "artifacts") -> None:
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover - optional production dependency
                raise RuntimeError("S3 artifact storage requires boto3 or an injected client") from error
            client = boto3.client("s3", endpoint_url=endpoint_url)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, digest: str, extension: str) -> str:
        stem = f"{digest}{extension}"
        return f"{self.prefix}/{digest[:2]}/{stem}" if self.prefix else f"{digest[:2]}/{stem}"

    def put_bytes(self, data: bytes, *, kind: AssetKind = AssetKind.OTHER, mime_type: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        extension = mimetypes.guess_extension(mime_type or "") or ".bin"
        key = self._key(digest, extension)
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": io.BytesIO(data)}
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        self.client.put_object(**kwargs)
        return ArtifactRef(kind, f"s3://{self.bucket}/{key}", sha256=digest, mime_type=mime_type, metadata=dict(metadata or {}))

    def get_bytes(self, artifact: ArtifactRef | str) -> bytes:
        uri = artifact.uri if isinstance(artifact, ArtifactRef) else str(artifact)
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError("artifact does not belong to this S3 bucket")
        response = self.client.get_object(Bucket=self.bucket, Key=uri.removeprefix(prefix))
        body = response.get("Body")
        if hasattr(body, "read"):
            return body.read()
        if isinstance(body, bytes):
            return body
        raise ValueError("S3 object response did not contain a readable body")


class ArtifactStoreProtocol:
    """Structural marker for dependency injection and documentation."""

    def put_bytes(self, data: bytes, *, kind: AssetKind = AssetKind.OTHER, mime_type: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRef: ...

    def get_bytes(self, artifact: ArtifactRef | str) -> bytes: ...


class FFmpegAssembler:
    """Concatenate local video files and mux optional audio tracks."""

    name = "ffmpeg"

    def __init__(self, *, ffmpeg_bin: str = "ffmpeg", artifact_store: LocalArtifactStore | None = None) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.artifact_store = artifact_store or LocalArtifactStore()

    def assemble(self, video_artifacts: Sequence[ArtifactRef], audio_artifacts: Sequence[ArtifactRef] = (), *, output_uri: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        if not video_artifacts:
            raise ValueError("at least one video artifact is required")
        if any(not Path(item.uri).exists() for item in video_artifacts):
            raise ValueError("ffmpeg assembler requires local video artifact paths")
        output = Path(output_uri) if output_uri else self.artifact_store.root / "delivery.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        concat_file = output.with_suffix(".concat.txt")
        entries = []
        for item in video_artifacts:
            escaped = Path(item.uri).resolve().as_posix().replace("'", "'\"'\"'")
            entries.append(f"file '{escaped}'\n")
        concat_file.write_text("".join(entries), encoding="utf-8")
        command = [self.ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy"]
        local_audio = [item for item in audio_artifacts if Path(item.uri).exists() and item.kind == AssetKind.AUDIO]
        if local_audio:
            command.extend(["-i", local_audio[0].uri, "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:a", "aac"])
        command.append(str(output))
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        finally:
            concat_file.unlink(missing_ok=True)
        return self.artifact_store.put_file(output, kind=AssetKind.VIDEO, mime_type="video/mp4", metadata={"video_count": len(video_artifacts), "audio_count": len(local_audio), **dict(metadata or {})})
