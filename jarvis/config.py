import os
from pathlib import Path

def load_env(path='.env'):
    p=Path(path)
    if not p.exists(): return
    for line in p.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key,value=line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))

load_env()
MODEL=os.getenv('OLLAMA_MODEL','qwen3.5:9b')
CHAT_PROVIDER=os.getenv('JARVIS_CHAT_PROVIDER','ollama').strip().lower()
DEEPSEEK_URL=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com').rstrip('/')
DEEPSEEK_MODEL=os.getenv('DEEPSEEK_MODEL','deepseek-flash')
DEEPSEEK_API_KEY=os.getenv('DEEPSEEK_API_KEY','').strip()
DEEPSEEK_THINK=os.getenv('DEEPSEEK_THINK','false').strip().lower() in ('1','true','yes','on')
DEEPSEEK_MAX_TOKENS=int(os.getenv('DEEPSEEK_MAX_TOKENS','8192'))
DEEPSEEK_MAX_RETRY_TOKENS=int(os.getenv('DEEPSEEK_MAX_RETRY_TOKENS','16384'))
DEEPSEEK_TIMEOUT=int(os.getenv('DEEPSEEK_TIMEOUT','120'))
NUM_CTX=int(os.getenv('OLLAMA_NUM_CTX','32768'))
NUM_PREDICT=int(os.getenv('OLLAMA_NUM_PREDICT','8192'))
MAX_NUM_PREDICT=int(os.getenv('OLLAMA_MAX_NUM_PREDICT','16384'))
CHAT_TIMEOUT=int(os.getenv('OLLAMA_CHAT_TIMEOUT','300'))
THINK=os.getenv('OLLAMA_THINK','true').strip().lower() in ('1','true','yes','on')
BASE_URL=os.getenv('OLLAMA_URL','http://127.0.0.1:11434').rstrip('/')
DB_PATH=os.getenv('JARVIS_DB','data/jarvis.db')
SEARCH_API_KEY=os.getenv('DOUBAO_SEARCH_API_KEY','').strip()
TIMEZONE=os.getenv('JARVIS_TIMEZONE','Asia/Tokyo')
EMBED_MODEL=os.getenv('JARVIS_EMBED_MODEL','qwen3-embedding:0.6b')
EMBED_URL=os.getenv('JARVIS_EMBED_URL',BASE_URL).rstrip('/')
KNOWLEDGE_DIR=os.getenv('JARVIS_KNOWLEDGE_DIR','knowledge')
KNOWLEDGE_TOP_K=int(os.getenv('JARVIS_KNOWLEDGE_TOP_K','4'))
KNOWLEDGE_MIN_SCORE=float(os.getenv('JARVIS_KNOWLEDGE_MIN_SCORE','0.5'))
DESKTOP_ENABLED=os.getenv('JARVIS_DESKTOP_ENABLED','true').strip().lower() in ('1','true','yes','on')
DESKTOP_BRIDGE_URL=os.getenv('JARVIS_DESKTOP_BRIDGE_URL','http://127.0.0.1:18765').rstrip('/')
TRACE_ENABLED=os.getenv('JARVIS_TRACE_ENABLED','true').strip().lower() in ('1','true','yes','on')
TRACE_STREAM=os.getenv('JARVIS_TRACE_STREAM','true').strip().lower() in ('1','true','yes','on')
TRACE_DIR=os.getenv('JARVIS_TRACE_DIR',str(Path(__file__).resolve().parent.parent / 'data' / 'traces'))
AUTO_ENABLED=os.getenv('JARVIS_AUTO_ENABLED','true').strip().lower() in ('1','true','yes','on')
AUTO_RESUME_SECONDS=float(os.getenv('JARVIS_AUTO_RESUME_SECONDS','30'))


def chat_model():
    if CHAT_PROVIDER == 'ollama':
        return MODEL
    if CHAT_PROVIDER == 'deepseek':
        return DEEPSEEK_MODEL
    raise ValueError('JARVIS_CHAT_PROVIDER 必须是 ollama 或 deepseek')
