"""Background indexer that runs in a daemon thread.

Provides non-blocking startup: the server is immediately queryable using
whatever embeddings are already cached. The indexer discovers new/changed
sessions and embeds them incrementally in the background.
"""

import ctypes
import gc
import hashlib
import logging
import platform
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .cache import EmbeddingCache
from .config import get_config
from .embeddings import EmbeddingBackend, get_embedding_backend
from .memory import get_memory_limit, select_sessions_within_limit
from .sessions import list_all_sessions, load_session_messages

logger = logging.getLogger(__name__)


def _release_memory():
    """Force garbage collection and return freed memory to the OS where possible."""
    gc.collect()
    # On Linux, ask glibc to return freed pages to the OS
    if platform.system() == "Linux":
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass


class IndexerState(str, Enum):
    """Current state of the background indexer."""
    IDLE = "idle"
    STARTING = "starting"
    DISCOVERING = "discovering"
    INDEXING = "indexing"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class IndexingStatus:
    """Shared status object readable by MCP tools."""
    state: IndexerState = IndexerState.IDLE
    sessions_total: int = 0
    sessions_processed: int = 0
    sessions_unchanged: int = 0
    messages_embedded: int = 0
    messages_cached: int = 0
    messages_skipped: int = 0
    errors: int = 0
    current_session: str = ""
    rate: float = 0.0  # messages per second
    started_at: float | None = None
    completed_at: float | None = None
    last_completed_at: float | None = None  # Timestamp of last successful pass (persists across rescans)
    last_error: str = ""
    embedding_count: int = 0  # Total embeddings in cache

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def progress_percent(self) -> float:
        if self.sessions_total == 0:
            return 0.0
        return (self.sessions_processed + self.sessions_unchanged) / self.sessions_total * 100

    @property
    def eta_seconds(self) -> float:
        if self.rate <= 0 or self.state != IndexerState.INDEXING:
            return 0.0
        remaining = self.sessions_total - self.sessions_processed - self.sessions_unchanged
        # Rough estimate: ~2 messages per session average
        return remaining * 2 / self.rate

    def to_dict(self) -> dict:
        """Convert to a dict suitable for MCP tool response."""
        return {
            "state": self.state.value,
            "progress_percent": round(self.progress_percent, 1),
            "sessions_total": self.sessions_total,
            "sessions_processed": self.sessions_processed,
            "sessions_unchanged": self.sessions_unchanged,
            "messages_embedded": self.messages_embedded,
            "messages_cached": self.messages_cached,
            "messages_skipped": self.messages_skipped,
            "errors": self.errors,
            "current_session": self.current_session,
            "rate_msg_per_sec": round(self.rate, 2),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "eta_seconds": round(self.eta_seconds, 0),
            "last_error": self.last_error,
            "embedding_count": self.embedding_count,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat() if self.started_at else None,
            "completed_at": datetime.fromtimestamp(self.completed_at).isoformat() if self.completed_at else None,
            "last_completed_at": datetime.fromtimestamp(self.last_completed_at).isoformat() if self.last_completed_at else None,
        }


class BackgroundIndexer:
    """Manages background indexing in a daemon thread.

    Usage:
        indexer = BackgroundIndexer()
        indexer.start()  # Non-blocking, returns immediately
        status = indexer.status  # Check progress
        indexer.trigger_reindex()  # Force a full rebuild
    """

    def __init__(self):
        self._status = IndexingStatus()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._reindex_event = threading.Event()
        self._rescan_event = threading.Event()
        self._cache: EmbeddingCache | None = None
        self._backend: EmbeddingBackend | None = None
        self._lock = threading.Lock()

    @property
    def status(self) -> IndexingStatus:
        return self._status

    @property
    def cache(self) -> EmbeddingCache | None:
        return self._cache

    @property
    def backend(self) -> EmbeddingBackend | None:
        return self._backend

    def start(self):
        """Start the background indexer thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="indexer")
        self._thread.start()

    def stop(self):
        """Signal the indexer to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger_reindex(self):
        """Trigger a full reindex (clears session state)."""
        self._reindex_event.set()
        # If the thread is not alive (e.g., rescan_interval=0), restart it
        if not self._thread or not self._thread.is_alive():
            self.start()

    def trigger_rescan(self):
        """Trigger an immediate rescan without clearing session state.

        Wakes the periodic rescan loop early. Only new/changed files are processed.
        """
        self._rescan_event.set()
        # If the thread is not alive, restart it
        if not self._thread or not self._thread.is_alive():
            self.start()

    def _run(self):
        """Main indexer loop. Runs initial scan, then rescans periodically."""
        try:
            self._status.state = IndexerState.STARTING
            self._status.started_at = time.time()

            # Initialize backend
            config = get_config()
            self._backend = get_embedding_backend()
            fingerprint = self._backend.fingerprint()

            # Open cache (handle corruption by deleting and retrying)
            try:
                self._cache = EmbeddingCache(fingerprint)
                self._status.embedding_count = self._cache.embedding_count
            except Exception as e:
                logger.warning(f"Cache database appears corrupt, recreating: {e}")
                import os
                db_path = self._cache._db_path if self._cache else None
                if db_path is None:
                    from .cache import _get_cache_db_path
                    db_path = _get_cache_db_path(fingerprint)
                # Remove corrupt file and WAL/SHM companions
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(str(db_path) + suffix)
                    except OSError:
                        pass
                self._cache = EmbeddingCache(fingerprint)
                self._status.embedding_count = 0

            # Restore last_completed_at from prior run
            stored = self._cache.get_meta("last_completed_at")
            if stored:
                try:
                    self._status.last_completed_at = float(stored)
                except (ValueError, TypeError):
                    pass

            # Check for force reindex
            if self._reindex_event.is_set():
                self._reindex_event.clear()
                self._cache.clear_session_state()
                logger.info("Force reindex: session state cleared")

            # Run initial indexing
            self._index_pass(config)

            # Release transient memory from the indexing pass
            _release_memory()

            # Mark initial pass as completed. Persist to SQLite BEFORE flipping
            # state to IDLE: a consumer that observes IDLE and then reads the
            # persisted last_completed_at must never see a stale/absent value.
            # (State-before-persist is a happens-before race — observed as a
            # flaky test on slower CI runners.)
            self._status.completed_at = time.time()
            self._status.last_completed_at = self._status.completed_at
            self._status.embedding_count = self._cache.embedding_count if self._cache else 0
            if self._cache:
                self._cache.set_meta(
                    "last_completed_at", str(self._status.last_completed_at)
                )
            self._status.state = IndexerState.IDLE

            # Periodic rescan loop
            rescan_interval = config.indexing.rescan_interval_minutes * 60
            while not self._stop_event.is_set() and rescan_interval > 0:
                # Wait for rescan interval, or until triggered by reindex/rescan
                # Use a short poll loop so we can check both events
                wait_start = time.time()
                while not self._stop_event.is_set():
                    elapsed_wait = time.time() - wait_start
                    if elapsed_wait >= rescan_interval:
                        break
                    if self._reindex_event.is_set() or self._rescan_event.is_set():
                        break
                    time.sleep(1)

                if self._stop_event.is_set():
                    break

                # Check for force reindex (clears state)
                if self._reindex_event.is_set():
                    self._reindex_event.clear()
                    self._cache.clear_session_state()
                    logger.info("Force reindex triggered during rescan wait")

                # Clear rescan event if set
                self._rescan_event.clear()

                # Re-read config in case it changed
                config = get_config()
                rescan_interval = config.indexing.rescan_interval_minutes * 60

                # Check if embedding backend config changed (model, dimensions, etc.)
                new_backend = get_embedding_backend()
                new_fingerprint = new_backend.fingerprint()
                if new_fingerprint != fingerprint:
                    logger.info(
                        f"Embedding config changed: {fingerprint} → {new_fingerprint}. "
                        f"Switching to new cache database."
                    )
                    self._backend = new_backend
                    fingerprint = new_fingerprint
                    self._cache = EmbeddingCache(fingerprint)
                    self._status.embedding_count = self._cache.embedding_count

                    # Invalidate the search index so it reloads from the new cache
                    from .search import invalidate_search_index
                    invalidate_search_index()

                    # Restore last_completed_at from the new cache (if any)
                    stored = self._cache.get_meta("last_completed_at")
                    if stored:
                        try:
                            self._status.last_completed_at = float(stored)
                        except (ValueError, TypeError):
                            self._status.last_completed_at = None
                    else:
                        self._status.last_completed_at = None

                # Reset status for new scan
                self._status.state = IndexerState.DISCOVERING
                self._status.sessions_processed = 0
                self._status.sessions_unchanged = 0
                self._status.messages_embedded = 0
                self._status.messages_cached = 0
                self._status.messages_skipped = 0
                self._status.errors = 0
                self._status.started_at = time.time()
                self._status.completed_at = None

                # Run rescan
                self._index_pass(config)

                # Release transient memory from the rescan pass
                _release_memory()

                # Mark this pass as completed. Persist BEFORE flipping to IDLE
                # (see the initial-pass note) so an observer of IDLE always
                # sees the persisted last_completed_at.
                self._status.completed_at = time.time()
                self._status.last_completed_at = self._status.completed_at
                self._status.embedding_count = self._cache.embedding_count if self._cache else 0
                if self._cache:
                    self._cache.set_meta(
                        "last_completed_at", str(self._status.last_completed_at)
                    )
                self._status.state = IndexerState.IDLE

        except Exception as e:
            self._status.state = IndexerState.ERROR
            self._status.last_error = str(e)
            logger.error(f"Background indexer error: {e}", exc_info=True)
        finally:
            self._status.completed_at = time.time()
            if self._status.state not in (IndexerState.ERROR,):
                self._status.last_completed_at = self._status.completed_at
                # Persist BEFORE flipping to IDLE (see the initial-pass note).
                if self._cache:
                    self._cache.set_meta(
                        "last_completed_at", str(self._status.last_completed_at)
                    )
                self._status.state = IndexerState.IDLE
            self._status.embedding_count = self._cache.embedding_count if self._cache else 0

    def _index_pass(self, config):
        """Discover and index sessions in a single pass."""
        self._status.state = IndexerState.DISCOVERING

        # Discover sessions
        all_sessions = list_all_sessions()
        memory_limit = get_memory_limit()
        selected, excluded = select_sessions_within_limit(all_sessions, memory_limit)

        self._status.sessions_total = len(selected)

        if not selected:
            return

        self._status.state = IndexerState.INDEXING
        batch_size = config.embedding.batch_size
        throttle_ms = config.indexing.throttle_ms

        t0 = time.time()

        for session_idx, session in enumerate(selected):
            if self._stop_event.is_set():
                break

            # Check for pending reindex request
            if self._reindex_event.is_set():
                self._reindex_event.clear()
                self._cache.clear_session_state()
                # Restart from beginning
                self._status.sessions_processed = 0
                self._status.sessions_unchanged = 0
                continue

            current_mtime = self._get_session_mtime(session)
            stored_mtime = self._cache.get_session_mtime(session.session_id)

            if stored_mtime is not None and current_mtime == stored_mtime:
                self._status.sessions_unchanged += 1
                continue

            self._status.current_session = session.session_id

            # Load messages
            try:
                messages = load_session_messages(session)
            except Exception as e:
                self._status.errors += 1
                self._status.last_error = f"Load error ({session.session_id}): {e}"
                continue

            if not messages:
                self._cache.update_session_state(session.session_id, current_mtime)
                self._status.sessions_processed += 1
                continue

            # Hash and embed
            texts_to_embed = []
            hashes_to_embed = []
            message_metadata = []

            for msg in messages:
                text_hash = hashlib.md5(msg.searchable_text.encode()).hexdigest()
                message_metadata.append((
                    msg.uuid, msg.session_id, msg.workspace,
                    msg.timestamp.timestamp(), msg.role, msg.searchable_text,
                    msg.message_index, msg.source.value, text_hash,
                    msg.content_tier.value, msg.tool_name,
                ))
                if not self._cache.has_embedding(text_hash):
                    texts_to_embed.append(msg.searchable_text)
                    hashes_to_embed.append(text_hash)
                else:
                    self._status.messages_cached += 1

            # Store metadata
            self._cache.delete_session_messages(session.session_id)
            self._cache.put_messages_batch(message_metadata)

            # Embed uncached messages
            if texts_to_embed:
                for batch_start in range(0, len(texts_to_embed), batch_size):
                    if self._stop_event.is_set():
                        break

                    batch_end = min(batch_start + batch_size, len(texts_to_embed))
                    batch_texts = texts_to_embed[batch_start:batch_end]
                    batch_hashes = hashes_to_embed[batch_start:batch_end]

                    try:
                        batch_embeddings = self._backend.encode(batch_texts)
                        items = [(h, batch_embeddings[i]) for i, h in enumerate(batch_hashes)]
                        self._cache.put_embeddings_batch(items)
                        self._status.messages_embedded += len(batch_texts)
                    except Exception as e:
                        # Single-message fallback
                        for text, h in zip(batch_texts, batch_hashes):
                            try:
                                emb = self._backend.encode([text])
                                self._cache.put_embedding(h, emb[0])
                                self._status.messages_embedded += 1
                            except Exception:
                                self._status.messages_skipped += 1
                                self._status.errors += 1

                    # Update rate
                    elapsed = time.time() - t0
                    if elapsed > 0 and self._status.messages_embedded > 0:
                        self._status.rate = self._status.messages_embedded / elapsed

                    # Throttle if configured
                    if throttle_ms > 0:
                        time.sleep(throttle_ms / 1000.0)

            self._cache.update_session_state(session.session_id, current_mtime)
            self._status.sessions_processed += 1
            self._status.embedding_count = self._cache.embedding_count

    @staticmethod
    def _get_session_mtime(session) -> float:
        if session.modified:
            return session.modified.timestamp()
        if session.created:
            return session.created.timestamp()
        return 0.0


# Global singleton
_background_indexer: BackgroundIndexer | None = None


def get_background_indexer() -> BackgroundIndexer:
    """Get or create the global background indexer."""
    global _background_indexer
    if _background_indexer is None:
        _background_indexer = BackgroundIndexer()
    return _background_indexer
