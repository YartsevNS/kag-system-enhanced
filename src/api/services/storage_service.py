"""
Storage service for original document files.

Handles:
- Storing uploaded original files with content-based addressing
- SHA-256 hashing for integrity verification
- Retrieval by stored path
"""

import hashlib
from pathlib import Path
from typing import Optional

from loguru import logger


class StorageService:
    """
    Stores original document files on disk with hash-based naming.

    Directory layout:
        {base_path}/
            {doc_id[:2]}/
                {doc_id}_{filename}

    The first two characters of the document UUID are used as a subdirectory
    to avoid too many files in a single directory.
    """

    def __init__(self, base_path: Optional[str] = None):
        if base_path is None:
            from pathlib import Path as P
            base_path = str(P("/app/user_data/originals"))

        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"StorageService инициализирован: {self.base_path}")

    def store_original(
        self, file_content: bytes, filename: str, doc_id: str
    ) -> tuple[str, str]:
        """
        Store original file content and return (path, sha256_hash).

        Args:
            file_content: Raw bytes of the file
            filename: Original filename (used in stored path)
            doc_id: Document UUID (first 2 chars used as subdirectory)

        Returns:
            Tuple of (absolute_path_on_disk, sha256_hex_hash)
        """
        sha256 = hashlib.sha256(file_content).hexdigest()

        subdir = self.base_path / doc_id[:2]
        subdir.mkdir(exist_ok=True)

        dest = subdir / f"{doc_id}_{filename}"
        dest.write_bytes(file_content)

        logger.debug(
            f"Файл сохранен: {dest} (sha256={sha256[:16]}..., size={len(file_content)})"
        )
        return str(dest), sha256

    def get_original(self, path: str) -> bytes:
        """
        Read original file content from disk.

        Args:
            path: Absolute path to the stored file.

        Returns:
            Raw file bytes.

        Raises:
            FileNotFoundError: If the path does not exist.
        """
        return Path(path).read_bytes()

    def delete_original(self, path: str) -> bool:
        """
        Delete original file from disk.

        Args:
            path: Absolute path to the stored file.

        Returns:
            True if deleted, False if file did not exist.
        """
        p = Path(path)
        if p.exists():
            p.unlink()
            logger.debug(f"Файл удален: {path}")
            return True
        logger.debug(f"Файл не найден для удаления: {path}")
        return False
