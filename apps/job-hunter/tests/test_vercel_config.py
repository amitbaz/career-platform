import json
from pathlib import Path


def test_vercel_config_declares_flask_root_entrypoint():
    config = json.loads(Path("vercel.json").read_text())

    assert config["framework"] == "flask"
    assert config["installCommand"] == "pip install -e '.[webhook]'"
    assert config["functions"]["main.py"]["maxDuration"] == 30
