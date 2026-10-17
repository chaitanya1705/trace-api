"""Safe extraction of a repository archive. The archive is untrusted input: nothing in it is executed, and anything that
could write outside the target folder or consume unbounded space is refused.
"""
from __future__ import annotations

import os
import tarfile

MAX_ARCHIVE_BYTES = 150 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 40000


class ArchiveError(ValueError):
    """Refusal with a reason; messages never include file content."""


def extract_tarball(archive_path: str, dest: str) -> int:
    """Extract a GitHub-style .tar.gz into `dest`, dropping the single leading folder. Returns the number of files written.

    Refused: absolute paths, `..` segments, links, devices, oversized or over-numerous archives. Only regular files and
    directories are materialised; permission bits are ignored (files are written 0644).
    """
    if os.path.getsize(archive_path) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("archive_too_large")
    root_real = os.path.realpath(dest)
    written = 0
    total = 0
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            for i, m in enumerate(tf):
                if i >= MAX_MEMBERS:
                    raise ArchiveError("too_many_files")
                if m.name.startswith("pax_global_header") or m.isdir():
                    continue
                if m.issym() or m.islnk() or not m.isfile():
                    continue  # links and special files are skipped, never followed or created
                name = m.name.replace("\\", "/")
                if name.startswith("/") or any(seg == ".." for seg in name.split("/")):
                    raise ArchiveError("unsafe_path")
                parts = name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1]
                target = os.path.realpath(os.path.join(dest, rel))
                if not target.startswith(root_real + os.sep):
                    raise ArchiveError("unsafe_path")
                total += m.size
                if total > MAX_UNPACKED_BYTES:
                    raise ArchiveError("unpacked_too_large")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    while chunk := src.read(1 << 16):
                        out.write(chunk)
                written += 1
    except tarfile.TarError:
        raise ArchiveError("not_a_valid_archive") from None
    return written
