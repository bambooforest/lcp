"""
The main setup for the aiohttp backend.

Register URLs and endpoints, add redis, query_service, websockets, etc.
"""

import importlib
import json
import logging
import os

import aiohttp_cors
import asyncio
import uvloop

from collections import defaultdict, deque
from typing import cast

from aiohttp import WSCloseCode, web
from aiohttp.client_exceptions import ClientConnectorError
from aiohttp_catcher import Catcher, catch
from redis import Redis
from redis import asyncio as aioredis
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import ConstantBackoff
from redis.exceptions import ConnectionError
from redis.retry import Retry
from rq.exceptions import AbandonedJobError, NoSuchJobError
from rq.queue import Queue
from rq.registry import FailedJobRegistry

from .check_file_permissions import check_file_permissions
from .configure import CorpusConfig
from .corpora import corpora
from .corpora import corpora_meta_update
from .document import document, document_ids
from .lama_user_data import lama_user_data
from .message import get_message
from .project import project_api_create, project_api_revoke
from .project import project_create, project_update
from .project import project_users_invite, project_users
from .project import project_users_invitation_remove, project_user_update
from .query import query, refresh_config
from .query_service import QueryService
from .sock import listen_to_redis, sock, ws_cleanup
from .store import fetch_queries, store_query
from .typed import Endpoint, Task, Websockets
from .upload import make_schema, upload
from .utils import TRUES, FALSES, handle_lama_error, handle_timeout, load_env
from .video import video


load_env()

# this is all just a way to find out if utils (and therefore the codebase) is a c extension
_LOADER = importlib.import_module(handle_timeout.__module__).__loader__
C_COMPILED = "SourceFileLoader" not in str(_LOADER)
SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")
REDIS_DB_INDEX = int(os.getenv("REDIS_DB_INDEX", 0))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
APP_PORT = int(os.getenv("AIO_PORT", 9090))
DEBUG = bool(os.getenv("DEBUG", "false").lower() in TRUES)

AUTH_CLASS: type | None = None
try:
    need = cast(str, os.getenv("AUTHENTICATION_CLASS"))
    if need.strip() and "." in need and need.lower() not in FALSES:
        path, name = need.rsplit(".", 1)
        if path and name:
            modu = importlib.import_module(path)
            AUTH_CLASS = getattr(modu, name)
            print(f"Using authenication class: {need}")
except Exception as err:
    print(f"Warning: no authentication class found! ({err})")


if SENTRY_DSN:

    from sentry_sdk import init as sentry_init
    from sentry_sdk.integrations.aiohttp import AioHttpIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_logging = LoggingIntegration(
        level=logging.INFO,
        event_level=logging.WARNING,
    )

    sentry_init(
        dsn=SENTRY_DSN,
        integrations=[AioHttpIntegration(), sentry_logging],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", 1.0)),
        environment=os.getenv("SENTRY_ENVIRONMENT", "lcpvian"),
    )


async def on_shutdown(app: web.Application) -> None:
    """
    Close websocket connections on app shutdown
    """
    try:
        await app["aredis"].quit()
    except Exception:
        pass
    try:
        app["redis"].quit()
    except Exception:
        pass
    msg = "Server shutdown"
    for room, conns in app["websockets"].items():
        for ws, uid in conns:
            try:
                await ws.close(code=WSCloseCode.GOING_AWAY, message=msg)
            except Exception as err:
                print(f"Issue closing websocket for {room}/{uid}: {err}")


async def start_background_tasks(app: web.Application) -> None:
    """
    Start the thread that listens to redis pubsub
    Start the thread that periodically removes stale websocket connections
    """
    listener: Task[None] = asyncio.create_task(listen_to_redis(app))
    app["redis_listener"] = listener
    cleanup: Task[None] = asyncio.create_task(ws_cleanup(app["websockets"]))
    app["ws_cleanup"] = cleanup


async def cleanup_background_tasks(app: web.Application) -> None:
    """
    Stop running background tasks: redis listener, stale ws cleaner
    """
    app["redis_listener"].cancel()
    await app["redis_listener"]
    app["ws_cleanup"].cancel()
    await app["ws_cleanup"]


async def create_app(test: bool = False) -> web.Application:
    """
    Build an instance of the app. If test=True is passed, it is returned
    before background tasks are added, to aid with unit tests
    """

    catcher: Catcher = Catcher()

    await catcher.add_scenario(
        catch(NoSuchJobError, AbandonedJobError)
        .with_status_code(200)
        .and_call(handle_timeout)
    )
    await catcher.add_scenario(
        catch(ClientConnectorError)
        .with_status_code(401)
        .and_stringify()
        .with_additional_fields(
            {"message": "Could not connect to LAMa. Probably not user's fault..."}
        )
        .and_call(handle_lama_error)
    )
    # await catcher.add_scenario(
    #     catch(AuthError)
    #     .with_status_code(403)
    #     .and_stringify()
    #     .with_additional_fields(
    #         {"message": "Authentical..."}
    #     )
    #     .and_call(handle_lama_error)
    # )

    app = web.Application(middlewares=[catcher.middleware])
    app["mypy"] = C_COMPILED
    if C_COMPILED:
        print("Running mypy/c app!")

    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        },
    )

    # all websocket connections are stored in here, as {room: {(connection, user_id,)}}
    # when a user leaves the app, the connection should be removed. If it's not,
    # the dict is periodically cleaned by a separate thread, to stop this from always growing
    ws: Websockets = defaultdict(set)
    app["websockets"] = ws
    app["_debug"] = DEBUG

    app["auth_class"] = AUTH_CLASS

    # here we store corpus_id: config
    conf: dict[str, CorpusConfig] = {}
    app["config"] = conf

    # app["auth"] = Authenticator(app)

    endpoints: list[tuple[str, str, Endpoint]] = [
        ("/check-file-permissions", "GET", check_file_permissions),
        ("/config", "POST", refresh_config),
        ("/corpora", "POST", corpora),
        ("/corpora/{corpora_id}/meta/update", "PUT", corpora_meta_update),
        ("/create", "POST", make_schema),
        ("/document/{doc_id}", "POST", document),
        ("/document_ids/{corpus_id}", "POST", document_ids),
        ("/fetch", "POST", fetch_queries),
        ("/get_message/{uuid}", "GET", get_message),
        ("/project", "POST", project_create),
        ("/project/{project}", "POST", project_update),
        ("/project/{project}/api/create", "POST", project_api_create),
        ("/project/{project}/api/{key}/revoke", "POST", project_api_revoke),
        ("/project/{project}/users", "GET", project_users),
        ("/project/{project}/user/{user}/update", "POST", project_user_update),
        ("/project/{project}/users/invite", "POST", project_users_invite),
        (
            "/project/{project}/users/invitation/{invitation}",
            "DELETE",
            project_users_invitation_remove,
        ),
        ("/query", "POST", query),
        ("/settings", "GET", lama_user_data),
        ("/store", "POST", store_query),
        ("/upload", "POST", upload),
        ("/video", "GET", video),
        ("/ws", "GET", sock),
    ]

    for url, method, func in endpoints:
        cors.add(cors.add(app.router.add_resource(url)).add_route(method, func))

    redis_url: str = f"{REDIS_URL}/{REDIS_DB_INDEX}"
    redis_settings = Redis.from_url(redis_url)
    limit = "client-output-buffer-limit"
    pubsub_limit = redis_settings.config_get(limit)[limit]
    redis_settings.quit()
    _pieces = pubsub_limit.split()
    sleep_time = int(_pieces[-1]) + 2
    app["redis_pubsub_limit_sleep"] = sleep_time
    retry_policy: Retry = Retry(ConstantBackoff(sleep_time), 3)
    async_retry_policy: AsyncRetry = AsyncRetry(ConstantBackoff(sleep_time), 3)

    # Danny seemed to say mypy used to need a parser_class here, but newer redis releases seem to do away with parser_class
    # app["aredis"] = aioredis.Redis.from_url(url=redis_url, parser_class=ParserClass)
    app["aredis"] = aioredis.Redis.from_url(
        redis_url,
        health_check_interval=10,
        # parser_class=ParserClass,
        retry_on_error=[ConnectionError],
        retry=async_retry_policy,
    )
    app["redis"] = Redis.from_url(
        redis_url,
        health_check_interval=10,
        retry_on_error=[ConnectionError],
        retry=retry_policy,
    )
    app["redis"].set("timebytes", json.dumps([]))
    app["_redis_url"] = redis_url

    # different queues for different kinds of jobs
    app["internal"] = Queue("internal", connection=app["redis"], job_timeout=-1)
    app["query"] = Queue("query", connection=app["redis"])
    app["background"] = Queue("background", connection=app["redis"], job_timeout=-1)
    app["_use_cache"] = os.getenv("USE_CACHE", "1").lower() in TRUES

    # so far unused, we could potentially provide users with detailed feedback by
    # exploiting the 'failed job registry' provided by RQ.
    app["failed_registry_internal"] = FailedJobRegistry(queue=app["internal"])
    app["failed_registry_query"] = FailedJobRegistry(queue=app["query"])
    app["failed_registry_background"] = FailedJobRegistry(queue=app["background"])

    app["query_service"] = QueryService(app)
    await app["query_service"].get_config()
    canceled: deque[str] = deque(maxlen=99999)
    app["canceled"] = canceled

    if test:
        return app

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    app.on_shutdown.append(on_shutdown)

    return app


async def start_app() -> None:
    try:
        app = await create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, port=APP_PORT)
        await site.start()
        # wait forever
        await asyncio.Event().wait()
        return None
    except KeyboardInterrupt:
        return None
    except (asyncio.exceptions.CancelledError, OSError) as err:
        print(f"Port in use? : {err}")
        return None


def start() -> None:
    """
    Alternative starter
    """
    if "_TEST" in os.environ:
        return

    try:
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(start_app())
    except KeyboardInterrupt:
        print("Application stopped.")
    except (BaseException, OSError) as err:
        print(f"Port in use? : {err}")
    return None


# test mode should not start a loop
if "_TEST" in os.environ:
    pass

# development mode starts a dev server
elif __name__ == "__main__":
    start()
