"""Tests for outbox batch size limits."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.jobs.process_outbox import (  # noqa: E402
    _chunk_jobs,
    _get_provider_max_batch_size,
    _group_batchable_jobs,
)


class TestBatchSizeLimits(unittest.TestCase):
    """Test batch size limit functionality."""

    def test_get_provider_max_batch_size_trakt(self):
        """Test that Trakt batch size is retrieved correctly."""
        size = _get_provider_max_batch_size("trakt")
        self.assertGreater(size, 0)
        # Should be default 750 or configured value
        self.assertGreaterEqual(size, 750)

    def test_get_provider_max_batch_size_simkl(self):
        """Test that SIMKL batch size is retrieved correctly."""
        size = _get_provider_max_batch_size("simkl")
        self.assertGreater(size, 0)
        # Should be default 750 or configured value
        self.assertGreaterEqual(size, 750)

    def test_get_provider_max_batch_size_unknown(self):
        """Test that unknown provider returns default batch size."""
        size = _get_provider_max_batch_size("unknown_provider")
        self.assertEqual(size, 1000)  # Default fallback

    def test_chunk_jobs_empty(self):
        """Test chunking empty list."""
        chunks = _chunk_jobs([], 100)
        self.assertEqual(chunks, [])

    def test_chunk_jobs_single_chunk(self):
        """Test chunking when all items fit in one chunk."""
        jobs = [_create_mock_job(i) for i in range(50)]
        chunks = _chunk_jobs(jobs, 100)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 50)

    def test_chunk_jobs_exact_multiple(self):
        """Test chunking when jobs divide evenly into chunks."""
        jobs = [_create_mock_job(i) for i in range(2000)]
        chunks = _chunk_jobs(jobs, 1000)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 1000)
        self.assertEqual(len(chunks[1]), 1000)

    def test_chunk_jobs_with_remainder(self):
        """Test chunking with remainder items."""
        jobs = [_create_mock_job(i) for i in range(2550)]
        chunks = _chunk_jobs(jobs, 1000)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 1000)
        self.assertEqual(len(chunks[1]), 1000)
        self.assertEqual(len(chunks[2]), 550)

    def test_chunk_jobs_large_dataset(self):
        """Test chunking with large dataset simulating 50k episodes."""
        jobs = [_create_mock_job(i) for i in range(50000)]

        # Test with SIMKL batch size (750)
        chunks = _chunk_jobs(jobs, 750)
        self.assertEqual(len(chunks), 67)
        for chunk in chunks[:-1]:
            self.assertEqual(len(chunk), 750)
        self.assertEqual(len(chunks[-1]), 500)

        # Test with Trakt batch size (750)
        chunks = _chunk_jobs(jobs, 750)
        self.assertEqual(len(chunks), 67)
        for chunk in chunks[:-1]:
            self.assertEqual(len(chunk), 750)
        self.assertEqual(len(chunks[-1]), 500)

    def test_chunk_jobs_zero_size(self):
        """Test chunking with zero chunk size uses default."""
        jobs = [_create_mock_job(i) for i in range(1500)]
        chunks = _chunk_jobs(jobs, 0)
        # Should use default of 1000
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 1000)
        self.assertEqual(len(chunks[1]), 500)

    def test_chunk_jobs_negative_size(self):
        """Test chunking with negative chunk size uses default."""
        jobs = [_create_mock_job(i) for i in range(1500)]
        chunks = _chunk_jobs(jobs, -1)
        # Should use default of 1000
        self.assertEqual(len(chunks), 2)

    def test_group_batchable_jobs_splits_large_batches(self):
        """Test that large batches are split according to provider limits."""
        # Create 5555 jobs for Trakt (should be split into chunks of 750 max)
        trakt_jobs = []
        for i in range(5555):
            job = _create_mock_job(i)
            job.user_id = "user1"
            job.target_provider = "trakt"
            job.job_type = "push_watched"
            trakt_jobs.append(job)

        batch_groups, remaining = _group_batchable_jobs(trakt_jobs)

        # Should have 8 batches (750 * 7 + 305)
        self.assertEqual(len(batch_groups), 8)
        for group in batch_groups[:-1]:
            self.assertEqual(len(group), 750)
        self.assertEqual(len(batch_groups[-1]), 305)
        self.assertEqual(len(remaining), 0)

    def test_group_batchable_jobs_splits_simkl_batches(self):
        """Test that SIMKL batches are split according to 750 item limit."""
        # Create 2550 jobs for SIMKL (should be split into 4 batches)
        simkl_jobs = []
        for i in range(2550):
            job = _create_mock_job(i)
            job.user_id = "user1"
            job.target_provider = "simkl"
            job.job_type = "push_watched"
            simkl_jobs.append(job)

        batch_groups, remaining = _group_batchable_jobs(simkl_jobs)

        # Should have 4 batches (750, 750, 750, 300)
        self.assertEqual(len(batch_groups), 4)
        self.assertEqual(len(batch_groups[0]), 750)
        self.assertEqual(len(batch_groups[1]), 750)
        self.assertEqual(len(batch_groups[2]), 750)
        self.assertEqual(len(batch_groups[3]), 300)
        self.assertEqual(len(remaining), 0)

    def test_group_batchable_jobs_single_item(self):
        """Test that single items are moved to remaining."""
        job = _create_mock_job(0)
        job.user_id = "user1"
        job.target_provider = "trakt"
        job.job_type = "push_watched"

        batch_groups, remaining = _group_batchable_jobs([job])

        # Single job should be in remaining, not batched
        self.assertEqual(len(batch_groups), 0)
        self.assertEqual(len(remaining), 1)

    def test_group_batchable_jobs_non_batchable_provider(self):
        """Test that non-batchable providers are in remaining."""
        jobs = []
        for i in range(10):
            job = _create_mock_job(i)
            job.user_id = "user1"
            job.target_provider = "letterboxd"  # Not batchable
            job.job_type = "push_watched"
            jobs.append(job)

        batch_groups, remaining = _group_batchable_jobs(jobs)

        self.assertEqual(len(batch_groups), 0)
        self.assertEqual(len(remaining), 10)

    def test_group_batchable_jobs_mixed_providers(self):
        """Test grouping with mixed providers and batch sizes."""
        jobs = []

        # 1500 Trakt jobs (should be in 2 batches)
        for i in range(1500):
            job = _create_mock_job(i)
            job.user_id = "user1"
            job.target_provider = "trakt"
            job.job_type = "push_watched"
            jobs.append(job)

        # 2100 SIMKL jobs (should be in 3 batches: 750, 750, 600)
        for i in range(2100):
            job = _create_mock_job(1500 + i)
            job.user_id = "user1"
            job.target_provider = "simkl"
            job.job_type = "push_watched"
            jobs.append(job)

        # 5 Letterboxd jobs (not batchable)
        for i in range(5):
            job = _create_mock_job(3600 + i)
            job.user_id = "user1"
            job.target_provider = "letterboxd"
            job.job_type = "push_watched"
            jobs.append(job)

        batch_groups, remaining = _group_batchable_jobs(jobs)

        # Trakt: 2 batches
        # SIMKL: 3 batches (750, 750, 600)
        # Letterboxd: all in remaining
        self.assertEqual(len(batch_groups), 5)  # 2 Trakt + 3 SIMKL
        self.assertEqual(len(remaining), 5)  # All Letterboxd


def _create_mock_job(idx: int):
    """Create a mock OutboxJob for testing."""
    job = MagicMock()
    job.id = f"job_{idx}"
    job.user_id = "test_user"
    job.target_provider = "trakt"
    job.job_type = "push_watched"
    job.created_at = datetime.now(timezone.utc)
    job.status = "pending"
    return job


if __name__ == "__main__":
    unittest.main()
