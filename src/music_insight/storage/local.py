from pathlib import Path
import os
import re
from uuid import uuid4

from fastapi import UploadFile

from music_insight.schemas import AudioAsset


class UploadTooLargeError(ValueError):
    pass


class LocalAudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    async def save_upload(
        self,
        upload: UploadFile,
        max_bytes: int | None = None,
    ) -> AudioAsset:
        suffix = Path(upload.filename or "audio.bin").suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,9}", suffix):
            suffix = ".audio"
        path = self.root / f"{uuid4().hex}{suffix}"

        size = 0
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise UploadTooLargeError(
                            f"Upload exceeds {max_bytes} bytes."
                        )
                    target.write(chunk)
        # ``asyncio.CancelledError`` is a ``BaseException`` on supported
        # Python versions. A disconnected upload must not leave a partial file.
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        return AudioAsset(
            path=path,
            media_type=upload.content_type or "application/octet-stream",
            size_bytes=size,
        )
