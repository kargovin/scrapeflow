"""One-time sweep of the `latest/` prefix left behind by the pre-P6 dual write.

`latest/` is removed by ADR-011 §4. Nothing writes it any more and nothing ever read
it, so what remains in the bucket is dead weight from before the cutover.

⚠️ **This deletes production objects and is the owner's to authorise.** It is a script
rather than a migration step for exactly that reason: nothing runs it automatically,
and it refuses to delete anything unless `--apply` is passed.

**No accounting adjustment is needed, and none is performed.** `latest/` was never
counted on either side: the storage meter is incremented from the `history/` object,
and the old permanent-delete path removed the `latest/` key without decrementing. That
is ADR-009 §8d's charging rule, which ADR-011 upholds — only the *sequencing* clause
of §8d was reversed. A sweep that decremented would therefore corrupt the counter.

Volume is bounded: the bucket was emptied at the Clerk production cutover
(2026-07-03), so only objects written since then exist.

Usage (from ./docker):

    # report only — the default, and safe
    docker compose exec api uv run python scripts/sweep_latest_objects.py

    # actually delete
    docker compose exec api uv run python scripts/sweep_latest_objects.py --apply
"""

import argparse
import asyncio

import structlog

from app.core.minio import close_client, create_client
from app.settings import settings

logger = structlog.get_logger()

PREFIX = "latest/"


async def _sweep(minio, apply: bool) -> tuple[int, int, int]:
    """Return (found, deleted, failed). Deletes nothing unless apply is True."""
    found = deleted = failed = 0
    total_bytes = 0

    objects = await minio.list_objects(settings.minio_bucket, prefix=PREFIX, recursive=True)
    for obj in objects:
        found += 1
        total_bytes += obj.size or 0

        if not apply:
            logger.info("sweep: would delete", key=obj.object_name, bytes=obj.size)
            continue

        try:
            await minio.remove_object(settings.minio_bucket, obj.object_name)
            deleted += 1
            logger.info("sweep: deleted", key=obj.object_name, bytes=obj.size)
        except Exception:
            failed += 1
            logger.exception("sweep: delete failed", key=obj.object_name)

    logger.info(
        "sweep: done",
        mode="apply" if apply else "dry-run",
        found=found,
        deleted=deleted,
        failed=failed,
        bytes=total_bytes,
    )
    return found, deleted, failed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete. Without it the script only reports what it would remove.",
    )
    args = parser.parse_args()

    if not args.apply:
        logger.warning("sweep: DRY RUN — nothing will be deleted. Pass --apply to delete.")

    minio = await create_client()
    try:
        await _sweep(minio, apply=args.apply)
    finally:
        await close_client(minio)


if __name__ == "__main__":
    asyncio.run(main())
