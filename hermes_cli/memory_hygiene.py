"""Conservative, deterministic hygiene for built-in durable memory files.

The default operation is report-only. Applying a report requires an explicit
``apply=True`` boundary, snapshots both stores before writing, updates only
changed stores under the existing memory-file locks, and rolls the transaction
back if any write fails. USER.md and entries that look like identity, preferences,
security policy, or active operating rules are reportable but never changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tools.memory_tool import ENTRY_DELIMITER, MemoryStore
from utils import atomic_json_write, atomic_replace


DEFAULT_TARGET_PERCENT = 70
DEFAULT_STALE_DAYS = 90
_STORE_FILES = {"memory": "MEMORY.md", "user": "USER.md"}
_PROTECTED_PATTERNS = (
    ("identity or preference", re.compile(
        r"\b(user|owner|name|identity|role|timezone|prefer(?:s|red|ence)?|expects?|likes?|dislikes?)\b",
        re.IGNORECASE,
    )),
    ("security or approval rule", re.compile(
        r"\b(security|credential|secret|password|token|approval|authori[sz]ed?|permission|authentication|human-gated)\b",
        re.IGNORECASE,
    )),
    ("active operating rule", re.compile(
        r"\b(always|never|must|must not|required|do not|don't|default|workflow|procedure|runbook|source of truth|authoritative|project|repository|repo|uses?|runs?)\b",
        re.IGNORECASE,
    )),
    ("active runtime fact", re.compile(
        r"\b(active|current|production|running|enabled)\b",
        re.IGNORECASE,
    )),
)
_LEADING_IDENTITY_RE = re.compile(r"^[A-Z][A-Za-z0-9_.-]{1,31}\s*:")
_SUPERSEDED_RE = re.compile(
    r"(?:^|\b)(?:\[superseded\]|\[obsolete\]|superseded by|obsolete:)",
    re.IGNORECASE,
)
_STALE_HINT_RE = re.compile(
    r"(?:\[stale\]|\b(?:temporary|todo|completed task|completed migration|finished task|one-off)\b)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_TRANSACTION_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
_VERBOSE_PREFIX_RE = re.compile(
    r"^(?:it is important to remember that|please remember that)\s+",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class HygieneError(RuntimeError):
    """A hygiene transaction could not complete safely."""


def _utc_now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _split_entries(content: bytes) -> list[str]:
    if not content.strip():
        return []
    text = content.decode("utf-8")
    return [entry.strip() for entry in text.split(ENTRY_DELIMITER) if entry.strip()]


def _render_entries(entries: list[str]) -> bytes:
    return ENTRY_DELIMITER.join(entries).encode("utf-8") if entries else b""


def _usage(entries: list[str]) -> int:
    return len(ENTRY_DELIMITER.join(entries)) if entries else 0


def _percent(chars: int, limit: int) -> int:
    return int((chars / limit) * 100) if limit > 0 else 0


def _normalized(entry: str) -> str:
    return " ".join(entry.split()).casefold()


def _protected_reason(target: str, entry: str) -> Optional[str]:
    if target == "user":
        return "user-profile entries require manual review"
    if _LEADING_IDENTITY_RE.match(entry):
        return "leading identity label"
    for reason, pattern in _PROTECTED_PATTERNS:
        if pattern.search(entry):
            return reason
    return None


def _is_stale(entry: str, *, now: datetime, stale_days: int) -> bool:
    if not _STALE_HINT_RE.search(entry):
        return False
    match = _DATE_RE.search(entry)
    if not match:
        return entry.lstrip().lower().startswith("[stale]")
    try:
        dated = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (now - dated).days >= stale_days


def _compact_verbose(entry: str) -> str:
    compact = " ".join(entry.split())
    compact = _VERBOSE_PREFIX_RE.sub("", compact)
    sentences = _SENTENCE_RE.split(compact)
    seen: set[str] = set()
    unique: list[str] = []
    for sentence in sentences:
        key = _normalized(sentence)
        if key and key not in seen:
            seen.add(key)
            unique.append(sentence)
    return " ".join(unique).strip()


def _candidate(
    *, index: int, classification: str, before: str, after: Optional[str], reason: str
) -> dict[str, Any]:
    action = "remove" if after is None else "replace"
    before_chars = len(before)
    after_chars = len(after or "")
    return {
        "index": index,
        "classification": classification,
        "action": action,
        "before": before,
        "after": after,
        "reason": reason,
        "estimated_savings": before_chars - after_chars,
    }


def _analyze_store(
    target: str,
    entries: list[str],
    *,
    limit: int,
    target_percent: int,
    stale_days: int,
    now: datetime,
) -> dict[str, Any]:
    protected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        reason = _protected_reason(target, entry)
        key = _normalized(entry)
        if reason:
            protected.append({"index": index, "entry": entry, "reason": reason})
            seen.add(key)
            continue
        if key in seen:
            candidates.append(_candidate(
                index=index,
                classification="duplicate",
                before=entry,
                after=None,
                reason="same text after case and whitespace normalization",
            ))
            continue
        seen.add(key)
        if _SUPERSEDED_RE.search(entry):
            candidates.append(_candidate(
                index=index,
                classification="superseded",
                before=entry,
                after=None,
                reason="entry carries an explicit superseded or obsolete marker",
            ))
            continue
        if _is_stale(entry, now=now, stale_days=stale_days):
            candidates.append(_candidate(
                index=index,
                classification="stale",
                before=entry,
                after=None,
                reason=f"explicit temporary/completed marker is at least {stale_days} days old",
            ))
            continue
        compact = _compact_verbose(entry)
        if compact and compact != entry and len(compact) < len(entry):
            candidates.append(_candidate(
                index=index,
                classification="verbose",
                before=entry,
                after=compact,
                reason="lossless whitespace, boilerplate, or repeated-sentence compaction",
            ))

    # Plan safe actions in deterministic priority/index order, stopping once the
    # configured target is reached. Candidate discovery remains complete even
    # when only a prefix is needed for the plan.
    priority = {"duplicate": 0, "superseded": 1, "stale": 2, "verbose": 3}
    ordered = sorted(candidates, key=lambda item: (priority[item["classification"]], item["index"]))
    working = list(entries)
    plan: list[dict[str, Any]] = []
    target_chars = (limit * target_percent) // 100
    if _usage(working) > target_chars:
        for item in ordered:
            original_index = item["index"]
            before = item["before"]
            # Locate the exact original at or after its original position. Earlier
            # removals shift indices, so content plus original order is safer than
            # carrying a mutable index in the public receipt.
            current_index = next(
                (i for i, value in enumerate(working) if value == before),
                None,
            )
            if current_index is None:
                continue
            if item["action"] == "remove":
                working.pop(current_index)
            else:
                working[current_index] = item["after"]
            plan.append({**item, "original_index": original_index})
            if _usage(working) <= target_chars:
                break

    before_chars = _usage(entries)
    after_chars = _usage(working)
    return {
        "file": _STORE_FILES[target],
        "limit": limit,
        "before_chars": before_chars,
        "before_percent": _percent(before_chars, limit),
        "projected_chars": after_chars,
        "projected_percent": _percent(after_chars, limit),
        "target_met": after_chars <= target_chars,
        "candidates": candidates,
        "protected": protected,
        "plan": plan,
        "projected_entries": working,
    }


def build_hygiene_report(
    memories_dir: Path,
    *,
    memory_char_limit: int,
    user_char_limit: int,
    target_percent: int = DEFAULT_TARGET_PERCENT,
    stale_days: int = DEFAULT_STALE_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a deterministic dry-run report without changing either store."""
    if not 1 <= target_percent <= 74:
        raise ValueError("target_percent must be 1 through 74")
    if stale_days < 1:
        raise ValueError("stale_days must be positive")
    if memory_char_limit < 1 or user_char_limit < 1:
        raise ValueError("memory limits must be positive")

    at = _utc_now(now)
    stores: dict[str, Any] = {}
    for target, filename in _STORE_FILES.items():
        path = Path(memories_dir) / filename
        content = _read_bytes(path)
        limit = memory_char_limit if target == "memory" else user_char_limit
        store = _analyze_store(
            target,
            _split_entries(content),
            limit=limit,
            target_percent=target_percent,
            stale_days=stale_days,
            now=at,
        )
        store["source_sha256"] = _sha256(content)
        stores[target] = store

    return {
        "schema_version": 1,
        "mode": "dry-run",
        "generated_at": at.isoformat().replace("+00:00", "Z"),
        "memories_dir": str(Path(memories_dir).resolve()),
        "target_percent": target_percent,
        "stale_days": stale_days,
        "stores": stores,
        "external_providers_touched": False,
    }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _transaction_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def apply_hygiene(
    memories_dir: Path,
    *,
    memory_char_limit: int,
    user_char_limit: int,
    target_percent: int = DEFAULT_TARGET_PERCENT,
    stale_days: int = DEFAULT_STALE_DAYS,
    now: Optional[datetime] = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Report by default; apply only across the explicit ``apply=True`` boundary."""
    memories_dir = Path(memories_dir)
    at = _utc_now(now)
    if not apply:
        return build_hygiene_report(
            memories_dir,
            memory_char_limit=memory_char_limit,
            user_char_limit=user_char_limit,
            target_percent=target_percent,
            stale_days=stale_days,
            now=at,
        )

    paths = {target: memories_dir / filename for target, filename in _STORE_FILES.items()}
    with ExitStack() as stack:
        for target in sorted(paths):
            stack.enter_context(MemoryStore._file_lock(paths[target]))

        report = build_hygiene_report(
            memories_dir,
            memory_char_limit=memory_char_limit,
            user_char_limit=user_char_limit,
            target_percent=target_percent,
            stale_days=stale_days,
            now=at,
        )
        transaction_id = _transaction_id(at)
        backup_dir = memories_dir / "backups" / transaction_id
        audit_dir = memories_dir / "audit"
        originals = {target: _read_bytes(path) for target, path in paths.items()}
        existed = {target: path.exists() for target, path in paths.items()}
        backup_dir.mkdir(parents=True, exist_ok=False)
        for target, filename in _STORE_FILES.items():
            _atomic_write_bytes(backup_dir / filename, originals[target])

        stores_receipt: dict[str, Any] = {}
        new_content: dict[str, bytes] = {}
        for target, store in report["stores"].items():
            content = _render_entries(store["projected_entries"])
            new_content[target] = content
            stores_receipt[target] = {
                "file": store["file"],
                "existed_before": existed[target],
                "changed": originals[target] != content,
                "before_sha256": _sha256(originals[target]),
                "after_sha256": _sha256(content),
                "before_chars": store["before_chars"],
                "after_chars": store["projected_chars"],
                "after_percent": store["projected_percent"],
                "target_met": store["target_met"],
                "actions": store["plan"],
            }

        changed_targets = [
            target for target in ("memory", "user") if originals[target] != new_content[target]
        ]
        written_targets: list[str] = []
        try:
            for target in changed_targets:
                _atomic_write_bytes(paths[target], new_content[target])
                written_targets.append(target)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for target in written_targets:
                try:
                    if existed[target]:
                        _atomic_write_bytes(paths[target], originals[target])
                    else:
                        paths[target].unlink(missing_ok=True)
                except BaseException as rollback_exc:  # pragma: no cover - catastrophic I/O
                    rollback_errors.append(f"{target}: {rollback_exc}")
            detail = f"; rollback errors: {', '.join(rollback_errors)}" if rollback_errors else ""
            raise HygieneError(f"memory hygiene apply failed and was rolled back: {exc}{detail}") from exc

        receipt_path = audit_dir / f"{transaction_id}.json"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "status": "applied",
            "transaction_id": transaction_id,
            "applied_at": at.isoformat().replace("+00:00", "Z"),
            "memories_dir": str(memories_dir.resolve()),
            "target_percent": target_percent,
            "stale_days": stale_days,
            "backup_dir": str(backup_dir.resolve()),
            "receipt_path": str(receipt_path.resolve()),
            "stores": stores_receipt,
            "external_providers_touched": False,
            "rollback_command": f"hermes memory hygiene --rollback {receipt_path.resolve()} --yes",
        }
        try:
            atomic_json_write(receipt_path, receipt, indent=2)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for target in changed_targets:
                if existed[target]:
                    try:
                        _atomic_write_bytes(paths[target], originals[target])
                    except BaseException as rollback_exc:  # pragma: no cover - catastrophic I/O
                        rollback_errors.append(f"{target}: {rollback_exc}")
                else:
                    try:
                        paths[target].unlink(missing_ok=True)
                    except BaseException as rollback_exc:  # pragma: no cover - catastrophic I/O
                        rollback_errors.append(f"{target}: {rollback_exc}")
            detail = f"; rollback errors: {', '.join(rollback_errors)}" if rollback_errors else ""
            raise HygieneError(
                f"audit receipt write failed and memory changes were rolled back: {exc}{detail}"
            ) from exc
        return receipt


def rollback_hygiene(
    receipt_path: Path,
    *,
    yes: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Restore an applied transaction, refusing to overwrite later memory drift."""
    if not yes:
        raise HygieneError("rollback requires explicit confirmation")
    receipt_path = Path(receipt_path).resolve()
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HygieneError(f"cannot read hygiene receipt: {exc}") from exc
    if receipt.get("status") != "applied" or receipt.get("schema_version") != 1:
        raise HygieneError("receipt is not an applied memory-hygiene transaction")

    memories_dir = receipt_path.parent.parent.resolve()
    backup_dir = memories_dir / "backups" / ".invalid"
    stores: dict[str, Any] = {}
    try:
        memories_dir = Path(receipt["memories_dir"]).resolve()
        backup_dir = Path(receipt["backup_dir"]).resolve()
        stores = receipt["stores"]
        transaction_id = receipt["transaction_id"]
        expected_memories_dir = receipt_path.parent.parent.resolve()
        expected_backup_root = (memories_dir / "backups").resolve()
        valid_paths = (
            receipt_path.parent.name == "audit"
            and memories_dir == expected_memories_dir
            and isinstance(transaction_id, str)
            and _TRANSACTION_RE.fullmatch(transaction_id) is not None
            and receipt_path.name == f"{transaction_id}.json"
            and Path(receipt.get("receipt_path", "")).resolve() == receipt_path
            and backup_dir == (expected_backup_root / transaction_id).resolve()
            and set(stores) == set(_STORE_FILES)
            and all(stores[target].get("file") == filename for target, filename in _STORE_FILES.items())
            and all(isinstance(stores[target].get("changed"), bool) for target in _STORE_FILES)
            and all(
                re.fullmatch(r"[0-9a-f]{64}", stores[target].get(hash_key, "")) is not None
                for target in _STORE_FILES
                for hash_key in ("before_sha256", "after_sha256")
            )
        )
    except (KeyError, TypeError, ValueError, OSError):
        valid_paths = False
    if not valid_paths:
        raise HygieneError("invalid receipt paths; refusing rollback")

    paths = {target: memories_dir / filename for target, filename in _STORE_FILES.items()}
    at = _utc_now(now)

    with ExitStack() as stack:
        for target in sorted(paths):
            stack.enter_context(MemoryStore._file_lock(paths[target]))
        verified_backups: dict[str, bytes] = {}
        for target in sorted(paths):
            backup = backup_dir / stores[target]["file"]
            try:
                backup_bytes = backup.read_bytes()
            except OSError as exc:
                raise HygieneError(f"cannot read backup for {target}: {exc}") from exc
            if _sha256(backup_bytes) != stores[target]["before_sha256"]:
                raise HygieneError(f"backup hash mismatch for {target}; refusing rollback")
            verified_backups[target] = backup_bytes

        changed_targets = [target for target in sorted(paths) if stores[target]["changed"]]
        for target in changed_targets:
            path = paths[target]
            current_hash = _sha256(_read_bytes(path))
            if current_hash != stores[target]["after_sha256"]:
                raise HygieneError(
                    f"refusing rollback: {path.name} has drift since the apply receipt"
                )

        rollback_id = _transaction_id(at)
        rollback_backup = memories_dir / "backups" / f"rollback-{rollback_id}"
        rollback_backup.mkdir(parents=True, exist_ok=False)
        post_apply_contents: dict[str, bytes] = {}
        for target in changed_targets:
            path = paths[target]
            post_apply_contents[target] = _read_bytes(path)
            _atomic_write_bytes(rollback_backup / path.name, post_apply_contents[target])

        restored: list[str] = []
        try:
            for target in changed_targets:
                path = paths[target]
                if stores[target]["existed_before"]:
                    _atomic_write_bytes(path, verified_backups[target])
                else:
                    path.unlink(missing_ok=True)
                restored.append(target)
        except BaseException as exc:
            for target in restored:
                path = paths[target]
                _atomic_write_bytes(path, post_apply_contents[target])
            raise HygieneError(f"rollback failed; post-apply state was restored: {exc}") from exc

        rollback_receipt_path = memories_dir / "audit" / f"rollback-{rollback_id}.json"
        rollback_receipt = {
            "schema_version": 1,
            "status": "rolled-back",
            "transaction_id": rollback_id,
            "rolled_back_transaction_id": receipt["transaction_id"],
            "rolled_back_at": at.isoformat().replace("+00:00", "Z"),
            "receipt_path": str(rollback_receipt_path.resolve()),
            "source_receipt": str(receipt_path),
            "safety_backup_dir": str(rollback_backup.resolve()),
        }
        atomic_json_write(rollback_receipt_path, rollback_receipt, indent=2)
        return rollback_receipt
