import json

with open('d:\\code\\Facet\\data\\providers.json', 'r') as f:
    data = json.load(f)
providers = data['providers']
print(f'Total providers: {len(providers)}')
for i, p in enumerate(providers):
    pid = p['id']
    name = p['name']
    modes = p.get('modes', {})
    print(f'\n--- {pid} ({name}) ---')
    for mode_key, mode in modes.items():
        tier = mode.get('tier', 'N/A')
        label = mode.get('label', 'N/A')
        billing = mode.get('billing', 'NOT SET')
        print(f'  mode: {mode_key}, tier: {tier}, label: {label}, billing: {billing}')