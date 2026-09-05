import os

from google_auth_oauthlib.flow import InstalledAppFlow

from job_hunter.gmail_models import GMAIL_READONLY_SCOPE


def main() -> None:
    client_config = {
        "installed": {
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=[GMAIL_READONLY_SCOPE])
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh token; revoke prior consent and retry")
    print(credentials.refresh_token)


if __name__ == "__main__":
    main()
