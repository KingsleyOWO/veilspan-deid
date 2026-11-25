"""ALLOWED_CLIENTS: the addresses that may reach the service. Everyone else is refused before anything else runs."""
import ipaddress
import logging

log = logging.getLogger("deid.access")
LOGGED = 200  # refused addresses written to the log once each, up to this many


def parse_clients(raw: str) -> list:
    """Addresses and ranges from a comma-separated list. An unreadable list is an error, never an open door."""
    nets = []
    if not raw.strip():
        return nets
    for part in raw.split(","):
        try:
            nets.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            raise ValueError(f"ALLOWED_CLIENTS 裡的「{part.strip()}」不是 IP 位址或網段") from None
    return nets


class AllowedClients:
    """ASGI wrapper. The address is the peer of the connection; forwarding headers are not looked at."""

    def __init__(self, app, networks: list):
        self.app, self.networks, self._logged = app, networks, set()

    def allowed(self, client) -> bool:
        try:
            ip = ipaddress.ip_address(client[0])
        except (TypeError, ValueError, IndexError):
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return any(ip.version == net.version and ip in net for net in self.networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or self.allowed(scope.get("client")):
            return await self.app(scope, receive, send)
        shown = str((scope.get("client") or ["不明"])[0] or "不明")
        if shown not in self._logged and len(self._logged) < LOGGED:
            self._logged.add(shown)
            log.info("refused a connection from %s (not in ALLOWED_CLIENTS)", shown)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        body = f"這個位址（{shown}）不在允許連線的清單內，請洽系統管理者。".encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
