"""Indexed official-source discovery. Snippets are hints, never verified facts."""
from urllib.parse import urlsplit

import asyncio
import httpx

from ...config import tavily_api_key
from ..core.telemetry import span


async def search_official_index(query, domains, *, timeout=15., deadline=None, client=None):
    domains = tuple(str(d).casefold().rstrip('.') for d in domains)
    if not domains:
        return []
    if deadline is not None:
        timeout = min(timeout, deadline - asyncio.get_running_loop().time())
    if timeout <= 0:
        raise TimeoutError('official search budget exhausted')
    key = tavily_api_key()
    if not key:
        return []
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        with span('official.search', tool='search_official_pages', provider='tavily_index'):
            async with asyncio.timeout(timeout):
                response = await client.post('https://api.tavily.com/search',
                    headers={'Authorization': 'Bearer ' + key},
                    json={'query': query, 'include_domains': list(domains), 'max_results': 8,
                          'search_depth': 'basic', 'include_answer': False, 'include_raw_content': False})
                response.raise_for_status()
                items = response.json().get('results', [])
        output, seen = [], set()
        for item in items:
            url = str(item.get('url', ''))
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or '').casefold()
                allowed = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                           and parsed.port in {None, 443} and any(host == d or host.endswith('.'+d) for d in domains))
            except ValueError:
                continue
            if allowed and url not in seen:
                seen.add(url)
                output.append({'url': url, 'title': str(item.get('title', ''))[:500],
                               'content': str(item.get('content', ''))[:800]})
        return output
    finally:
        if own:
            await client.aclose()
