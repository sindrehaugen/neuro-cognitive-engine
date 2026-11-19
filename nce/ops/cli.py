"""Operator CLI entrypoint (``nce-ops``) for watermark backup, restore, drill & status."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from nce.ops.backup import (
    create_watermark_backup,
    get_backup_status,
    restore_watermark_backup,
    run_ephemeral_restore_drill,
    sweep_stale_drill_targets,
)
from nce.ops.models import OpsDiskHeadroomError, OpsRestorePreflightError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nce-ops",
        description="NCE Day-2 Operations CLI for T_cut backup, verified restore, drill, and status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Create a verified T_cut watermark backup.")
    p_backup.add_argument("--backup-dir", default=None, help="Override backup archive directory.")
    p_backup.add_argument("--holder-id", default=None, help="Backup lease holder identifier.")
    p_backup.add_argument(
        "--retention-count", type=int, default=None, help="Archive retention count."
    )
    p_backup.add_argument(
        "--min-free-bytes", type=int, default=None, help="Override free disk headroom floor."
    )
    p_backup.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    p_restore = sub.add_parser("restore", help="Verify and restore a T_cut watermark backup.")
    p_restore.add_argument("archive_path", help="Path to the .tar.zst or .tar.gz backup archive.")
    p_restore.add_argument("--manifest", default=None, help="Explicit manifest JSON sidecar path.")
    p_restore.add_argument(
        "--backup-dir", default=None, help="Override persistent backup/tombstone dir."
    )
    p_restore.add_argument(
        "--tombstones", default=None, help="Explicit shred_tombstones.jsonl path."
    )
    p_restore.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    p_drill = sub.add_parser(
        "drill",
        aliases=["restore-drill"],
        help="Run an isolated ephemeral single-host restore drill.",
    )
    p_drill.add_argument("--archive", default=None, help="Specific archive path to drill.")
    p_drill.add_argument("--backup-id", default=None, help="Specific backup UUID to drill.")
    p_drill.add_argument("--manifest", default=None, help="Explicit manifest JSON path.")
    p_drill.add_argument("--backup-dir", default=None, help="Override backup dir.")
    p_drill.add_argument(
        "--sweep-stale", action="store_true", help="Sweep leftover drill targets first."
    )
    p_drill.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    p_status = sub.add_parser(
        "status", help="Inspect backup inventory, lease state, and disk headroom."
    )
    p_status.add_argument("--backup-dir", default=None, help="Override backup directory.")
    p_status.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    return parser


def _print_output(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {v}")
    else:
        print(data)


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "backup":
        rec = await create_watermark_backup(
            backup_dir=args.backup_dir,
            holder_id=args.holder_id,
            retention_count=args.retention_count,
            min_free_bytes=args.min_free_bytes,
        )
        _print_output(rec.model_dump(mode="json"), as_json=True if args.json else False)
        return 0

    if args.command == "restore":
        res = await restore_watermark_backup(
            args.archive_path,
            manifest_path=args.manifest,
            backup_dir=args.backup_dir,
            tombstones_path=args.tombstones,
        )
        _print_output(res, as_json=args.json)
        return 0

    if args.command in ("drill", "restore-drill"):
        if args.sweep_stale:
            await sweep_stale_drill_targets(backup_dir=args.backup_dir, max_age_seconds=0)
        report = await run_ephemeral_restore_drill(
            args.archive,
            backup_id=args.backup_id,
            manifest_path=args.manifest,
            backup_dir=args.backup_dir,
        )
        _print_output(report.model_dump(mode="json"), as_json=True if args.json else False)
        return 0 if report.status == "passed" else 1

    if args.command == "status":
        st = await get_backup_status(backup_dir=args.backup_dir)
        _print_output(st, as_json=True if args.json else False)
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint registered as ``nce-ops``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except (OpsDiskHeadroomError, OpsRestorePreflightError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
