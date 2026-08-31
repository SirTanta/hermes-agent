"""Behavior contracts for conservative built-in durable-memory hygiene."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli.memory_hygiene import (
    HygieneError,
    apply_hygiene,
    build_hygiene_report,
    rollback_hygiene,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)
DELIM = "\n§\n"


def _write(memories: Path, name: str, entries: list[str]) -> Path:
    memories.mkdir(parents=True, exist_ok=True)
    path = memories / name
    path.write_text(DELIM.join(entries), encoding="utf-8")
    return path


def test_dry_run_identifies_safe_classes_without_writing_protected_entries(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    protected = [
        "User prefers concise replies.",
        "Credentials require explicit human approval.",
        "Always run the release verification before deployment.",
        "Current production gateway is active.",
        "Jon: Asia/Singapore.",
        "Jon:  Asia/Singapore.",
        "Project uses pytest with xdist.",
        "project  uses pytest with xdist.",
        "Concise responses are preferred.",
        "Concise  responses are preferred.",
    ]
    entries = protected + [
        "Disposable build cache lives under /tmp/build.",
        "  disposable   build cache lives under /tmp/build.  ",
        "[superseded] Old checkout path was /srv/old.",
        "2025-01-01: temporary migration note for completed task.",
        "It is important to remember that local fixtures are synthetic. Local fixtures are synthetic.",
    ]
    path = _write(memories, "MEMORY.md", entries)
    before = path.read_bytes()

    report = build_hygiene_report(
        memories,
        memory_char_limit=500,
        user_char_limit=300,
        target_percent=70,
        stale_days=90,
        now=NOW,
    )

    assert report["mode"] == "dry-run"
    assert path.read_bytes() == before
    classes = {candidate["classification"] for candidate in report["stores"]["memory"]["candidates"]}
    assert {"duplicate", "superseded", "stale", "verbose"} <= classes
    protected_text = {item["entry"] for item in report["stores"]["memory"]["protected"]}
    assert set(protected) <= protected_text
    changed_originals = {action["before"] for action in report["stores"]["memory"]["plan"]}
    assert not changed_originals.intersection(protected)


def test_user_store_is_reported_but_never_auto_compacted(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    entries = ["User prefers dark mode.", " user   prefers dark mode. "]
    _write(memories, "USER.md", entries)

    report = build_hygiene_report(
        memories,
        memory_char_limit=500,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
    )

    user = report["stores"]["user"]
    assert user["plan"] == []
    assert len(user["protected"]) == 2
    assert {item["reason"] for item in user["protected"]} == {"user-profile entries require manual review"}


@pytest.mark.parametrize("target", [0, 75, 100])
def test_target_must_be_below_75_percent(tmp_path: Path, target: int) -> None:
    with pytest.raises(ValueError, match="1 through 74"):
        build_hygiene_report(
            tmp_path,
            memory_char_limit=100,
            user_char_limit=100,
            target_percent=target,
            now=NOW,
        )


def test_apply_requires_explicit_boundary_and_emits_backup_and_audit_receipt(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    original = [
        "Disposable cache alpha.",
        " disposable   cache alpha. ",
        "[superseded] Old disposable cache beta.",
        "2025-01-01: temporary completed migration note.",
    ]
    path = _write(memories, "MEMORY.md", original)

    dry = apply_hygiene(
        memories,
        memory_char_limit=100,
        user_char_limit=100,
        target_percent=70,
        stale_days=90,
        now=NOW,
        apply=False,
    )
    assert dry["mode"] == "dry-run"
    assert path.read_text(encoding="utf-8") == DELIM.join(original)

    receipt = apply_hygiene(
        memories,
        memory_char_limit=100,
        user_char_limit=100,
        target_percent=70,
        stale_days=90,
        now=NOW,
        apply=True,
    )

    assert receipt["status"] == "applied"
    assert receipt["target_percent"] == 70
    assert receipt["stores"]["memory"]["after_percent"] < 75
    receipt_path = Path(receipt["receipt_path"])
    backup_dir = Path(receipt["backup_dir"])
    assert receipt_path.is_file()
    assert backup_dir.is_dir()
    assert (backup_dir / "MEMORY.md").read_text(encoding="utf-8") == DELIM.join(original)
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["transaction_id"] == receipt["transaction_id"]


def test_apply_rolls_back_changed_store_when_audit_write_fails(tmp_path: Path, monkeypatch) -> None:
    memories = tmp_path / "memories"
    memory_path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    user_path = _write(memories, "USER.md", ["User prefers concise replies."])
    before_memory = memory_path.read_bytes()
    before_user = user_path.read_bytes()

    import hermes_cli.memory_hygiene as memory_hygiene

    monkeypatch.setattr(
        memory_hygiene,
        "atomic_json_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated audit failure")),
    )

    with pytest.raises(HygieneError, match="audit receipt write failed"):
        apply_hygiene(
            memories,
            memory_char_limit=100,
            user_char_limit=100,
            target_percent=70,
            now=NOW,
            apply=True,
        )

    assert memory_path.read_bytes() == before_memory
    assert user_path.read_bytes() == before_user


def test_apply_does_not_create_or_rewrite_unchanged_user_store(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memory_path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    user_path = memories / "USER.md"
    assert not user_path.exists()

    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    assert memory_path.read_text(encoding="utf-8") == "Cache note."
    assert not user_path.exists()
    assert receipt["stores"]["memory"]["changed"] is True
    assert receipt["stores"]["user"]["changed"] is False


def test_apply_preserves_protected_user_bytes(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memory_path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    user_path = memories / "USER.md"
    user_bytes = b"  User prefers concise replies.\r\n\r\n"
    user_path.write_bytes(user_bytes)

    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=20,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    assert memory_path.read_text(encoding="utf-8") == "Cache note."
    assert user_path.read_bytes() == user_bytes
    assert receipt["stores"]["user"]["actions"] == []
    assert receipt["stores"]["user"]["changed"] is False


@pytest.mark.parametrize(
    "memory_bytes",
    [b"\n \n\n", b"\n  Always run release verification.  \n\n"],
)
def test_apply_preserves_memory_newlines_and_blank_lines_when_plan_empty(
    tmp_path: Path, memory_bytes: bytes
) -> None:
    memories = tmp_path / "memories"
    memories.mkdir(parents=True)
    memory_path = memories / "MEMORY.md"
    memory_path.write_bytes(memory_bytes)

    receipt = apply_hygiene(
        memories,
        memory_char_limit=20,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    assert memory_path.read_bytes() == memory_bytes
    assert receipt["stores"]["memory"]["actions"] == []
    assert receipt["stores"]["memory"]["changed"] is False


def test_report_and_apply_preserve_no_candidate_store_bytes(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    memories.mkdir(parents=True)
    memory_path = memories / "MEMORY.md"
    memory_bytes = b"Disposable cache note.\n\n"
    memory_path.write_bytes(memory_bytes)

    report = build_hygiene_report(
        memories,
        memory_char_limit=100,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
    )
    assert report["stores"]["memory"]["plan"] == []
    assert memory_path.read_bytes() == memory_bytes

    receipt = apply_hygiene(
        memories,
        memory_char_limit=100,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    assert memory_path.read_bytes() == memory_bytes
    assert receipt["stores"]["memory"]["actions"] == []
    assert receipt["stores"]["memory"]["changed"] is False


def test_apply_receipt_changed_matches_planned_byte_mutation(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    user_path = memories / "USER.md"
    user_bytes = b" User prefers dark mode. \n"
    user_path.write_bytes(user_bytes)

    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=20,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    for store in receipt["stores"].values():
        assert store["changed"] is bool(store["actions"])
        assert (store["before_sha256"] != store["after_sha256"]) is store["changed"]
    assert user_path.read_bytes() == user_bytes


def test_rollback_restores_backup_and_refuses_post_apply_drift(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    original = ["Disposable cache.", " disposable   cache. "]
    path = _write(memories, "MEMORY.md", original)
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )

    rollback = rollback_hygiene(Path(receipt["receipt_path"]), yes=True, now=NOW)
    assert rollback["status"] == "rolled-back"
    assert path.read_text(encoding="utf-8") == DELIM.join(original)

    second = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    path.write_text("newer operator edit", encoding="utf-8")
    with pytest.raises(HygieneError, match="drift"):
        rollback_hygiene(Path(second["receipt_path"]), yes=True, now=NOW)
    assert path.read_text(encoding="utf-8") == "newer operator edit"


def test_rollback_rejects_forged_receipt_paths(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    audit = memories / "audit"
    audit.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not overwrite", encoding="utf-8")
    forged = {
        "schema_version": 1,
        "status": "applied",
        "transaction_id": "forged",
        "memories_dir": str(memories),
        "backup_dir": str(tmp_path),
        "stores": {
            "memory": {
                "file": "../../outside.md",
                "existed_before": True,
                "after_sha256": "0" * 64,
            },
            "user": {
                "file": "USER.md",
                "existed_before": False,
                "after_sha256": "0" * 64,
            },
        },
    }
    receipt = audit / "forged.json"
    receipt.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(HygieneError, match="invalid receipt paths"):
        rollback_hygiene(receipt, yes=True, now=NOW)
    assert outside.read_text(encoding="utf-8") == "do not overwrite"


def test_rollback_rejects_tampered_backup(tmp_path: Path) -> None:
    memories = tmp_path / "memories"
    path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    backup = Path(receipt["backup_dir"]) / "MEMORY.md"
    backup.write_text("tampered", encoding="utf-8")

    with pytest.raises(HygieneError, match="backup hash mismatch"):
        rollback_hygiene(Path(receipt["receipt_path"]), yes=True, now=NOW)
    assert path.read_text(encoding="utf-8") == "Cache note."


def test_rollback_uses_verified_backup_bytes_without_toctou_reread(
    tmp_path: Path, monkeypatch
) -> None:
    memories = tmp_path / "memories"
    original = ["Cache note.", " cache   note. "]
    path = _write(memories, "MEMORY.md", original)
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    source_backup = Path(receipt["backup_dir"]) / "MEMORY.md"

    import hermes_cli.memory_hygiene as memory_hygiene

    real_write = memory_hygiene._atomic_write_bytes
    tampered = False

    def tamper_after_verification(path_arg: Path, content: bytes) -> None:
        nonlocal tampered
        if not tampered and path_arg.parent.name.startswith("rollback-"):
            source_backup.write_text("tampered after verification", encoding="utf-8")
            tampered = True
        real_write(path_arg, content)

    monkeypatch.setattr(memory_hygiene, "_atomic_write_bytes", tamper_after_verification)
    rollback_hygiene(Path(receipt["receipt_path"]), yes=True, now=NOW)

    assert tampered is True
    assert path.read_text(encoding="utf-8") == DELIM.join(original)


def test_rollback_audit_failure_retains_post_apply_state_and_source_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    memories = tmp_path / "memories"
    path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    receipt_path = Path(receipt["receipt_path"])
    post_apply_bytes = path.read_bytes()
    source_receipt_bytes = receipt_path.read_bytes()

    import hermes_cli.memory_hygiene as memory_hygiene

    real_audit_write = memory_hygiene.atomic_json_write

    def write_then_fail(*args, **kwargs) -> None:
        real_audit_write(*args, **kwargs)
        raise OSError("simulated rollback audit failure")

    monkeypatch.setattr(
        memory_hygiene,
        "atomic_json_write",
        write_then_fail,
    )

    with pytest.raises(HygieneError, match="rollback audit receipt write failed"):
        rollback_hygiene(receipt_path, yes=True, now=NOW)

    assert path.read_bytes() == post_apply_bytes
    assert receipt_path.read_bytes() == source_receipt_bytes
    assert list(receipt_path.parent.glob("rollback-*.json")) == []


def test_rollback_can_retry_after_audit_failure(tmp_path: Path, monkeypatch) -> None:
    memories = tmp_path / "memories"
    original = ["Cache note.", " cache   note. "]
    path = _write(memories, "MEMORY.md", original)
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    receipt_path = Path(receipt["receipt_path"])

    import hermes_cli.memory_hygiene as memory_hygiene

    real_audit_write = memory_hygiene.atomic_json_write
    attempts = 0

    def fail_once(*args, **kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated rollback audit failure")
        real_audit_write(*args, **kwargs)

    monkeypatch.setattr(memory_hygiene, "atomic_json_write", fail_once)

    with pytest.raises(HygieneError, match="rollback audit receipt write failed"):
        rollback_hygiene(receipt_path, yes=True, now=NOW)
    rollback = rollback_hygiene(receipt_path, yes=True, now=NOW)

    assert attempts == 2
    assert rollback["status"] == "rolled-back"
    assert path.read_text(encoding="utf-8") == DELIM.join(original)


def test_rollback_keyboard_interrupt_during_audit_retains_post_apply_state(
    tmp_path: Path, monkeypatch
) -> None:
    memories = tmp_path / "memories"
    path = _write(memories, "MEMORY.md", ["Cache note.", " cache   note. "])
    receipt = apply_hygiene(
        memories,
        memory_char_limit=30,
        user_char_limit=100,
        target_percent=70,
        now=NOW,
        apply=True,
    )
    receipt_path = Path(receipt["receipt_path"])
    post_apply_bytes = path.read_bytes()
    source_receipt_bytes = receipt_path.read_bytes()

    import hermes_cli.memory_hygiene as memory_hygiene

    monkeypatch.setattr(
        memory_hygiene,
        "atomic_json_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(HygieneError, match="rollback audit receipt write failed"):
        rollback_hygiene(receipt_path, yes=True, now=NOW)

    assert path.read_bytes() == post_apply_bytes
    assert receipt_path.read_bytes() == source_receipt_bytes
