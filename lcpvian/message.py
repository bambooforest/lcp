import json

from aiohttp import web

from .typed import JSONObject
from .utils import ensure_authorised, push_msg


@ensure_authorised
async def get_message(request: web.Request) -> web.Response:
    """
    Fetch arbitrary JSON data from redis
    """
    uu: str = request.match_info["uuid"]
    data: bytes | None
    response: JSONObject
    data = request.app["redis"].get(uu, None)
    if data is None:
        data = await request.app["aredis"].get(uu, None)
    if data is None:
        response = {"action": "fetch", "msg_id": uu, "status": "failed"}
        return web.json_response(response)
    jso: JSONObject = json.loads(data)
    room: None | str = jso.get("room", None)
    user: str = jso.get("user", "")
    await push_msg(request.app["websockets"], room or "", jso, just=(room, user))
    response = {"action": "fetch", "user": user, "room": room, "msg_id": uu}
    return web.json_response(response)
