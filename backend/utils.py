import aiohttp
import json
import jwt
import os
import re

from collections import defaultdict
from datetime import date, datetime
from functools import wraps
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Reversible,
    Tuple,
    Union,
    Sequence,
    Mapping,
    Sized,
)
from uuid import UUID

from aiohttp import web
from rq.command import PUBSUB_CHANNEL_TEMPLATE
from rq.connections import Connection
from rq.exceptions import NoSuchJobError
from rq.job import Job


PUBSUB_CHANNEL = PUBSUB_CHANNEL_TEMPLATE % "query"


class Interrupted(Exception):
    """
    Used when a user interrupts a query from frontend
    """

    pass


class CustomEncoder(json.JSONEncoder):
    """
    UUID and time to string
    """

    def default(self, obj: Any):
        if isinstance(obj, UUID):
            return obj.hex
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return json.JSONEncoder.default(self, obj)


def ensure_authorised(func: Callable):
    """
    auth decorator, still wip
    """
    return func

    @wraps(func)
    async def deco(request: web.Request, *args, **kwargs):
        headers = await _lama_user_details(getattr(request, "headers", request))

        if "X-Access-Token" in headers:
            token = headers.get("X-Access-Token")
            try:
                decoded = jwt.decode(
                    token, os.getenv("JWT_SECRET_KEY"), algorithms=["HS256"]
                )
                request.jwt = decoded
            except Exception as err:
                raise err
        if "X-Display-Name" in headers:
            username = headers.get("X-Display-Name")
            request.username = username
        if "X-Mail" in headers:
            username = headers.get("X-Mail")
            request.username = username

        if not request.username:
            raise ValueError("401? No username")

        return func(request, *args, **kwargs)

    return deco


def _extract_lama_headers(headers: Mapping) -> Dict[str, str]:
    """
    Create needed headers from existing headers
    """
    retval = {
        "X-API-Key": os.environ["LAMA_API_KEY"],
        "X-Remote-User": headers.get("X-Remote-User"),
        "X-Display-Name": headers["X-Display-Name"].encode("cp1252").decode("utf8")
        if headers.get("X-Display-Name")
        else "",
        "X-Edu-Person-Unique-Id": headers.get("X-Edu-Person-Unique-Id"),
        "X-Home-Organization": headers.get("X-Home-Organization"),
        "X-Schac-Home-Organization": headers.get("X-Schac-Home-Organization"),
        "X-Persistent-Id": headers.get("X-Persistent-Id"),
        "X-Given-Name": headers["X-Given-Name"].encode("cp1252").decode("utf8")
        if headers.get("X-Given-Name")
        else "",
        "X-Surname": headers["X-Surname"].encode("cp1252").decode("utf8")
        if headers.get("X-Surname")
        else "",
        "X-Principal-Name": headers.get("X-Principal-Name"),
        "X-Mail": headers.get("X-Mail"),
        "X-Shib-Identity-Provider": headers.get("X-Shib-Identity-Provider"),
    }
    return {k: v for k, v in retval.items() if v}


def _check_email(email: str) -> bool:
    """
    Is an email address valid?
    """
    regex = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    return bool(re.fullmatch(regex, email))


def get_user_identifier(headers: Dict[str, Any]) -> Optional[str]:
    """
    Get best possible identifier
    """
    persistent_id = headers.get("X-Persistent-Id")
    persistent_name = headers.get("X-Principal-Name")
    edu_person_unique_id = headers.get("X-Edu-Person-Unique-Id")
    mail = headers.get("X-Mail")
    retval = None

    if persistent_id and bool(re.match("(.*)!(.*)!(.*)", persistent_id)):
        retval = persistent_id
    elif persistent_name and str(persistent_name).count("@") == 1:
        retval = persistent_name
    elif edu_person_unique_id and str(edu_person_unique_id).count("@") == 1:
        retval = edu_person_unique_id
    elif mail and _check_email(mail):
        retval = mail
    return retval


async def _lama_user_details(headers: Mapping[str, Any]) -> Dict:
    """
    todo: not tested yet, but the syntax is something like this
    """
    url = f"{os.getenv('LAMA_API_URL')}/user/details"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_extract_lama_headers(headers)) as resp:
            return await resp.json()


def _get_all_results(job: Union[Job, str], connection: Connection) -> List[Tuple]:
    """
    Get results from all parents -- reconstruct results from just latest batch
    """
    out: List[Tuple] = []
    if isinstance(job, str):
        job = Job.fetch(job, connection=connection)
    while True:
        batch = _add_results(job.result, 0, True, False, False, 0)
        batch.reverse()
        for bit in batch:
            out.append(bit)
        parent = job.kwargs.get("parent", None)
        if not parent:
            break
        job = Job.fetch(parent, connection=connection)
    out.reverse()
    return out


def _old_add_results(
    result: List[List],
    so_far: int,
    unlimited: bool,
    offset: Optional[int],
    restart: Union[bool, int],
    total_requested: int,
) -> List[Tuple]:
    """
    Helper function, run inside callback
    the args (total_found:18, len(result):9, so_far:9, None, False, 6, 20)
    """
    out: List = []
    for n, res in enumerate(result):
        if not unlimited and offset and n < offset:
            continue
        if restart is not False and n + 1 < restart:
            continue
        # fix: move sent_id to own column
        sent_id = res[0][0]
        tok_ids = res[0][1:]
        fixed = ((sent_id,), tuple(tok_ids), res[1], res[2])
        # end fix
        out.append(fixed)
        if not unlimited and so_far + len(out) >= total_requested:
            break
    return out


def _make_kwic_line(original, sents):
    out = []
    for sent in sents:
        sent = [str(sent[0])] + list(sent[1:])
        if str(sent[0]) == str(original[0]):
            return [original[0]] + list(sent) + original[1:]
    raise ValueError("matching sent not found", original)


def _add_results(
    result: List[List],
    so_far: int,
    unlimited: bool,
    offset: Optional[int],
    restart: Union[bool, int],
    total_requested: int,
    kwic: bool = False,
    sents: Optional[List[List]] = None,
    result_sets: Optional[Dict] = None,
) -> Tuple[Dict[int, List], int]:
    """
    todo: respect limits here?
    """
    bundle = {}
    count = 0
    kwics = set()
    counts = defaultdict(int)
    if not result_sets:
        for line in result:
            if not int(line[0]):
                res = line[1]["result_sets"]
                kwics = [
                    i for i, r in enumerate(res, start=1) if r.get("type") == "plain"
                ]
                kwics = set(kwics)
                break
    else:
        itt = result_sets.get("result_sets", result_sets)
        kwics = [i for i, r in enumerate(itt, start=1) if r.get("type") == "plain"]
        kwics = set(kwics)

    for line in result:
        key = int(line[0])
        rest = line[1]
        if not key:
            assert isinstance(rest, dict)
            bundle[key] = rest
        else:
            if not kwic and key in kwics:
                counts[key] += 1
                continue
            if key in kwics and kwic:
                counts[key] += 1
                if not unlimited and offset and count < offset:
                    continue
                if restart is not False and counts.get(key, 0) < restart:
                    continue
                if (
                    not unlimited
                    and so_far + len(bundle.get(key, [])) >= total_requested
                ):
                    continue

                rest = _make_kwic_line(rest, sents)

                if key not in bundle:
                    bundle[key] = [rest]
                else:
                    bundle[key].append(rest)
            elif key in kwics and not kwic:
                continue
            elif key not in kwics and kwic:
                continue
            elif key not in kwics and not kwic:
                if key not in bundle:
                    bundle[key] = [rest]
                else:
                    bundle[key].append(rest)

    n_results = None
    for k in kwics:
        if k not in bundle:
            continue
        if len(bundle[k]) > total_requested:
            bundle[k] = bundle[k][:total_requested]
            n_results = total_requested
    if n_results is None:
        n_results = counts[list(kwics)[0]]

    return bundle, n_results


def _union_results(so_far: Dict[int, List], incoming: Dict[int, List]) -> [int, List]:
    """
    Join two results objects
    """
    for k, v in incoming.items():
        if not k:
            if k in so_far:
                continue
            else:
                so_far[k] = v
                continue
        if k not in so_far:
            so_far[k] = []
        so_far[k] += v
    return so_far


def _push_stats(previous: str, connection: Connection) -> Dict[str, Any]:
    """
    Send statistics to the websocket
    """
    depended = Job.fetch(previous, connection=connection)
    base = depended.kwargs.get("base")
    basejob = Job.fetch(base, connection=connection) if base else depended

    jso = {
        "result": basejob.meta["_stats"],
        "status": depended.meta["_status"],
        "action": "stats",
        "user": basejob.kwargs["user"],
        "room": basejob.kwargs["room"],
    }
    connection.publish(PUBSUB_CHANNEL, json.dumps(jso, cls=CustomEncoder))
    return {
        "stats": True,
        "sentences_job": basejob.meta.get("latest_sentences", None),
        "status": "faked",
        "job": previous,
    }


async def handle_timeout(exc: Exception, request: web.Request) -> None:
    """
    If a job dies due to TTL, we send this...
    """
    request_data = await request.json()
    user = request_data["user"]
    room = request_data["room"]
    job = str(exc).split("rq:job:")[-1]
    jso = {
        "user": user,
        "room": room,
        "error": str(exc),
        "status": "timeout",
        "job": job,
        "action": "timeout",
    }
    connection = request.app["redis"]
    connection.publish(PUBSUB_CHANNEL, json.dumps(jso, cls=CustomEncoder))
