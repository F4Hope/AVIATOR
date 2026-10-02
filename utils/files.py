"""Validate portable output names and atomically publish completed local files."""

import os
from pathlib import Path
import tempfile


def plain_filename(filename: str, suffixes: tuple[str, ...]) -> str:
    if (
        not isinstance(filename, str) or not filename or filename != filename.strip()
        or filename.startswith(".") or filename.endswith(".")
        or any(character in filename for character in '/\\:*?"<>|')
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or Path(filename).suffix.lower() not in suffixes
    ):
        raise ValueError("Output must be a plain filename with a supported extension.")
    filename.encode("utf-8")
    return filename


def publish_file(temporary: Path, target: Path, *, overwrite: bool = False) -> None:
    """Publish a closed, flushed file in the same directory in one operation.

    The hard-link operation fails atomically if any target already exists.
    Replacement is allowed only when explicitly requested by an export caller.
    The caller owns cleanup of the temporary file, including on failure.
    """
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean.")
    if temporary.parent.resolve() != target.parent.resolve():
        raise ValueError("Publication requires files in the same directory.")
    if overwrite:
        os.replace(temporary, target)
    else:
        os.link(temporary, target)


def write_bytes(directory: Path, filename: str, content: bytes, *, overwrite: bool = False) -> Path:
    """Write validated content privately, flush it, then publish without partial files."""
    if not isinstance(filename, str):
        raise ValueError("Output must be a plain filename.")
    filename = plain_filename(filename, (Path(filename).suffix.lower(),))
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not overwrite and (target.exists() or target.is_symlink()):
        raise FileExistsError("The output already exists.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".aie-export-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        publish_file(temporary, target, overwrite=overwrite)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
