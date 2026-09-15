"""Same-origin settings transport; configuration is persisted before activation."""
import asyncio
from pathlib import Path

from aiohttp import web
from settings_config import SETTINGS_SCHEMA, validate_settings


def payload(service):
    values = dict(service.configuration)
    values['action_confirmation'] = 'confirm' if service.confirmation_required else 'automatic'
    values['audio_capture'] = service.audio_capture_enabled
    return {
        'schema': [dict(item, default=service.configuration_defaults[item['key']]) for item in SETTINGS_SCHEMA],
        'values': values,
        'revision': service.settings_revision,
        'restart_required': [item['key'] for item in SETTINGS_SCHEMA
                             if item['apply'] == 'restart' and values[item['key']] != service.active_configuration[item['key']]],
        'read_only': {
            'input_sample_rate_hz': 16000, 'output_sample_rate_hz': 24000,
            'transcription': service.stt_description, 'speech_output': service.tts_backend_description,
            'semantic_endpoint': service.semantic_turn_description,
            'deployment_managed': ['listen host/port', 'model endpoint URLs', 'model and executable paths',
                                   'GPU assignment', 'service resource limits', 'TLS/private ingress',
                                   'credentials', 'history/recording/debug storage locations',
                                   'Qwen model-server deployment parameters', 'Android wake-word/Assistant permission'],
        },
    }


def install(app, origin_matches):
    root = Path(__file__).parent

    async def page(request):
        return web.FileResponse(root / ('settings.js' if request.path.endswith('.js') else 'settings.html'), headers={'Cache-Control': 'no-store'})

    async def settings(request):
        service = request.app['service']
        if not origin_matches(request):
            return web.json_response({'error': 'Settings require the same origin.'}, status=403)
        if request.method == 'GET':
            return web.json_response(payload(service), headers={'Cache-Control': 'no-store'})
        if request.content_type != 'application/json':
            return web.json_response({'error': 'Use application/json.'}, status=415)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'values', 'revision'}:
                raise ValueError('Expected values and revision.')
            if type(body['revision']) is not int or body['revision'] != service.settings_revision:
                return web.json_response({'error': 'Settings changed elsewhere. Reload saved settings before saving.'}, status=409)
            values = validate_settings(body['values'], payload(service)['values'])
            old = (service.configuration, service.confirmation_required, service.audio_capture_enabled, service.settings_revision)
            service.configuration = values
            service.confirmation_required = values['action_confirmation'] == 'confirm'
            service.audio_capture_enabled = values['audio_capture']
            service.settings_revision += 1
            try:
                service._save_settings(strict=True)
            except OSError:
                service.configuration, service.confirmation_required, service.audio_capture_enabled, service.settings_revision = old
                return web.json_response({'error': 'Could not persist settings. No changes were applied.'}, status=500)
            if not service.confirmation_required:
                service.pending_confirmation = None
            for socket in list(request.app.get('live_sockets', ())):
                if not socket.closed:
                    try:
                        await socket.send_json(service.settings_payload())
                    except ConnectionError:
                        pass
            return web.json_response(payload(service))
        except (ValueError, TypeError) as error:
            return web.json_response({'error': str(error)}, status=400)

    async def restart(request):
        if not origin_matches(request) or request.content_type != 'application/json':
            return web.json_response({'error': 'Same-origin JSON request required.'}, status=403)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({'error': 'Invalid JSON.'}, status=400)
        service = request.app['service']
        if not isinstance(body, dict) or type(body.get('revision')) is not int or body.get('revision') != service.settings_revision:
            return web.json_response({'error': 'Reload settings before applying.'}, status=409)
        # Fixed service name: never accept executable or unit names from the browser.
        process = await asyncio.create_subprocess_exec('systemctl', '--user', '--no-block', 'restart',
                                                      'openclaw-live-conversation.service',
                                                      stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
        code = await process.wait()
        if code:
            return web.json_response({'error': 'Service manager could not restart the voice service.'}, status=503)
        return web.json_response({'ok': True})

    app.router.add_get('/settings', page)
    app.router.add_get('/settings.js', page)
    app.router.add_get('/api/settings', settings)
    app.router.add_put('/api/settings', settings)
    app.router.add_post('/api/settings/restart', restart)
