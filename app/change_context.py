"""Request-local actor shared by FastAPI's separate synchronous worker calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect


@dataclass
class RequestActor:
    actor: dict | None = None
    active: bool = True


_request = ContextVar("staff_change_request", default=None)
_automatic = ContextVar("staff_change_automatic", default=False)


def current_actor():
    context = _request.get()
    return context.actor if context and context.active and not _automatic.get() else None


def set_authenticated_actor(user):
    context = _request.get()
    if context and context.active:
        context.actor = {"uid": user.user_id, "role": user.role, "org_id": user.org_id,
                         "display_name": user.display_name or user.username}


@contextmanager
def actor_context(actor):
    """Explicit request scope for service tests; ordinary background sessions have none."""
    context = RequestActor(dict(actor))
    token = _request.set(context)
    try:
        yield context
    finally:
        context.active = False
        _request.reset(token)


class ChangeActorMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        context = RequestActor()
        token = _request.set(context)

        async def send_response(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                # Starlette background tasks run after this message, possibly in a copied context.
                context.active = False
                context.actor = None
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        finally:
            context.active = False
            context.actor = None
            _request.reset(token)


@contextmanager
def automatic_changes(db):
    """Flush manual work first and automatic work before restoring the actor."""
    if not current_actor():
        yield
        return
    db.flush()
    token = _automatic.set(True)
    try:
        yield
        db.flush()
    finally:
        _automatic.reset(token)


def automatic(func=None, *, when=None):
    if func is None:
        return lambda wrapped: automatic(wrapped, when=when)
    signature = inspect.signature(func)

    @wraps(func)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        if when is not None and not when(arguments):
            return func(*args, **kwargs)
        db = arguments["db"]
        with automatic_changes(db):
            return func(*args, **kwargs)
    return wrapped
