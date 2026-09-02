import sys, json, os
sys.path.insert(0, '/opt/data/voice-assistant')
from coolify import call

APP = 'gfnyhjybqzsakfn3gowdfp5i'
TOK = '/home/hermes/.hermes/home/.tokens'

def read_secret(path):
    with open(path) as f:
        return f.read().strip()

groq = read_secret(os.path.join(TOK, 'groq_api_key'))
# API_SERVER_KEY lives in .env under a different name; read from hermes .env
apikey = None
env_path = '/home/hermes/.hermes/.env'
if os.path.exists(env_path):
    for line in open(env_path):
        if line.startswith('API_SERVER_KEY='):
            apikey = line.split('=', 1)[1].strip()
if not apikey:
    # fallback: read from the coolify app envs (we only read what we need)
    print('NO API_SERVER_KEY found in .env — will keep existing value')

# 1. List envs, delete every entry
st, d = call(f'/applications/{APP}/envs')
envs = d if isinstance(d, list) else d.get('envs', [])
print('before delete:', len(envs))
deleted = 0
for e in envs:
    uid = e.get('uuid') or e.get('id')
    if uid:
        st2, _ = call(f'/applications/{APP}/envs/{uid}', 'DELETE')
        deleted += 1
print('deleted:', deleted)

# 2. Confirm 0
st, d = call(f'/applications/{APP}/envs')
envs = d if isinstance(d, list) else d.get('envs', [])
print('after delete:', len(envs))
assert len(envs) == 0, 'envs not fully cleared'

# 3. Re-add each, one at a time
desired = [
    ('GROQ_API_KEY', groq, True),
    ('API_SERVER_KEY', apikey, True),
    ('API_SERVER_HOST', '169.58.74.130', True),
    ('API_SERVER_PORT', '8642', True),
    ('BRAIN_MODEL', 'hermes-agent', True),
    ('TTS_VOICE', 'en-US-AriaNeural', True),
    ('PORT', '8000', True),
]
for k, v, lit in desired:
    if v is None:
        print('SKIP', k, '(no value)')
        continue
    st2, d2 = call(f'/applications/{APP}/envs', 'POST', {'key': k, 'value': v, 'is_literal': lit})
    print('add', k, '->', st2)

# 4. Verify dedup
st, d = call(f'/applications/{APP}/envs')
envs = d if isinstance(d, list) else d.get('envs', [])
from collections import Counter
cnt = Counter(e.get('key') for e in envs)
print('final count:', len(envs))
dupes = {k: v for k, v in cnt.items() if v > 1}
print('duplicates:', dupes if dupes else 'NONE')
