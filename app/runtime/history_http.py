"""Shared bounded history endpoints. Authorization stays with each server."""
import json
import math
from aiohttp import web


def register_history(app, store, owner, deletable, admin=None, delete_related=None):
    async def listing(request):
        who = None
        if request.path.startswith('/admin/'):
            admin(request)
        else: who = owner(request)
        before = None
        try:
            if request.query.get('before'):
                before = json.loads(request.query['before'])
                if not isinstance(before, list) or len(before) != 2 or not isinstance(before[0], (float, int)) or not math.isfinite(before[0]) or not isinstance(before[1], str) or len(before[1]) > 64:
                    raise ValueError()
            result = await store(request).acall('listing', who, request.query.get('q', ''), before)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='Invalid history search or cursor') from None
        return web.json_response(result)

    async def detail(request):
        who = None
        if request.path.startswith('/admin/'):
            admin(request)
        else: who = owner(request)
        record = await store(request).acall('detail', request.match_info['call_id'], who)
        if record is None: raise web.HTTPNotFound()
        if who is not None:
            # A caller may review their words, not private operator instructions.
            record['config'] = {k:v for k,v in record['config'].items() if k not in {'context', 'system_prompt'}}
            record['events'] = [e for e in record['events'] if e['kind'] != 'prompt']
        response = web.json_response(record)
        if request.query.get('download') == '1':
            response.headers['Content-Disposition'] = f'attachment; filename="conversation-{record["id"]}.json"'
        return response

    async def delete(request):
        if request.headers.get('X-Playground-Request') != '1': raise web.HTTPForbidden()
        if request.path.startswith('/admin/'):
            admin(request)
            who = await store(request).acall('owner_of', request.match_info['call_id'])
            if who is None: raise web.HTTPNotFound()
        else: who = owner(request)
        await deletable(request, who)
        try:
            count = await store(request).acall('delete', who, request.match_info['call_id'])
        except ValueError as exc: raise web.HTTPConflict(text=str(exc)) from None
        if delete_related is not None:
            count += await delete_related(request, who, request.match_info['call_id'])
        if not count: raise web.HTTPNotFound()
        return web.json_response({'deleted': True})

    app.router.add_get('/api/conversations', listing)
    app.router.add_get('/api/conversations/{call_id:[a-f0-9]{32}}', detail)
    app.router.add_delete('/api/conversations/{call_id:[a-f0-9]{32}}', delete)
    if admin:
        app.router.add_get('/admin/conversations', listing)
        app.router.add_get('/admin/conversations/{call_id:[a-f0-9]{32}}', detail)
        app.router.add_delete('/admin/conversations/{call_id:[a-f0-9]{32}}', delete)
