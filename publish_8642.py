import sys, base64, time, json
sys.path.insert(0, '/opt/data/voice-assistant')
from coolify import call

SVC = 'kl7hbed36wlg9vcxudhm8jco'

# 1. Read current compose
st, d = call('/services/' + SVC)
raw = d.get('docker_compose_raw', '')
print('ORIGINAL compose len:', len(raw))
assert 'hermes-agent:' in raw, 'hermes-agent service not found'

# 2. Inject ports into the hermes-agent service block only.
# The block starts after "services:\n  hermes-agent:\n" and runs until the
# next top-level "  <name>:" at col-2 (e.g. "  hermes-webui:\n").
key = '    environment:\n      - HERMES_HOME=/home/hermes/.hermes\n'
assert key in raw, 'hermes-agent environment anchor not found'

# Insert a ports block right after the hermes-agent volumes/healthcheck? The
# safest insertion point that keeps hermes-agent structurally intact: add
# '    ports:\n      - "\'8642:8642\'"\n' immediately before '    healthcheck:'.
anchor = '    healthcheck:\n'
assert anchor in raw, 'healthcheck anchor not found'

# Insert AFTER the volumes block but location-agnostic: place ports just before
# healthcheck (valid compose: ports and healthcheck are both service keys).
new = raw.replace(anchor, "    ports:\n      - '8642:8642'\n" + anchor, 1)

# Guard against double-injection
if new.count("'8642:8642'") > 1:
    print('ABORT: would inject twice, count =', new.count("'8642:8642'"))
    sys.exit(2)

print('NEW compose len:', len(new))
print('ports injected on hermes-agent:', "'8642:8642'" in new)

# 3. PATCH compose (base64 required)
b64 = base64.b64encode(new.encode()).decode()
st, d = call('/services/' + SVC, 'PATCH', {'docker_compose_raw': b64})
print('PATCH /services status:', st, str(d)[:180])
