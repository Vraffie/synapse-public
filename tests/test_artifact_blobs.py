from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from synapse.infrastructure.artifact_blobs import ArtifactBlobStore, ArtifactIntegrityError


def test_store_read_deduplicate_and_bound_size(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=5)
    ref = store.put([b"he", b"llo"])
    assert ref.sha256 == sha256(b"hello").hexdigest()
    assert ref.byte_size == 5
    assert store.read(ref.sha256) == b"hello"
    assert store.put([b"hello"]) == ref
    with pytest.raises(ValueError, match="maximum"):
        store.put([b"hello", b"!"])
    assert {path.name for path in tmp_path.iterdir()} == {ref.sha256}


def test_concurrent_publication_produces_one_valid_blob(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: store.put([b"same", b" content"]), range(8)))
    assert all(ref == refs[0] for ref in refs)
    assert store.read(refs[0].sha256) == b"same content"
    assert len(list(tmp_path.iterdir())) == 1


def test_rejects_tampering_symlink_and_traversal(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    ref = store.put([b"original"])
    (tmp_path / ref.sha256).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError):
        store.read(ref.sha256)
    repaired = store.put([b"original"])
    assert repaired.repaired
    assert store.read(ref.sha256) == b"original"
    quarantined = list(tmp_path.glob(".corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"changed"
    with pytest.raises(ValueError):
        store.read("../secret")
    name = sha256(b"other").hexdigest()
    (tmp_path / name).symlink_to(tmp_path / ref.sha256)
    with pytest.raises(OSError):
        store.read(name)


def test_generator_failure_removes_temporary_blob(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=20)

    def broken():
        yield b"partial"
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        store.put(broken())
    assert list(tmp_path.iterdir()) == []


def test_rejects_public_directory_and_invalid_chunks(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    with pytest.raises(PermissionError):
        ArtifactBlobStore(public, max_bytes=20).put([b"data"])
    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    with pytest.raises(TypeError):
        store.put(["not bytes"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["public"]


def test_read_rejects_fifo_without_blocking(tmp_path):
    import os

    name = sha256(b"fifo").hexdigest()
    os.mkfifo(tmp_path / name, mode=0o600)
    with pytest.raises(PermissionError, match="regular"):
        ArtifactBlobStore(tmp_path, max_bytes=20).read(name)


def test_empty_blob_and_oversized_stored_blob(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=0)
    ref = store.put([])
    assert store.read(ref.sha256) == b""
    (tmp_path / ref.sha256).write_bytes(b"x")
    with pytest.raises(ArtifactIntegrityError, match="maximum"):
        store.read(ref.sha256)


def test_concurrent_repair_preserves_corrupt_bytes(tmp_path):
    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    original = store.put([b"correct"])
    (tmp_path / original.sha256).write_bytes(b"broken")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.put([b"correct"]), range(8)))
    assert any(result.repaired for result in results)
    assert all(result.sha256 == original.sha256 for result in results)
    assert store.read(original.sha256) == b"correct"
    assert any(path.read_bytes() == b"broken" for path in tmp_path.glob(".corrupt-*"))
    assert not list(tmp_path.glob(".tmp-*"))


def test_failure_to_preserve_corrupt_blob_does_not_replace_it(tmp_path, monkeypatch):
    import os

    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    ref = store.put([b"correct"])
    (tmp_path / ref.sha256).write_bytes(b"broken")
    real_link = os.link

    def fail_quarantine(src, dst, **kwargs):
        if dst.startswith(".corrupt-"):
            raise OSError("quarantine unavailable")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", fail_quarantine)
    with pytest.raises(OSError, match="quarantine unavailable"):
        store.put([b"correct"])
    assert (tmp_path / ref.sha256).read_bytes() == b"broken"
    assert not list(tmp_path.glob(".tmp-*"))


def test_concurrent_repair_rechecks_after_quarantine_link_race(tmp_path, monkeypatch):
    import os

    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    ref = store.put([b"correct"])
    (tmp_path / ref.sha256).write_bytes(b"broken")
    real_link = os.link
    raced = False

    def race_quarantine(src, dst, **kwargs):
        nonlocal raced
        if dst.startswith(".corrupt-") and not raced:
            raced = True
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"correct")
            replacement.chmod(0o600)
            os.replace(replacement, tmp_path / ref.sha256)
            raise FileNotFoundError("concurrent replacement")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", race_quarantine)
    assert store.put([b"correct"]) == ref
    assert raced and store.read(ref.sha256) == b"correct"
    assert not list(tmp_path.glob(".tmp-*"))


def test_repeated_quarantine_link_races_fail_without_data_loss(tmp_path, monkeypatch):
    import os

    store = ArtifactBlobStore(tmp_path, max_bytes=20)
    ref = store.put([b"correct"])
    (tmp_path / ref.sha256).write_bytes(b"broken")
    real_link = os.link

    def race_quarantine(src, dst, **kwargs):
        if dst.startswith(".corrupt-"):
            raise FileNotFoundError("concurrent replacement")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", race_quarantine)
    with pytest.raises(ArtifactIntegrityError, match="Could not verify or repair"):
        store.put([b"correct"])
    assert (tmp_path / ref.sha256).read_bytes() == b"broken"
    assert not list(tmp_path.glob(".tmp-*"))
