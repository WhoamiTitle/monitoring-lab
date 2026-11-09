"""Учебные метрики Nginx и cgroup текущего контейнера, без зависимостей."""

from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time


LOG_PATH = Path("/var/log/nginx/monitoring-metrics.log")
CGROUP_ROOT = Path("/sys/fs/cgroup")
ROUTES = {"/": "root", "/__demo/slow": "slow", "/__demo/error": "error"}
ROUTE_NAMES = ("root", "slow", "error", "other")
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", "other")
BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


class RequestMetrics:
    """Инкрементально читает завершённые строки Nginx access log."""

    def __init__(self, log_path=LOG_PATH):
        self.log_path = Path(log_path)
        self.offset = 0
        self.identity = None
        self.count = defaultdict(int)
        self.duration_sum = defaultdict(float)
        self.buckets = defaultdict(lambda: [0] * len(BUCKETS))
        self.read_errors = 0
        self.lock = threading.Lock()

    def _observe(self, line):
        try:
            status_text, duration_text, uri = line.decode("utf-8").rstrip("\n").split(" ", 2)
            status = int(status_text)
            duration = float(duration_text)
            if not 100 <= status <= 599 or not 0 <= duration < float("inf"):
                return
        except (ValueError, UnicodeDecodeError):
            return
        key = (ROUTES.get(uri, "other"), f"{status // 100}xx")
        self.count[key] += 1
        self.duration_sum[key] += duration
        for index, bound in enumerate(BUCKETS):
            if duration <= bound:
                self.buckets[key][index] += 1

    def _read_new_lines(self):
        try:
            with self.log_path.open("rb") as log:
                stat = self.log_path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.offset:
                    self.identity = identity
                    self.offset = 0
                log.seek(self.offset)
                while True:
                    line = log.readline()
                    # Незавершённую строку дочитаем при следующем опросе.
                    if not line or not line.endswith(b"\n"):
                        break
                    self._observe(line)
                    self.offset = log.tell()
        except OSError as exc:
            self.read_errors += 1
            print(f"Cannot read Nginx metrics log: {exc}", file=sys.stderr, flush=True)

    def render(self):
        with self.lock:
            self._read_new_lines()
            lines = [
                "# HELP lab_http_requests_total Completed Nginx requests.",
                "# TYPE lab_http_requests_total counter",
            ]
            for route in ROUTE_NAMES:
                for status_class in STATUS_CLASSES:
                    key = (route, status_class)
                    labels = f'route="{route}",status_class="{status_class}"'
                    lines.append(f"lab_http_requests_total{{{labels}}} {self.count[key]}")
            lines += [
                "# HELP lab_http_request_duration_seconds Nginx request durations in seconds.",
                "# TYPE lab_http_request_duration_seconds histogram",
            ]
            for route in ROUTE_NAMES:
                for status_class in STATUS_CLASSES:
                    key = (route, status_class)
                    labels = f'route="{route}",status_class="{status_class}"'
                    for bound, value in zip(BUCKETS, self.buckets[key]):
                        lines.append(f'lab_http_request_duration_seconds_bucket{{{labels},le="{bound:g}"}} {value}')
                    lines.append(f'lab_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {self.count[key]}')
                    lines.append(f"lab_http_request_duration_seconds_sum{{{labels}}} {self.duration_sum[key]:.6f}")
                    lines.append(f"lab_http_request_duration_seconds_count{{{labels}}} {self.count[key]}")
            lines += [
                "# HELP lab_http_log_read_errors_total Errors reading the Nginx metrics access log.",
                "# TYPE lab_http_log_read_errors_total counter",
                f"lab_http_log_read_errors_total {self.read_errors}",
            ]
            return "\n".join(lines) + "\n"


def _read_number(path):
    return int(path.read_text().strip())


def _first_existing(root, names):
    for name in names:
        path = root / name
        if path.exists():
            return path
    raise FileNotFoundError(", ".join(names))


def cgroup_metrics(root=CGROUP_ROOT):
    """Читает значения v2 или v1; недоступные показатели не подменяются нулём."""
    root = Path(root)
    values = {}
    if (root / "cgroup.controllers").exists():
        try:
            cpu_stat = dict(line.split() for line in (root / "cpu.stat").read_text().splitlines())
            quota, period = (root / "cpu.max").read_text().split()
            if quota != "max" and int(quota) > 0 and int(period) > 0:
                values["lab_container_cpu_usage_seconds_total"] = int(cpu_stat["usage_usec"]) / 1_000_000
                values["lab_container_cpu_limit_cores"] = int(quota) / int(period)
        except (OSError, ValueError, KeyError):
            pass
        try:
            memory_limit = (root / "memory.max").read_text().strip()
            if memory_limit != "max" and int(memory_limit) > 0:
                values["lab_container_memory_usage_bytes"] = _read_number(root / "memory.current")
                values["lab_container_memory_limit_bytes"] = int(memory_limit)
        except (OSError, ValueError):
            pass
    else:
        try:
            usage = _first_existing(root, ("cpuacct/cpuacct.usage", "cpu,cpuacct/cpuacct.usage"))
            quota = _first_existing(root, ("cpu/cpu.cfs_quota_us", "cpu,cpuacct/cpu.cfs_quota_us"))
            period = _first_existing(root, ("cpu/cpu.cfs_period_us", "cpu,cpuacct/cpu.cfs_period_us"))
            quota_value, period_value = _read_number(quota), _read_number(period)
            if quota_value > 0 and period_value > 0:
                values["lab_container_cpu_usage_seconds_total"] = _read_number(usage) / 1_000_000_000
                values["lab_container_cpu_limit_cores"] = quota_value / period_value
        except (OSError, ValueError):
            pass
        try:
            limit = _read_number(root / "memory/memory.limit_in_bytes")
            if 0 < limit < 2**60:
                values["lab_container_memory_usage_bytes"] = _read_number(root / "memory/memory.usage_in_bytes")
                values["lab_container_memory_limit_bytes"] = limit
        except (OSError, ValueError):
            pass

    definitions = {
        "lab_container_cpu_usage_seconds_total": ("counter", "CPU time used by the container in seconds."),
        "lab_container_cpu_limit_cores": ("gauge", "Container CPU quota expressed in cores."),
        "lab_container_memory_usage_bytes": ("gauge", "Current container cgroup memory usage in bytes."),
        "lab_container_memory_limit_bytes": ("gauge", "Container memory limit in bytes."),
    }
    lines = []
    for name, (metric_type, description) in definitions.items():
        lines.extend((f"# HELP {name} {description}", f"# TYPE {name} {metric_type}"))
        if name in values:
            lines.append(f"{name} {values[name]}")
    for resource in ("cpu", "memory"):
        name = f"lab_container_{resource}_stats_available"
        present = int(f"lab_container_{resource}_limit_" + ("cores" if resource == "cpu" else "bytes") in values)
        lines.extend((f"# HELP {name} Whether the cgroup resource and its limit are readable.",
                      f"# TYPE {name} gauge", f"{name} {present}"))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    metrics = RequestMetrics()

    def do_GET(self):
        if self.path == "/metrics":
            content = (self.metrics.render() + cgroup_metrics()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/slow":
            time.sleep(0.8)
            content = b"Simulated slow response\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        # Опросы /metrics не должны заполнять журнал контейнера.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9117), Handler).serve_forever()
