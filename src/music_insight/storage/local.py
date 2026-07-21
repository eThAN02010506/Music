from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from music_insight.schemas import AudioAsset


class UploadTooLargeError(ValueError):
    pass


class LocalAudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def save_upload(
        self,
        upload: UploadFile,
        max_bytes: int | None = None,
    ) -> AudioAsset:
        suffix = Path(upload.filename or "audio.bin").suffix or ".bin"
        path = self.root / f"{uuid4().hex}{suffix}"

        size = 0
        try:
            with path.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise UploadTooLargeError(
                            f"Upload exceeds {max_bytes} bytes."
                        )
                    target.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise

        return AudioAsset(
            path=path,
            media_type=upload.content_type or "application/octet-stream",
            size_bytes=size,
        )
