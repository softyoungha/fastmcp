from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import (
    AccessTokenT,
    AuthorizationCodeT,
    OAuthAuthorizationServerProvider,
    RefreshTokenT,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel.server import LifespanResultT
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from fastmcp.utilities.logging import get_logger

if TYPE_CHECKING:
    from fastmcp.server.server import FastMCP

logger = get_logger(__name__)


_current_http_request: ContextVar[Request | None] = ContextVar(
    "http_request",
    default=None,
)


@contextmanager
def set_http_request(request: Request) -> Generator[Request, None, None]:
    token = _current_http_request.set(request)
    try:
        yield request
    finally:
        _current_http_request.reset(token)


class RequestContextMiddleware:
    """
    Middleware that stores each request in a ContextVar
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            with set_http_request(Request(scope)):
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def setup_auth_middleware_and_routes(
    auth_server_provider: OAuthAuthorizationServerProvider[
        AuthorizationCodeT, RefreshTokenT, AccessTokenT
    ]
    | None,
    auth_settings: AuthSettings | None,
) -> tuple[list[Middleware], list[BaseRoute], list[str]]:
    """Set up authentication middleware and routes if auth is enabled.

    Args:
        auth_server_provider: The OAuth authorization server provider
        auth_settings: The auth settings

    Returns:
        Tuple of (middleware, auth_routes, required_scopes)
    """
    middleware: list[Middleware] = []
    auth_routes: list[BaseRoute] = []
    required_scopes: list[str] = []

    if auth_server_provider:
        if not auth_settings:
            raise ValueError(
                "auth_settings must be provided when auth_server_provider is specified"
            )

        middleware = [
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(provider=auth_server_provider),
            ),
            Middleware(AuthContextMiddleware),
        ]

        required_scopes = auth_settings.required_scopes or []

        auth_routes.extend(
            create_auth_routes(
                provider=auth_server_provider,
                issuer_url=auth_settings.issuer_url,
                service_documentation_url=auth_settings.service_documentation_url,
                client_registration_options=auth_settings.client_registration_options,
                revocation_options=auth_settings.revocation_options,
            )
        )

    return middleware, auth_routes, required_scopes


def create_base_app(
    routes: list[BaseRoute],
    middleware: list[Middleware],
    debug: bool = False,
    lifespan: Callable | None = None,
) -> Starlette:
    """Create a base Starlette app with common middleware and routes.

    Args:
        routes: List of routes to include in the app
        middleware: List of middleware to include in the app
        debug: Whether to enable debug mode
        lifespan: Optional lifespan manager for the app

    Returns:
        A Starlette application
    """
    # Always add RequestContextMiddleware as the outermost middleware
    middleware.append(Middleware(RequestContextMiddleware))

    return Starlette(
        routes=routes,
        middleware=middleware,
        debug=debug,
        lifespan=lifespan,
    )


def create_sse_app(
    server: FastMCP[LifespanResultT],
    message_path: str,
    sse_path: str,
    auth_server_provider: OAuthAuthorizationServerProvider[
        AuthorizationCodeT, RefreshTokenT, AccessTokenT
    ]
    | None = None,
    auth_settings: AuthSettings | None = None,
    debug: bool = False,
    routes: list[BaseRoute] | None = None,
    middleware: list[Middleware] | None = None,
) -> Starlette:
    """Return an instance of the SSE server app.

    Args:
        server: The FastMCP server instance
        message_path: Path for SSE messages
        sse_path: Path for SSE connections
        auth_server_provider: Optional auth provider
        auth_settings: Optional auth settings
        debug: Whether to enable debug mode
        routes: Optional list of custom routes
        middleware: Optional list of middleware
    Returns:
        A Starlette application with RequestContextMiddleware
    """

    server_routes: list[BaseRoute] = []
    server_middleware: list[Middleware] = []

    # Set up SSE transport
    sse = SseServerTransport(message_path)

    # Create handler for SSE connections
    async def handle_sse(scope: Scope, receive: Receive, send: Send) -> Response:
        async with sse.connect_sse(scope, receive, send) as streams:
            await server._mcp_server.run(
                streams[0],
                streams[1],
                server._mcp_server.create_initialization_options(),
            )
        return Response()

    # Get auth middleware and routes
    auth_middleware, auth_routes, required_scopes = setup_auth_middleware_and_routes(
        auth_server_provider, auth_settings
    )

    server_routes.extend(auth_routes)
    server_middleware.extend(auth_middleware)

    # Add SSE routes with or without auth
    if auth_server_provider:
        # Auth is enabled, wrap endpoints with RequireAuthMiddleware
        server_routes.append(
            Route(
                sse_path,
                endpoint=RequireAuthMiddleware(handle_sse, required_scopes),
                methods=["GET"],
            )
        )
        server_routes.append(
            Mount(
                message_path,
                app=RequireAuthMiddleware(sse.handle_post_message, required_scopes),
            )
        )
    else:
        # No auth required
        async def sse_endpoint(request: Request) -> Response:
            return await handle_sse(request.scope, request.receive, request._send)  # type: ignore[reportPrivateUsage]

        server_routes.append(
            Route(
                sse_path,
                endpoint=sse_endpoint,
                methods=["GET"],
            )
        )
        server_routes.append(
            Mount(
                message_path,
                app=sse.handle_post_message,
            )
        )

    # Add custom routes with lowest precedence
    if routes:
        server_routes.extend(routes)

    # Add middleware
    if middleware:
        server_middleware.extend(middleware)

    # Create and return the app
    return create_base_app(
        routes=server_routes,
        middleware=server_middleware,
        debug=debug,
    )


def create_streamable_http_app(
    server: FastMCP[LifespanResultT],
    streamable_http_path: str,
    event_store: None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[
        AuthorizationCodeT, RefreshTokenT, AccessTokenT
    ]
    | None = None,
    auth_settings: AuthSettings | None = None,
    json_response: bool = False,
    stateless_http: bool = False,
    debug: bool = False,
    routes: list[BaseRoute] | None = None,
    middleware: list[Middleware] | None = None,
) -> Starlette:
    """Return an instance of the StreamableHTTP server app.

    Args:
        server: The FastMCP server instance
        streamable_http_path: Path for StreamableHTTP connections
        event_store: Optional event store for session management
        auth_server_provider: Optional auth provider
        auth_settings: Optional auth settings
        json_response: Whether to use JSON response format
        stateless_http: Whether to use stateless mode (new transport per request)
        debug: Whether to enable debug mode
        routes: Optional list of custom routes
        middleware: Optional list of middleware

    Returns:
        A Starlette application with StreamableHTTP support
    """
    server_routes: list[BaseRoute] = []
    server_middleware: list[Middleware] = []

    # Create session manager using the provided event store
    if server._session_manager is None:
        server._session_manager = StreamableHTTPSessionManager(
            app=server._mcp_server,
            event_store=event_store,
            json_response=json_response,
            stateless=stateless_http,
        )

    # Create the ASGI handler
    async def handle_streamable_http(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        await server.session_manager.handle_request(scope, receive, send)

    # Get auth middleware and routes
    auth_middleware, auth_routes, required_scopes = setup_auth_middleware_and_routes(
        auth_server_provider, auth_settings
    )

    server_routes.extend(auth_routes)
    server_middleware.extend(auth_middleware)

    # Add StreamableHTTP routes with or without auth
    if auth_server_provider:
        # Auth is enabled, wrap endpoint with RequireAuthMiddleware
        server_routes.append(
            Mount(
                streamable_http_path,
                app=RequireAuthMiddleware(handle_streamable_http, required_scopes),
            )
        )
    else:
        # No auth required
        server_routes.append(
            Mount(
                streamable_http_path,
                app=handle_streamable_http,
            )
        )

    # Add custom routes with lowest precedence
    if routes:
        server_routes.extend(routes)

    # Add middleware
    if middleware:
        server_middleware.extend(middleware)

    # Create a lifespan manager to start and stop the session manager
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        async with server.session_manager.run():
            yield

    # Create and return the app with lifespan
    return create_base_app(
        routes=server_routes,
        middleware=server_middleware,
        debug=debug,
        lifespan=lifespan,
    )
