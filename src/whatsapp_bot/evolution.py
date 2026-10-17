import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 10
_INITIAL_RETRY_DELAY_SECONDS = 0.5
_RETRYABLE_STATUSES = frozenset({429, 502, 503})


class EvolutionAPIError(Exception):
    """Raised when an Evolution API request fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Evolution API error {status_code}: {detail}")


class EvolutionClient:
    """Async HTTP client for Evolution API v2."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"apikey": self._api_key},
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "EvolutionClient not started. Call start() first."
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self.client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait = _INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
                    logger.warning(
                        "Evolution API %s %s network error: %s, retry %d/%d in %.1fs",
                        method, path, exc, attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise EvolutionAPIError(
                    0, f"Network error after {_MAX_RETRIES} retries: {exc}"
                ) from exc

            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                wait = _INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    "Evolution API %s %s returned %d, retry %d/%d in %.1fs",
                    method, path, response.status_code,
                    attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            if not response.is_success:
                try:
                    detail = response.text
                except Exception:
                    detail = f"HTTP {response.status_code}"
                raise EvolutionAPIError(response.status_code, detail)
            return response.json()

        # Should never reach here, but satisfy type checker
        raise EvolutionAPIError(0, "Retry loop exhausted")  # pragma: no cover

    async def create_instance(
        self,
        name: str,
        *,
        qrcode: bool = True,
        integration: str = "WHATSAPP-BAILEYS",
    ) -> dict:
        """Create a new WhatsApp instance.

        POST /instance/create
        """
        return await self._request(
            "POST",
            "/instance/create",
            json={
                "instanceName": name,
                "qrcode": qrcode,
                "integration": integration,
            },
        )

    async def connect_instance(self, name: str) -> dict:
        """Connect an instance and get QR code / pairing code.

        GET /instance/connect/{name}
        """
        return await self._request("GET", f"/instance/connect/{name}")

    async def connection_state(self, name: str) -> dict:
        """Get the connection state of an instance.

        GET /instance/connectionState/{name}
        """
        return await self._request(
            "GET", f"/instance/connectionState/{name}"
        )

    async def send_text(
        self,
        instance: str,
        number: str,
        text: str,
        *,
        link_preview: bool = False,
    ) -> dict:
        """Send a text message.

        POST /message/sendText/{instance}

        Set *link_preview* to ``True`` to generate a link preview for URLs
        in the message (extended text message).
        """
        payload: dict[str, object] = {
            "number": number,
            "text": text,
        }
        if link_preview:
            payload["linkPreview"] = True
        return await self._request(
            "POST",
            f"/message/sendText/{instance}",
            json=payload,
        )

    async def fetch_all_groups(self, instance: str) -> Any:
        """Fetch all groups for an instance.

        GET /group/fetchAllGroups/{instance}
        """
        return await self._request(
            "GET",
            f"/group/fetchAllGroups/{instance}",
            params={"getParticipants": "false"},
        )

    async def find_group_info(self, instance: str, group_jid: str) -> dict:
        """Get info for a specific group.

        GET /group/findGroupInfos/{instance}
        """
        return await self._request(
            "GET",
            f"/group/findGroupInfos/{instance}",
            params={"groupJid": group_jid},
        )

    async def send_buttons(
        self,
        instance: str,
        number: str,
        *,
        title: str,
        description: str,
        footer: str = "",
        buttons: list[dict[str, str]],
    ) -> dict:
        """Send a button message.

        POST /message/sendButtons/{instance}

        *buttons* should be a list of dicts like:
        ``[{"type": "reply", "displayText": "...", "id": "..."}]``
        """
        payload: dict[str, object] = {
            "number": number,
            "title": title,
            "description": description,
            "buttons": buttons,
        }
        if footer:
            payload["footer"] = footer
        return await self._request(
            "POST",
            f"/message/sendButtons/{instance}",
            json=payload,
        )

    async def send_media(
        self,
        instance: str,
        number: str,
        media_type: str,
        url: str,
        caption: str = "",
        file_name: str = "",
    ) -> dict:
        """Send a media message (image, video, document, audio).

        POST /message/sendMedia/{instance}

        *media_type* must be one of: ``image``, ``video``, ``audio``, ``document``.
        Use *file_name* to set the displayed filename (useful for documents).

        For security, *url* must use ``data:`` (base64) or ``https://`` scheme.
        """
        # M7: Prevent SSRF via user-controlled URLs
        if not (url.startswith("data:") or url.startswith("https://")):
            raise ValueError(
                f"send_media url must use data: or https:// scheme, got {url[:30]!r}..."
            )
        payload: dict[str, object] = {
            "number": number,
            "mediatype": media_type,
            "media": url,
            "caption": caption,
        }
        if file_name:
            payload["fileName"] = file_name
        return await self._request(
            "POST",
            f"/message/sendMedia/{instance}",
            json=payload,
        )

    async def send_list(
        self,
        instance: str,
        number: str,
        *,
        title: str,
        description: str,
        button_text: str,
        sections: list[dict],
        footer: str = "",
    ) -> dict:
        """Send a list message.

        POST /message/sendList/{instance}

        *sections* should be a list like::

            [{"title": "...", "rows": [{"title": "...", "description": "...", "rowId": "..."}]}]
        """
        payload: dict[str, object] = {
            "number": number,
            "title": title,
            "description": description,
            "buttonText": button_text,
            "sections": sections,
        }
        if footer:
            payload["footerText"] = footer
        return await self._request(
            "POST",
            f"/message/sendList/{instance}",
            json=payload,
        )

    async def send_poll(
        self,
        instance: str,
        number: str,
        *,
        name: str,
        values: list[str],
        selectable_count: int = 1,
    ) -> dict:
        """Send a poll message.

        POST /message/sendPoll/{instance}
        """
        return await self._request(
            "POST",
            f"/message/sendPoll/{instance}",
            json={
                "number": number,
                "name": name,
                "values": values,
                "selectableCount": selectable_count,
            },
        )

    async def delete_message(
        self, instance: str, remote_jid: str, message_id: str
    ) -> dict:
        """Delete one of this bot's messages for everyone in its chat."""
        return await self._request(
            "DELETE",
            f"/chat/deleteMessageForEveryone/{instance}",
            json={"id": message_id, "fromMe": True, "remoteJid": remote_jid},
        )

    async def send_location(
        self,
        instance: str,
        number: str,
        *,
        latitude: float,
        longitude: float,
        name: str = "",
        address: str = "",
    ) -> dict:
        """Send a location message.

        POST /message/sendLocation/{instance}
        """
        payload: dict[str, object] = {
            "number": number,
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            payload["name"] = name
        if address:
            payload["address"] = address
        return await self._request(
            "POST",
            f"/message/sendLocation/{instance}",
            json=payload,
        )

    async def send_contact(
        self,
        instance: str,
        number: str,
        *,
        contacts: list[dict],
    ) -> dict:
        """Send a contact card message.

        POST /message/sendContact/{instance}

        *contacts* should be a list like::

            [{"fullName": "...", "phoneNumber": "..."}]
        """
        return await self._request(
            "POST",
            f"/message/sendContact/{instance}",
            json={
                "number": number,
                "contact": contacts,
            },
        )

    async def send_reaction(
        self,
        instance: str,
        *,
        key: dict,
        reaction: str,
    ) -> dict:
        """Send a reaction to a message.

        POST /message/sendReaction/{instance}
        """
        return await self._request(
            "POST",
            f"/message/sendReaction/{instance}",
            json={
                "key": key,
                "reaction": reaction,
            },
        )

    async def send_sticker(
        self,
        instance: str,
        number: str,
        sticker: str,
    ) -> dict:
        """Send a sticker message.

        POST /message/sendSticker/{instance}

        *sticker* should be a URL or base64 string. The API auto-converts
        to WebP format. If the URL contains ``.gif``, it is sent as animated.
        """
        return await self._request(
            "POST",
            f"/message/sendSticker/{instance}",
            json={
                "number": number,
                "sticker": sticker,
            },
        )

    async def send_whatsapp_audio(
        self,
        instance: str,
        number: str,
        audio: str,
        *,
        encoding: bool = True,
    ) -> dict:
        """Send a voice note (push-to-talk audio).

        POST /message/sendWhatsAppAudio/{instance}

        *audio* should be a URL or base64 string. The API auto-converts
        to OGG Opus format and sends as ``ptt: true``.
        Set *encoding* to ``False`` to skip audio conversion.
        """
        return await self._request(
            "POST",
            f"/message/sendWhatsAppAudio/{instance}",
            json={
                "number": number,
                "audio": audio,
                "encoding": encoding,
            },
        )

    async def send_ptv(
        self,
        instance: str,
        number: str,
        video: str,
    ) -> dict:
        """Send a push-to-view circular video note.

        POST /message/sendPtv/{instance}

        *video* should be a URL or base64 string.
        """
        return await self._request(
            "POST",
            f"/message/sendPtv/{instance}",
            json={
                "number": number,
                "video": video,
            },
        )
