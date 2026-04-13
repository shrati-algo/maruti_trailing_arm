from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_BASE_DIR = PROJECT_ROOT / "data_base"
CONFIG_DIR = PROJECT_ROOT / "config"

LOG_DIR.mkdir(exist_ok=True)
DATA_BASE_DIR.mkdir(exist_ok=True)


def log_path(filename: str) -> str:
    return str(LOG_DIR / filename)


def project_path(*parts: str) -> str:
    return str(PROJECT_ROOT.joinpath(*parts))
