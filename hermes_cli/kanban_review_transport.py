"""Request-local managed routing checks; no global HTTP monkeypatches."""
import json
from urllib.parse import urlsplit


def pin_route(agent):
    route = (agent.provider, agent.api_mode, agent.model, str(agent.base_url or '').rstrip('/'))
    if route[1] not in {'chat_completions', 'anthropic_messages', 'codex_responses'}:
        raise PermissionError('managed transport is unsupported; use a verifiable SDK route')
    parsed = urlsplit(route[3])
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PermissionError('managed transport requires a verifiable endpoint')
    previous = getattr(agent, '_kanban_pinned_route', None)
    if previous is not None and previous != route:
        raise PermissionError('managed transport route changed after admission')
    agent._kanban_pinned_route = route


def guard_client(agent, client):
    route = getattr(agent, '_kanban_pinned_route', None)
    if route is None:
        return client
    pin_route(agent)
    import httpx
    wire = getattr(client, '_client', None)
    if not isinstance(wire, httpx.Client) or getattr(client, 'max_retries', None) != 0:
        raise PermissionError('managed transport requires a finite request-local SDK client')
    if getattr(wire, '_kanban_guard_owner', None) is agent:
        return client
    base = urlsplit(route[3])

    def authorize(request):
        pin_route(agent)
        target = urlsplit(str(request.url))
        if ((target.scheme, target.netloc) != (base.scheme, base.netloc)
                or not target.path.startswith(base.path.rstrip('/') + '/')):
            raise PermissionError('managed transport request endpoint differs from admission')
        try:
            model = json.loads(request.content)['model']
        except (ValueError, KeyError, TypeError) as exc:
            raise PermissionError('managed transport request model is unverifiable') from exc
        if model != route[2]:
            raise PermissionError('managed transport request model differs from admission')
        from hermes_cli.kanban_review_worker import before_model_request
        before_model_request(agent, {'model': model})

    wire.event_hooks['request'].append(authorize)
    wire._kanban_guard_owner = agent
    return client
