import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "web" / "exporter.py"
SPEC = importlib.util.spec_from_file_location("lab_exporter", MODULE_PATH)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ExporterTests(unittest.TestCase):
    def test_reads_completed_requests_once_and_builds_cumulative_histogram(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            path.write_text("200 0.800 /__demo/slow\n500 0.002 /__demo/error\n200 0.002 /")
            metrics = exporter.RequestMetrics(path)
            first = metrics.render()
            self.assertIn('lab_http_requests_total{route="slow",status_class="2xx"} 1', first)
            self.assertIn('lab_http_request_duration_seconds_bucket{route="slow",status_class="2xx",le="0.5"} 0', first)
            self.assertIn('lab_http_request_duration_seconds_bucket{route="slow",status_class="2xx",le="1"} 1', first)
            self.assertIn('lab_http_request_duration_seconds_bucket{route="slow",status_class="2xx",le="+Inf"} 1', first)
            self.assertIn('lab_http_requests_total{route="error",status_class="5xx"} 1', first)
            self.assertIn('lab_http_requests_total{route="root",status_class="2xx"} 0', first)
            with path.open("a") as log:
                log.write("\n")
            second = metrics.render()
            self.assertIn('lab_http_requests_total{route="root",status_class="2xx"} 1', second)
            self.assertIn('lab_http_requests_total{route="slow",status_class="2xx"} 1', second)
            self.assertEqual(second, metrics.render())

    def test_cgroup_v2_limits_and_unlimited_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in {"cgroup.controllers": "cpu memory", "cpu.stat": "usage_usec 2500000\n", "cpu.max": "100000 100000", "memory.current": "33554432", "memory.max": "134217728"}.items():
                (root / name).write_text(value)
            output = exporter.cgroup_metrics(root)
            self.assertIn("lab_container_cpu_usage_seconds_total 2.5", output)
            self.assertIn("lab_container_cpu_limit_cores 1.0", output)
            self.assertIn("lab_container_memory_usage_bytes 33554432", output)
            self.assertIn("lab_container_memory_limit_bytes 134217728", output)
            (root / "memory.max").write_text("max")
            output = exporter.cgroup_metrics(root)
            self.assertIn("lab_container_memory_stats_available 0", output)
            self.assertNotIn("\nlab_container_memory_limit_bytes ", output)

    def test_cgroup_v1_separate_cpu_and_memory_hierarchies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in {"cpuacct/cpuacct.usage": "1500000000", "cpu/cpu.cfs_quota_us": "50000", "cpu/cpu.cfs_period_us": "100000", "memory/memory.usage_in_bytes": "1024", "memory/memory.limit_in_bytes": "2048"}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            output = exporter.cgroup_metrics(root)
            self.assertIn("lab_container_cpu_usage_seconds_total 1.5", output)
            self.assertIn("lab_container_cpu_limit_cores 0.5", output)
            self.assertIn("lab_container_memory_limit_bytes 2048", output)


if __name__ == "__main__":
    unittest.main()
