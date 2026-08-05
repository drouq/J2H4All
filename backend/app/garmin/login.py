"""One-time interactive Garmin login.

Run in YOUR terminal (credentials never leave this machine):

    cd backend
    .venv\\Scripts\\python.exe -m app.garmin.login

Prompts for Garmin email/password (+ MFA code if enabled), then saves the
long-lived garth token blob to backend/.env as GARTH_TOKEN. Re-run whenever
the token expires (~1 year).
"""

import sys
from getpass import getpass
from pathlib import Path

import garth

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _write_env_var(name: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    print("J2H4All - Garmin Connect login (one-time; garth caches a ~1-year token)")
    email = input("Garmin email: ").strip()
    password = getpass("Garmin password: ")
    client = garth.Client()
    try:
        client.login(email, password)  # garth prompts for the MFA code itself if needed
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    token = client.dumps()
    _write_env_var("GARTH_TOKEN", token)
    try:
        name = client.profile["displayName"]
    except Exception:
        name = email
    print(f"Logged in as {name}. Token saved to {ENV_PATH} (GARTH_TOKEN).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
