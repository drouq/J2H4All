"""Local-dev Telegram runner: long-polls getUpdates instead of a webhook
(no public HTTPS locally). Same handler as the webhook, so behavior is
identical when we later deploy and switch back to setWebhook.

    cd backend
    .venv\\Scripts\\python.exe -m app.telegram_poll
"""

import asyncio
import logging

import httpx

from .config import get_settings
from .telegram import handle_update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=70) as client:
        # A registered webhook blocks getUpdates; drop it while running locally.
        await client.post(f"{base}/deleteWebhook")
        logger.info("Polling for Telegram updates (Ctrl+C to stop)")
        offset: int | None = None
        while True:
            try:
                resp = await client.get(
                    f"{base}/getUpdates",
                    params={"timeout": 50, **({"offset": offset} if offset else {})},
                )
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    await handle_update(update)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Poll error (%s); retrying in 5s", exc)
                await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
