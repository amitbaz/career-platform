from __future__ import annotations

import requests

_DISPATCH_URL_TEMPLATE = "https://api.github.com/repos/{repo}/dispatches"


def trigger_repository_dispatch(
    repo: str,
    token: str,
    event_type: str,
    client_payload: dict,
    *,
    http=None,
) -> None:
    """Fire a GitHub repository_dispatch event, e.g. to trigger a workflow."""
    http = http or requests
    response = http.post(
        _DISPATCH_URL_TEMPLATE.format(repo=repo),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"event_type": event_type, "client_payload": client_payload},
    )
    response.raise_for_status()
