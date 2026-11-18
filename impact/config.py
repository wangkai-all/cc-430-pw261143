"""Runtime configuration, read from the environment on every call so tests
can relocate IMPACT_HOME between cases."""
import os


def _env(key, default):
    return os.environ.get(key, default)


def home():
    return os.path.abspath(_env("IMPACT_HOME", os.path.join(os.getcwd(), "data")))


def db_path():
    return os.path.join(home(), "impact.db")


def package_dir():
    return os.path.join(home(), "packages")


def bind_host():
    return _env("IMPACT_BIND", "127.0.0.1")


def ingest_port():
    return int(_env("IMPACT_INGEST_PORT", "8001"))


def worker_port():
    return int(_env("IMPACT_WORKER_PORT", "8002"))


def package_port():
    return int(_env("IMPACT_PACKAGE_PORT", "8003"))


def ingest_base():
    return _env("IMPACT_INGEST_BASE", "http://127.0.0.1:%d" % ingest_port())


def poll_interval():
    return float(_env("IMPACT_POLL_INTERVAL", "0.2"))


def build_step_delay():
    """Artificial per-step delay in the package builder, so cancel / fencing
    behaviour is observable in demos and tests."""
    return float(_env("IMPACT_BUILD_STEP_DELAY", "0"))


def build_workers():
    return int(_env("IMPACT_BUILD_WORKERS", "2"))


def ensure_dirs():
    os.makedirs(package_dir(), exist_ok=True)
