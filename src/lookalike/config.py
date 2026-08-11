from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DATA_FILE = DATA_DIR / "Bank_Marketing_Dataset.csv"
OUTPUT_DIR = PROJECT_ROOT / "output"
# P1 durable store for processes / versions / candidate CSVs / score tables
STORE_DIR = PROJECT_ROOT / "data" / "store"

ID_COL = "ClientID"
TARGET_COL = "TermDepositSubscribed"
LABEL_COL = "label"
DROP_COLS = ["ResponsePropensity"]

DEFAULT_IV_LIMIT = 0.02
DEFAULT_MISSING_LIMIT = 0.95
DEFAULT_IDENTICAL_LIMIT = 0.95
