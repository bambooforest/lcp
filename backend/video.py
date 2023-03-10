import os

from aiohttp import web

from . import utils


@utils.ensure_authorised
async def video(request):
    corpora = [i.strip() for i in request.rel_url.query["corpora"].split(",")]
    out = {}
    for corpus in corpora:
        try:
            paths = request.app["corpora"][corpus]["videos"]
        except (AttributeError, KeyError):
            paths = [f"{corpus}.mp4"]
        out[corpus] = paths
    return web.json_response(out)