"""The application factory.

The exception handlers are the substance of this module, and the mapping from
ValKit's error hierarchy onto status codes is a compliance decision rather than
an HTTP convention:

``SpecError`` → **422**. The specification is well-formed HTTP and wrong on its
merits; the response carries the dotted path to the offending field.

``IntegrityError`` (and its ``AuditError``/``VaultError`` subclasses) → **500**.
A broken hash chain or a corrupted evidence object is not the caller's mistake.
It means this service cannot vouch for what it stored, which is a server
condition and belongs in the alarms. It is checked before ``ValKitError``
because both subclass it.

``AuthorizationError`` → **403**, ``SignatureError`` → **400**, everything else
in the hierarchy → **400**.

Acceptance failure appears nowhere in that list. An agent that missed its target
is a successful request with ``passed: false``, exactly as the CLI exits 1
rather than crashing.

And the handler that matters most is the one for request-validation errors. By
default FastAPI echoes the offending input back in the 422 body, which for a
signing request means returning the password that failed validation. The handler
below redacts the body first.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from valkit.errors import (
    AuthorizationError,
    IntegrityError,
    SignatureError,
    SpecError,
    ValKitError,
)
from valkit.util import redact

from .deps import Services, build_services, set_services
from .routes import health_router, router
from .settings import Settings

__all__ = ["create_app", "app"]

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Part 11 validation-as-code for LLM agents in GxP workflows.

Ingest a `valkit.yaml`, run the acceptance battery, generate the validation
package, and apply 21 CFR Part 11 electronic signatures — with a hash-chained
audit trail and content-addressed immutable evidence underneath.

**Every POST requires an `X-ValKit-Actor` header.** It is recorded in the audit
trail; a record whose actor is "the API" is not an audit trail.

**There is no PUT, PATCH or DELETE.** Records are append-only by design.

**ValKit does not make anyone compliant.** It produces evidence and documents.
""".strip()


def create_app(
    settings: Settings | None = None,
    *,
    services: Services | None = None,
    **service_kwargs: Any,
) -> FastAPI:
    """Build the application.

    ``services`` is injectable so a test can supply a frozen clock and a
    temporary vault; nothing here reaches for a module-level singleton.
    """
    container = services or build_services(settings, **service_kwargs)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        container.close()

    application = FastAPI(
        title="ValKit",
        version=container.settings.version,
        description=DESCRIPTION,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    set_services(application, container)

    if container.settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(container.settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    application.include_router(health_router)
    application.include_router(router)
    _install_error_handlers(application)

    if container.settings.serve_console and STATIC_DIR.is_dir():
        _install_console(application)

    return application


def _install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        """One error shape for the whole API.

        Starlette's default is ``{"detail": ...}`` while every ValKit error
        below returns ``{"error", "error_type"}``. A client that has to branch
        on which shape came back will get it wrong somewhere.
        """
        return JSONResponse(
            status_code=error.status_code,
            content={"error": str(error.detail), "error_type": "HTTPError"},
            headers=getattr(error, "headers", None),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """Redact before echoing.

        FastAPI's default handler returns the input that failed validation. For
        a signing request that input contains a password, and a 422 body is as
        durable as any other log line, so the components are stripped first.
        """
        details = []
        for item in error.errors():
            entry = {k: v for k, v in item.items() if k != "ctx"}
            if "input" in entry:
                entry["input"] = redact(entry["input"])
            if any(str(part).lower() in _CREDENTIAL_FIELDS for part in item.get("loc", ())):
                entry["input"] = "***REDACTED***"
            details.append(entry)
        return JSONResponse(
            status_code=422,
            content={
                "error": "the request body did not validate",
                "error_type": "RequestValidationError",
                "detail": details,
            },
        )

    @application.exception_handler(IntegrityError)
    async def integrity_error(request: Request, error: IntegrityError) -> JSONResponse:
        # Checked before ValKitError: AuditError and VaultError subclass both,
        # and reporting a broken chain as a client error would be wrong in the
        # one case where getting it right matters most.
        return JSONResponse(
            status_code=500,
            content={
                "error": str(error),
                "error_type": type(error).__name__,
                "detail": (
                    "Stored evidence failed verification. This is an integrity failure, "
                    "not an acceptance failure: the evidence cannot be trusted until it "
                    "is investigated."
                ),
            },
        )

    @application.exception_handler(SpecError)
    async def spec_error(request: Request, error: SpecError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": str(error),
                "error_type": "SpecError",
                "path": getattr(error, "path", None),
            },
        )

    @application.exception_handler(AuthorizationError)
    async def authorization_error(
        request: Request, error: AuthorizationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": str(error), "error_type": "AuthorizationError"},
        )

    @application.exception_handler(SignatureError)
    async def signature_error(request: Request, error: SignatureError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": str(error), "error_type": type(error).__name__},
        )

    @application.exception_handler(ValKitError)
    async def valkit_error(request: Request, error: ValKitError) -> JSONResponse:
        # "no such document" and "no such validation" are the common case here,
        # and 404 is the honest code for them.
        message = str(error)
        status = 404 if message.lower().startswith("no ") else 400
        return JSONResponse(
            status_code=status,
            content={"error": message, "error_type": type(error).__name__},
        )


_CREDENTIAL_FIELDS = {"components", "password", "second_factor", "secret", "token"}


def _install_console(application: FastAPI) -> None:
    from fastapi.staticfiles import StaticFiles

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @application.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # The page declares an SVG icon, but a browser that ignores the tag asks
        # for this path anyway, and a 404 in the console of a validation tool is
        # noise that trains people to ignore the console.
        return FileResponse(str(STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """The ASGI entry point the deployment names: ``uvicorn api.main:app``.

    Built on first access rather than at import, so that importing this module
    — which a test or a documentation build does — neither creates a workspace
    directory nor opens the audit database.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
