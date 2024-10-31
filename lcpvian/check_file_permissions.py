"""
check-file-permissions.py: endpoint for telling FE whether user has permission
for a given resource
"""

from aiohttp import web


async def check_file_permissions(request: web.Request) -> web.Response:
    """
    Returns if user has access to file
    """

    msg, status = request.app["auth_class"](request.app).check_file_permissions(request)

    return web.Response(body=msg, status=status)
