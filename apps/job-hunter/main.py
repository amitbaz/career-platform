from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from job_hunter.telegram_webhook import create_app

app = create_app()
