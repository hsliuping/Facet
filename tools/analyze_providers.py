import json

# Analyze overlay providers
print("=" * 60)
print("OVERLAY: data/providers.json")
print("=" * 60)

with open('d:\\code\\Facet\\data\\providers.json', 'r') as f:
    data = json.load(f)
providers = data['providers']
print(f'Total providers in overlay: {len(providers)}')
for p in providers:
    pid = p['id']
    name = p['name']
    modes = p.get('modes', {})
    print(f'\n--- {pid} ({name}) ---')
    for mode_key, mode in modes.items():
        tier = mode.get('tier', 'N/A')
        label = mode.get('label', 'N/A')
        billing = mode.get('billing', 'NOT SET')
        has_free_quota = 'free_quota' in mode
        print(f'  mode: {mode_key}, tier: {tier}, label: {label}, billing: {billing}, has_free_quota: {has_free_quota}')

# Analyze builtin providers
print("\n" + "=" * 60)
print("BUILTIN: src/facet/gateway/providers.json")
print("=" * 60)

with open('d:\\code\\Facet\\src\\facet\\gateway\\providers.json', 'r') as f:
    data = json.load(f)
providers = data['providers']
print(f'Total providers in builtin: {len(providers)}')
for p in providers:
    pid = p['id']
    name = p['name']
    modes = p.get('modes', {})
    print(f'\n--- {pid} ({name}) ---')
    for mode_key, mode in modes.items():
        tier = mode.get('tier', 'N/A')
        label = mode.get('label', 'N/A')
        billing = mode.get('billing', 'NOT SET')
        has_free_quota = 'free_quota' in mode
        print(f'  mode: {mode_key}, tier: {tier}, label: {label}, billing: {billing}, has_free_quota: {has_free_quota}')