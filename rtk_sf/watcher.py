"""
watcher.py — OS file watcher for live differential re-indexing.

Uses the `watchdog` library to monitor a Salesforce DX project directory.
When a .cls or .xml file changes, it is re-indexed immediately.

Usage:
    python3 -m rtk_sf watch [--path ./force-app]
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    logger.warning(
        "watchdog library not installed. Run: pip install watchdog>=3.0.0"
    )


WATCHED_EXTENSIONS = {".cls", ".xml", ".trigger", ".page", ".component"}
IGNORED_SUFFIXES = {".cls-meta.xml", ".trigger-meta.xml"}


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------


class _SalesforceEventHandler:
    """
    Handles file system events and triggers incremental re-indexing.

    This class is defined regardless of whether watchdog is installed so that
    the module can be imported safely; it dynamically inherits from
    FileSystemEventHandler only when watchdog is available.
    """

    # Will be set to the real base class when watchdog is available
    _base_class: Any = object

    def __init__(self, indexer: Any, search_engine: Any) -> None:
        super().__init__()  # type: ignore[call-arg]
        self._indexer = indexer
        self._search = search_engine

    def _should_process(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in WATCHED_EXTENSIONS:
            return False
        for suffix in IGNORED_SUFFIXES:
            if path.endswith(suffix):
                return False
        # Ignore hidden directories
        if any(part.startswith(".") for part in p.parts):
            return False
        return True

    def on_modified(self, event: Any) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path, "modified")

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path, "created")

    def on_deleted(self, event: Any) -> None:
        if event.is_directory:
            return
        path = event.src_path
        if not self._should_process(path):
            return
        file_path = Path(path)
        name = file_path.stem
        logger.info("File deleted, removing from index: %s", path)
        try:
            self._indexer.registry.remove(file_path)
            self._search.delete_component(name)
        except Exception as exc:
            logger.error("Error removing %s from index: %s", name, exc)

    def _handle(self, path: str, event_type: str) -> None:
        if not self._should_process(path):
            return
        file_path = Path(path)
        logger.info("File %s: %s", event_type, path)
        try:
            indexed = self._indexer.index_file(file_path, force=True)
            if indexed:
                # Sync the updated spec into the search DB
                self._search.sync_from_specs()
        except Exception as exc:
            logger.error("Error re-indexing %s: %s", path, exc)


# Dynamically set up the real class with watchdog base when available
if _WATCHDOG_AVAILABLE:

    class SalesforceEventHandler(_SalesforceEventHandler, FileSystemEventHandler):  # type: ignore[misc]
        """Watchdog-based event handler for Salesforce metadata files."""

        pass

else:

    class SalesforceEventHandler(_SalesforceEventHandler):  # type: ignore[no-redef]
        """Stub event handler (watchdog not installed)."""

        pass


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class FileWatcher:
    """
    Watches a Salesforce DX project directory and re-indexes changed files.

    Args:
        project_root: Root of the Salesforce project.
        watch_path: Directory to watch (defaults to force-app/).
    """

    def __init__(
        self,
        project_root: str | Path = ".",
        watch_path: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.watch_path = Path(watch_path) if watch_path else self.project_root / "force-app"
        if not self.watch_path.exists():
            self.watch_path = self.project_root
            logger.info("force-app not found; watching project root: %s", self.watch_path)

        self._observer: Any = None
        self._indexer: Any = None
        self._search: Any = None

    def _setup(self) -> None:
        """Lazily import and initialize the indexer and search engine."""
        from rtk_sf.indexer import SalesforceIndexer
        from rtk_sf.search import SearchEngine

        self._indexer = SalesforceIndexer(self.project_root)
        self._indexer._ensure_dirs()
        self._search = SearchEngine(self.project_root)
        # Perform initial index sync
        logger.info("Performing initial sync before watching...")
        self._search.sync_from_specs()

    def start(self, blocking: bool = True) -> None:
        """
        Start watching the project directory for changes.

        Args:
            blocking: If True, block until KeyboardInterrupt. If False,
                      start the observer in the background and return immediately.
        """
        if not _WATCHDOG_AVAILABLE:
            raise RuntimeError(
                "watchdog library is required for file watching. "
                "Install it with: pip install watchdog>=3.0.0"
            )

        self._setup()
        handler = SalesforceEventHandler(self._indexer, self._search)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.watch_path), recursive=True)
        self._observer.start()

        logger.info("Watching for changes in: %s", self.watch_path)
        print(f"rtk-sf watcher started. Monitoring: {self.watch_path}")
        print("Press Ctrl+C to stop.")

        if blocking:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stop()

    def stop(self) -> None:
        """Stop the file watcher."""
        if self._observer:
            logger.info("Stopping file watcher...")
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._search:
            self._search.close()
        print("rtk-sf watcher stopped.")

    def __enter__(self) -> "FileWatcher":
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()
