"""Read-only checks for the local Phase 1 runtime."""
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import config


def check():
    failed = False
    print(f'Python: {sys.version.split()[0]}')
    try:
        print(f'Chat provider: {config.CHAT_PROVIDER}; model: {config.chat_model()}')
    except ValueError as exc:
        print(f'Chat provider: [FAIL] {exc}')
        failed = True
    if config.CHAT_PROVIDER == 'deepseek':
        print(f'Chat runtime: max_tokens={config.DEEPSEEK_MAX_TOKENS}, '
              f'retry_tokens={config.DEEPSEEK_MAX_RETRY_TOKENS}, think={config.DEEPSEEK_THINK}, '
              f'timeout={config.DEEPSEEK_TIMEOUT}s')
        if not config.DEEPSEEK_API_KEY:
            print('DeepSeek: [FAIL] Set DEEPSEEK_API_KEY in .env, or select ollama')
            failed = True
        else:
            try:
                request = Request(config.DEEPSEEK_URL + '/models', headers={
                    'Authorization': 'Bearer ' + config.DEEPSEEK_API_KEY})
                with urlopen(request, timeout=10) as response:
                    models = {item['id'] for item in json.load(response)['data']}
                if config.DEEPSEEK_MODEL not in models:
                    raise ValueError('Selected DEEPSEEK_MODEL is not available')
                print('DeepSeek: authentication and selected model [OK]; no generation requested')
            except (OSError, ValueError, KeyError) as exc:
                detail = str(exc).replace(config.DEEPSEEK_API_KEY, '[REDACTED]')
                print(f'DeepSeek: [FAIL] {detail}')
                failed = True
    else:
        print(f'Chat runtime: num_ctx={config.NUM_CTX}, num_predict={config.NUM_PREDICT}, '
              f'max_num_predict={config.MAX_NUM_PREDICT}, think={config.THINK}, '
              f'timeout={config.CHAT_TIMEOUT}s')
    print(f'Database: {Path(config.DB_PATH).resolve()}')
    from .knowledge import resolve_root
    root=resolve_root(config.KNOWLEDGE_DIR)
    print(f'Knowledge directory: {root} [{"OK" if root.is_dir() else "MISSING"}]')
    try:
        ZoneInfo(config.TIMEZONE)
        print(f'Timezone: {config.TIMEZONE} [OK]')
    except (KeyError, ValueError) as exc:
        print(f'Timezone: [FAIL] {exc}')
        failed = True
    services = {config.EMBED_URL: [('Embedding model', config.EMBED_MODEL)]}
    if config.CHAT_PROVIDER == 'ollama':
        services.setdefault(config.BASE_URL, []).insert(0, ('Chat model', config.MODEL))
    for url, selections in services.items():
        try:
            with urlopen(url + '/api/tags', timeout=5) as response:
                models = {item['name'] for item in json.load(response)['models']}
            print(f'Ollama service: {url} [OK]')
        except (OSError, ValueError, KeyError) as exc:
            print(f'Ollama: [FAIL] {exc}. Start the service: sudo systemctl start ollama')
            failed = True
            continue
        for label, model in selections:
            selected = model if ':' in model else model + ':latest'
            if selected not in models:
                print(f'{label}: {model} [FAIL] Run: ollama pull {model}')
                failed = True
            else:
                print(f'{label}: {model} [OK]')
    print('Web search (Doubao Custom): ' + ('API key configured (not verified; use --search-test)' if config.SEARCH_API_KEY
                            else 'unavailable; set DOUBAO_SEARCH_API_KEY in .env'))
    print('Reminders: checked before each CLI prompt; no background notifications.')
    return 1 if failed else 0

