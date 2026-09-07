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


def test_relay_vercel_config_watches_the_paths_it_is_built_from():
    config = json.loads(Path("../relay/vercel.json").read_text())

    command = config["ignoreCommand"]
    assert "../../scripts/vercel-ignore-build.sh" in command
    # Relay is built from more than its own directory: the workspace lockfile
    # and the shared schema both change what it produces.
    for watched in ("':/pnpm-lock.yaml'", "':/supabase'"):
        assert watched in command
