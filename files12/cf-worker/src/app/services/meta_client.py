"""
Sending WhatsApp replies via Meta's Graph API
(POST https://graph.facebook.com/{version}/{phone_number_id}/messages,
Bearer auth with the store's own access_token).
"""

import logging

import httpx

logger = logging.getLogger("boltane.meta_client")

GRAPH_API_VERSION = "v23.0"
REQUEST_TIMEOUT_SECONDS = 20.0


async def send_whatsapp(phone_number_id: str, access_token: str, to: str, message: str) -> None:
    """
    Sends a WhatsApp text message via Meta's Graph API.

    Never raises for ordinary send failures (network errors, an
    expired/invalid access_token, a Meta-side error) — those are
    logged and swallowed here, so a failed send can never crash the
    background task or affect any other in-flight conversation. Only
    a genuinely unexpected bug would propagate past this function.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("Meta send failed (request error) phone_number_id=%s to=%s: %s",
                        phone_number_id, to, exc)
        return

    if response.status_code != 200:
        logger.warning(
            "Meta send failed (status=%d) phone_number_id=%s to=%s: %s",
            response.status_code, phone_number_id, to, response.text[:300],
        )
        return

    logger.info("Meta send succeeded phone_number_id=%s to=%s", phone_number_id, to)
