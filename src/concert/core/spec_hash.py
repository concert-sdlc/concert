"""The spec hash (`spec_revision`): a content hash of everything under docs/specs/<issue>/.

A git tree hash of the directory would do on real git; this hashes the same set of files
(path and content) so the hash is computable from a file listing alone, in the real client
and in the fake, and is independent of the order the files are listed in.
"""

import hashlib
from collections.abc import Mapping


def spec_dir(issue: int) -> str:
    return f"docs/specs/{issue}/"


def spec_tree_hash(tree: Mapping[str, str], issue: int) -> str:
    prefix = spec_dir(issue)
    digest = hashlib.sha1()  # an identifier, not a security boundary
    for path in sorted(p for p in tree if p.startswith(prefix)):
        content = tree[path].encode("utf-8")
        digest.update(f"{len(path)}:{path}\0{len(content)}:".encode())
        digest.update(content)
    return digest.hexdigest()
