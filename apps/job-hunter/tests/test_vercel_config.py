import json
from pathlib import Path


def test_vercel_config_declares_flask_root_entrypoint():
    config = json.loads(Path("vercel.json").read_text())

    assert config["framework"] == "flask"
    assert config["installCommand"] == "pip install -e '.[webhook]'"
    assert config["functions"]["main.py"]["maxDuration"] == 30


def test_vercel_config_skips_builds_unaffected_by_a_commit():
    config = json.loads(Path("vercel.json").read_text())

    # Without this, every pull request rebuilds this project even when only
    # unrelated files changed, which costs build minutes on a shared free tier.
    assert "../../scripts/vercel-ignore-build.sh" in config["ignoreCommand"]
    # Job Hunter opts production in as well: it is a Flask function installed
    # from this directory and importing nothing outside it, so the watched path
    # list is exhaustive rather than inferred.
    assert "--allow-production" in config["ignoreCommand"]
