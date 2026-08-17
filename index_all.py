import asyncio
import logging
import os

from nce.orchestrator import NCEEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("index_all")


def get_files_to_index(root_dir="."):
    """
    Finds all Python files to index while excluding non-source directories.
    """
    ignore_dirs = {
        ".venv",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "env",
        "venv",
    }
    files = []
    for root, dirs, filenames in os.walk(root_dir):
        # Modify dirs in-place to prevent os.walk from descending into ignored directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in filenames:
            if f.endswith(".py"):
                # Normalize path for indexing
                files.append(os.path.normpath(os.path.join(root, f)))
    return files


async def index_repo(namespace_id: str = "default"):
    files_to_index = get_files_to_index()
    log.info(
        "Found %s Python files to index (namespace=%s).",
        len(files_to_index),
        namespace_id,
    )

    engine = NCEEngine()
    await engine.connect()

    try:
        # Use a semaphore to avoid overwhelming the database/Redis with too many concurrent requests
        sem = asyncio.Semaphore(10)

        async def process_file(filepath):
            async with sem:
                try:
                    with open(filepath, encoding="utf-8") as f:
                        raw_code = f.read()

                    from uuid import UUID

                    from nce.models import IndexCodeFileRequest

                    ns_uuid = None
                    if namespace_id:
                        try:
                            ns_uuid = UUID(str(namespace_id))
                        except ValueError:
                            pass

                    payload = IndexCodeFileRequest(
                        filepath=filepath,
                        raw_code=raw_code,
                        language="python",
                        namespace_id=ns_uuid,
                    )
                    res = await engine.index_code_file(payload)
                    return res
                except Exception as e:
                    log.error("Error submitting %s: %s", filepath, e)
                    return {"status": "error", "filepath": filepath, "error": str(e)}

        chunk_size = 20
        all_enqueued = []
        all_skipped = []
        all_errors = []

        log.info("Submitting files for indexing in chunks of %s...", chunk_size)
        for i in range(0, len(files_to_index), chunk_size):
            chunk = files_to_index[i : i + chunk_size]
            chunk_num = (i // chunk_size) + 1
            total_chunks = (len(files_to_index) + chunk_size - 1) // chunk_size
            log.info("Processing chunk %s/%s (%s files)...", chunk_num, total_chunks, len(chunk))

            tasks = [process_file(f) for f in chunk]
            results = await asyncio.gather(*tasks)

            chunk_enqueued = [r["job_id"] for r in results if r and r.get("status") == "enqueued"]
            chunk_skipped = [r for r in results if r and r.get("status") == "skipped"]
            chunk_errors = [r for r in results if r and r.get("status") == "error"]

            all_enqueued.extend(chunk_enqueued)
            all_skipped.extend(chunk_skipped)
            all_errors.extend(chunk_errors)

            pending_jobs = set(chunk_enqueued)
            while pending_jobs:
                log.info("Waiting for %s jobs in current chunk to complete...", len(pending_jobs))
                await asyncio.sleep(2)  # Graceful non-blocking wait

                async def check_status(j_id):
                    async with sem:
                        return await engine.get_job_status(j_id)

                status_results = await asyncio.gather(
                    *(check_status(j_id) for j_id in pending_jobs)
                )

                done_jobs = set()
                for status_res in status_results:
                    job_id = status_res.get("job_id")
                    status = status_res.get("status")

                    # Check for terminal states
                    if status in ("finished", "failed", "canceled", "not_found"):
                        if status == "failed":
                            log.error("Job %s failed: %s", job_id, status_res.get("error"))
                        elif status == "finished":
                            log.debug("Job %s finished successfully.", job_id)
                        else:
                            log.warning("Job %s completed with status: %s", job_id, status)
                        done_jobs.add(job_id)

                pending_jobs -= done_jobs

            log.info("Chunk %s/%s finished processing.", chunk_num, total_chunks)
            if i + chunk_size < len(files_to_index):
                # Cooldown period to allow DB indexes to catch up
                await asyncio.sleep(1.0)

        log.info(
            "All indexing completed. Total Enqueued: %s, Total Skipped: %s, Total Errors: %s.",
            len(all_enqueued),
            len(all_skipped),
            len(all_errors),
        )

    finally:
        await engine.disconnect()


if __name__ == "__main__":
    import sys

    ns = os.environ.get("TRIMCP_NAMESPACE_ID", "default")
    if len(sys.argv) > 1:
        ns = sys.argv[1]
    asyncio.run(index_repo(ns))
