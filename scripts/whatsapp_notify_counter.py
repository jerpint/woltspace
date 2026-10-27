"""Body-blind notification counter shared by isolated acceptance runners."""
import threading


class _NotifyCounter:
    """Count only /notify request/response metadata, never bodies."""
    def __init__(self, app, *, owner_only=False):
        self.owner_only = owner_only
        self.app, self.count, self.status = app, 0, None
        self.owner_count = self.legacy_count = 0
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        notify = scope.get("type") == "http" and scope.get("path") in {"/notify", "/notify/owner"}
        if notify:
            with self.lock:
                self.count += 1
                if scope.get("path") == "/notify/owner":
                    self.owner_count += 1
                else:
                    self.legacy_count += 1
        async def counted_send(message):
            if notify and message.get("type") == "http.response.start":
                with self.lock:
                    status = message.get("status")
                    self.status = status if type(status) is int and 100 <= status <= 599 else None
            await send(message)
        if self.owner_only and notify and (
                scope.get("path") != "/notify/owner" or self.owner_count > 1):
            # Refuse fallback/duplicate requests BEFORE application/provider I/O.
            await counted_send({"type": "http.response.start", "status": 409,
                                "headers": [(b"content-type", b"application/json")]})
            await counted_send({"type": "http.response.body",
                                "body": b'{"status":"refused"}'})
            return
        await self.app(scope, receive, counted_send)
