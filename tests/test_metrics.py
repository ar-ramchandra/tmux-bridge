"""Metrics must never hold up the terminal event loop."""
import asyncio
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import server
import sysinfo


class MetricsResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_probe_does_not_block_terminal_input(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_probe(previous):
            started.set()
            release.wait(2)
            finished.set()
            return {}, None, {}, False
        with patch.object(server, '_sample_metrics', slow_probe):
            task = asyncio.create_task(server._collect_metrics())
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(started.is_set())
                # If the probe ran on the event loop, its two-second wait
                # would have expired before this coroutine could resume.
                self.assertFalse(task.done())
                self.assertFalse(release.is_set())
                tick = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(tick.set_result, 'input handled')
                self.assertEqual(await asyncio.wait_for(tick, .2), 'input handled')
                self.assertFalse(finished.is_set(), 'Probe blocked the input loop until it finished')
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_probe_is_retried(self):
        calls = []
        def probe(previous):
            calls.append(previous)
            if len(calls) == 1:
                raise OSError('transient process-table failure')
            return {}, None, {}, False
        with patch.object(server, '_sample_metrics', probe), patch.object(server, 'METRICS_INTERVAL', .01):
            task = asyncio.create_task(server._collect_metrics())
            try:
                for _ in range(100):
                    if len(calls) >= 2:
                        break
                    await asyncio.sleep(.005)
                self.assertGreaterEqual(len(calls), 2)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class MetricsSnapshotTests(unittest.TestCase):
    def test_sessions_share_one_scan_and_descendants_are_counted_once(self):
        def proc(pid, ppid, cpu, rss, created=1):
            return SimpleNamespace(info=dict(pid=pid, ppid=ppid, create_time=created,
                cpu_times=SimpleNamespace(user=cpu, system=0), memory_info=SimpleNamespace(rss=rss)))
        counters = SimpleNamespace(bytes_recv=100, bytes_sent=200, read_bytes=300, write_bytes=400)
        previous = dict(time=8, net=counters, disk=counters,
                        processes={(10, 1): 1, (11, 1): 2, (12, 1): 3, (20, 1): 99})
        # Pane 11 is also a descendant of pane 10: do not double-count it.
        table = [proc(10, 1, 2, 1024**2), proc(11, 10, 3, 2*1024**2),
                 proc(12, 11, 4, 3*1024**2), proc(20, 1, 100, 1024**2, created=2)]
        with patch.object(sysinfo, 'HAS_NVIDIA', False), \
             patch.object(server.time, 'monotonic', return_value=10), \
             patch.object(psutil, 'net_io_counters', return_value=counters), \
             patch.object(psutil, 'disk_io_counters', return_value=counters), \
             patch.object(psutil, 'cpu_percent', return_value=25), \
             patch.object(psutil, 'virtual_memory', return_value=SimpleNamespace(percent=30)), \
             patch.object(psutil, 'process_iter', return_value=table) as scan, \
             patch.object(server.subprocess, 'check_output', return_value='a\t10\na\t11\nb\t20\n') as tmux:
            state, samples, sessions, gpu = server._sample_metrics(previous)
        scan.assert_called_once()
        tmux.assert_called_once()
        self.assertEqual(sessions['a'], (150.0, 6.0))
        self.assertEqual(sessions['b'], (0.0, 1.0), 'PID reuse must not inherit CPU counters')
        self.assertEqual(samples['cpu'], 25)
        self.assertEqual(samples['net_rx'], 0)
        self.assertFalse(gpu)

    def test_temporarily_unavailable_tmux_keeps_session_history(self):
        counters = SimpleNamespace(bytes_recv=0, bytes_sent=0)
        previous = dict(time=1, net=counters, disk=None, processes={})
        with patch.object(sysinfo, 'HAS_NVIDIA', False), \
             patch.object(psutil, 'net_io_counters', return_value=counters), \
             patch.object(psutil, 'disk_io_counters', return_value=None), \
             patch.object(server.subprocess, 'check_output', side_effect=subprocess.TimeoutExpired('tmux', 2)):
            _, samples, sessions, _ = server._sample_metrics(previous)
        self.assertIsNone(sessions)
        self.assertEqual(samples['disk_read'], 0)


if __name__ == '__main__':
    unittest.main()
