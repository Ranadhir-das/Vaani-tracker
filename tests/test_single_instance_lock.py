"""Tests for Windows single-instance protection and mutual exclusion."""

import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.app import SingleInstanceLock


class TestSingleInstanceLock(unittest.TestCase):
    """Verifies OS-level mutual exclusion, duplicate prevention, and clean reacquisition."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.temp_dir.name) / "test_tracker.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_single_instance_lock_rejects_second_acquisition(self):
        """Scenario 10: Single-instance lock rejects second acquisition."""
        lock1 = SingleInstanceLock(str(self.lock_file))
        lock2 = SingleInstanceLock(str(self.lock_file))

        # First acquisition must succeed
        self.assertTrue(lock1.acquire(), "First acquisition should succeed")

        # Second acquisition for the same target must fail
        self.assertFalse(lock2.acquire(), "Second acquisition must be rejected")

        # Releasing lock2 (which didn't acquire) must not affect lock1
        lock2.release()
        self.assertFalse(lock2.acquire(), "Second acquisition still rejected while lock1 is held")

        # Clean release of lock1
        lock1.release()

    def test_lock_reacquired_after_clean_release(self):
        """Scenario 11: Lock reacquired after clean release."""
        lock1 = SingleInstanceLock(str(self.lock_file))
        lock2 = SingleInstanceLock(str(self.lock_file))

        self.assertTrue(lock1.acquire())
        self.assertFalse(lock2.acquire())

        # Release first instance
        lock1.release()

        # Second instance can now acquire cleanly
        self.assertTrue(lock2.acquire(), "Second instance must acquire after first releases")
        lock2.release()

    def test_single_instance_lock_release_from_separate_thread(self):
        """Verifies that release() invoked from a different thread cleanly releases ownership."""
        import threading

        lock1 = SingleInstanceLock(str(self.lock_file))
        lock2 = SingleInstanceLock(str(self.lock_file))

        # Acquire on this thread
        self.assertTrue(lock1.acquire(), "Main thread should acquire lock")
        self.assertFalse(lock2.acquire(), "Second instance should be rejected")

        # Call release() on a different worker thread (simulating tray quit or signal handler)
        release_error = []

        def worker_release():
            try:
                lock1.release()
            except Exception as e:
                release_error.append(e)

        t = threading.Thread(target=worker_release)
        t.start()
        t.join(timeout=2.0)

        self.assertEqual(len(release_error), 0, f"Cross-thread release raised error: {release_error}")

        # Now lock2 can acquire cleanly because the Win32 mutex was released on its owning thread
        self.assertTrue(lock2.acquire(), "Second instance must acquire after cross-thread release")
        lock2.release()

    def test_non_owner_release_does_not_destroy_active_lock_file(self):
        """Verifies that calling release() on an unacquired lock does not delete the valid lock file."""
        import threading

        lock1 = SingleInstanceLock(str(self.lock_file))
        lock2 = SingleInstanceLock(str(self.lock_file))

        self.assertTrue(lock1.acquire())
        self.assertTrue(os.path.exists(self.lock_file), "Lock file must exist")

        with open(self.lock_file, "r") as f:
            owner_pid = f.read().strip()
        self.assertEqual(owner_pid, str(os.getpid()))

        # Second lock fails to acquire
        self.assertFalse(lock2.acquire())

        # Releasing lock2 from any thread must not delete the lock file created by lock1
        lock2.release()
        self.assertTrue(os.path.exists(self.lock_file), "Lock file must still exist after unacquired release")

        with open(self.lock_file, "r") as f:
            self.assertEqual(f.read().strip(), owner_pid)

        # Finally release lock1
        lock1.release()
        self.assertFalse(os.path.exists(self.lock_file), "Lock file should be removed after owner release")

    def test_repeated_release_is_safe_and_idempotent(self):
        """Verifies calling release() multiple times is safe, idempotent, and returns True."""
        lock = SingleInstanceLock(str(self.lock_file))
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.release())
        self.assertTrue(lock.release(), "Repeated release should return True")
        self.assertTrue(lock.release(), "Subsequent release should return True")

        # Can acquire again cleanly
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.release())

    def test_release_failure_reporting(self):
        """Verifies that Win32 ReleaseMutex failure is captured and reported."""
        import ctypes
        from unittest.mock import patch

        lock = SingleInstanceLock(str(self.lock_file))
        self.assertTrue(lock.acquire())

        # Simulate ReleaseMutex failing with FALSE (0)
        with patch.object(ctypes.windll.kernel32, "ReleaseMutex", return_value=0):
            with patch.object(ctypes.windll.kernel32, "GetLastError", return_value=6):  # ERROR_INVALID_HANDLE
                res = lock.release(timeout=1.0)
                self.assertFalse(res, "release() must return False when ReleaseMutex fails")
                # When release fails, ownership state and PID file must NOT be cleared
                self.assertTrue(lock._acquired, "Ownership flag must remain True on release failure")
                self.assertTrue(os.path.exists(self.lock_file), "Lock file must be preserved on release failure")
                self.assertEqual(lock._release_error, 6)

        # Clean up test resources manually since simulated API failure left unconfirmed state
        lock._acquired = False
        if os.path.exists(self.lock_file):
            os.remove(self.lock_file)

    def test_owner_thread_still_alive_after_timeout(self):
        """Verifies that release returns False and keeps tracking if owner thread does not terminate."""
        import threading

        lock = SingleInstanceLock(str(self.lock_file))
        self.assertTrue(lock.acquire())

        # Replace owner thread with a hung mock thread that ignores the release event
        hung_event = threading.Event()
        def hung_worker():
            hung_event.wait(timeout=2.0)

        hung_thread = threading.Thread(target=hung_worker, daemon=True)
        hung_thread.start()
        original_thread = lock._owner_thread
        lock._owner_thread = hung_thread

        try:
            res = lock.release(timeout=0.05)
            self.assertFalse(res, "release() must return False when thread fails to terminate within timeout")
            self.assertTrue(lock._acquired, "Must remain acquired if owner thread is still alive")
            self.assertIsNotNone(lock._owner_thread, "Owner thread reference must not be lost")
        finally:
            hung_event.set()
            hung_thread.join(timeout=1.0)
            lock._owner_thread = original_thread
            lock.release()

    def test_acquisition_readiness_timeout(self):
        """Verifies acquire() handles readiness timeout without losing track of owner thread."""
        from unittest.mock import patch
        import threading

        lock = SingleInstanceLock(str(self.lock_file))

        # Mock owner worker never setting _ready_event
        ready_never_called = threading.Event()
        def slow_worker():
            ready_never_called.wait(timeout=1.0)

        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = threading.Thread(target=slow_worker, daemon=True)
            mock_thread_cls.return_value = mock_thread
            acquired = lock.acquire(timeout=0.05)
            self.assertFalse(acquired, "acquire() must return False on readiness timeout")
            ready_never_called.set()
            mock_thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()

