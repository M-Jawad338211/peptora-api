import logging
import httpx

logger = logging.getLogger("peptora.push")
EXPO_PUSH_URL = "https://exp.host/push/send"


async def send_expo_push(token: str, title: str, body: str, data: dict | None = None) -> bool:
    payload = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "data": data or {},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                EXPO_PUSH_URL,
                json=payload,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
            )
            resp.raise_for_status()
            ticket = resp.json().get("data", [{}])[0]
            if ticket.get("status") == "error":
                logger.warning("expo_push_error token=%.20s msg=%s", token, ticket.get("message"))
                return False
            logger.info("expo_push_sent token=%.20s", token)
            return True
    except Exception as exc:
        logger.error("expo_push_failed token=%.20s error=%s", token, exc)
        return False
