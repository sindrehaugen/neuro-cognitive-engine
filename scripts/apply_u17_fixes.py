import os
import re
import subprocess

repo_path = r'C:\Users\SindreLøvlieHaugen\Documents\systemer\Neuro-Cognitive Engine\NCE-Main'
staging_dir = r'C:\Claude\NCE_DOCS_STAGING'


def get_base_content(rel_path):
    stg_p = os.path.join(staging_dir, rel_path.replace('/', os.sep))
    if os.path.exists(stg_p):
        with open(stg_p, encoding='utf-8') as f:
            return f.read()
    cmd = ['git', '-C', repo_path, 'show', f'7304330:{rel_path}']
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
    return res.stdout


def write_staging_file(rel_path, content):
    stg_p = os.path.join(staging_dir, rel_path.replace('/', os.sep))
    os.makedirs(os.path.dirname(stg_p), exist_ok=True)
    with open(stg_p, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'Wrote {rel_path} ({len(content)} bytes)')


# 1. docs/README.md
readme = get_base_content('docs/README.md')
readme = readme.replace(
    '[`vertical_engines/`](vertical_engines/)',
    '[`vertical_engines/`](vertical_engines/00-ENGINES-ROADMAP.md)',
)
readme = readme.replace(
    '[**Architecture Decision Records**](adr/)',
    '[**Architecture Decision Records**](adr/README.md)',
)
write_staging_file('docs/README.md', readme)

# 2. docs/_navbar.md
navbar = """<!-- docs/_navbar.md -->

- [Home](README.md)
- [API Reference](API.md)
- [API Payload Specifications](usage_modes.md)
- [IT Admin Guide](it_admin_guide.md)
- [GitHub Repository](https://github.com/sindrehaugen/NCE)
"""
write_staging_file('docs/_navbar.md', navbar)

# 3. docs/database_architecture.md
# (already modified in staging, let's make sure it's clean)
db_arch = get_base_content('docs/database_architecture.md')
db_arch = re.sub(
    r'file:///C:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Main/nce/schema\.sql',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/schema.sql',
    db_arch,
)
db_arch = re.sub(
    r'file:///C:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Main/nce/migrations/?',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/migrations/',
    db_arch,
)
write_staging_file('docs/database_architecture.md', db_arch)

# 4. docs/engines/product-admin.md
prod_admin = get_base_content('docs/engines/product-admin.md')
prod_admin = re.sub(
    r'\*\*Verified-against:\*\*\s*[a-f0-9]+', '**Verified-against:** 7304330', prod_admin
)
# Replace file:/// links
prod_admin = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/vertical_modules/product/_guard\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/product/_guard.py',
    prod_admin,
    flags=re.IGNORECASE,
)
prod_admin = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/vertical_modules/product/sources/nettailer\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/product/sources/nettailer.py',
    prod_admin,
    flags=re.IGNORECASE,
)
prod_admin = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/vertical_modules/product/enrich\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/product/enrich.py',
    prod_admin,
    flags=re.IGNORECASE,
)
prod_admin = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/vertical_modules/product/watchers\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/product/watchers.py',
    prod_admin,
    flags=re.IGNORECASE,
)
prod_admin = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/vertical_modules/product/pricing\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/product/pricing.py',
    prod_admin,
    flags=re.IGNORECASE,
)
write_staging_file('docs/engines/product-admin.md', prod_admin)

# 5. docs/engines/project-user.md
proj_user = get_base_content('docs/engines/project-user.md')
proj_user = proj_user.replace(
    '../../nce/vertical_modules/project/phase_gates.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/phase_gates.py',
)
proj_user = proj_user.replace(
    '../../nce/vertical_modules/project/convert.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/convert.py',
)
proj_user = proj_user.replace(
    '../../nce/vertical_modules/project/tasks.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/tasks.py',
)
proj_user = proj_user.replace(
    '../../nce/vertical_modules/project/automation.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/automation.py',
)
proj_user = proj_user.replace(
    '../../nce/vertical_modules/project/baseline.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/project/baseline.py',
)
write_staging_file('docs/engines/project-user.md', proj_user)

# 6. docs/netbox_and_cognitive_extensions.md
netbox = get_base_content('docs/netbox_and_cognitive_extensions.md')
netbox = re.sub(r'\*\*Verified-against:\*\*\s*[a-f0-9]+', '**Verified-against:** 7304330', netbox)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/atms\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/atms.py',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/causal/chrono\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/causal/chrono.py',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/graph_query\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/graph_query.py',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/analytics/stress\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/analytics/stress.py',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/active_learning\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/active_learning.py',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/nce/vertical_modules/netbox/?',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/netbox/',
    netbox,
    flags=re.IGNORECASE,
)
netbox = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/[^/]+/src/nce-netbox-plugin/nce_netbox_plugin/api/views\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/src/nce-netbox-plugin/nce_netbox_plugin/api/views.py',
    netbox,
    flags=re.IGNORECASE,
)
write_staging_file('docs/netbox_and_cognitive_extensions.md', netbox)

# 7. docs/quick_start.md
quick_start = get_base_content('docs/quick_start.md')
quick_start = re.sub(
    r'\*\*Verified-against:\*\*\s*[a-f0-9]+', '**Verified-against:** 7304330', quick_start
)
quick_start = quick_start.replace(
    '../deploy/README.md', 'https://github.com/sindrehaugen/NCE/blob/main/deploy/README.md'
)
write_staging_file('docs/quick_start.md', quick_start)

# 8. docs/shared-core/pricing-signing-grounding.md
psg = get_base_content('docs/shared-core/pricing-signing-grounding.md')
psg = re.sub(r'\*\*Verified-against:\*\*\s*[a-f0-9]+', '**Verified-against:** 7304330', psg)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/pricing/dg\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/pricing/dg.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/pricing/resolver\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/pricing/resolver.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/config\.py#L1001',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/config.py#L1001',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/pricing/mcp_handlers\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/pricing/mcp_handlers.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/signing_service/transport\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/signing_service/transport.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/signing_service/manual\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/signing_service/manual.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/structural/grounded\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/structural/grounded.py',
    psg,
    flags=re.IGNORECASE,
)
psg = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/nce/structural/no_person_grain\.py',
    'https://github.com/sindrehaugen/NCE/blob/main/nce/structural/no_person_grain.py',
    psg,
    flags=re.IGNORECASE,
)
write_staging_file('docs/shared-core/pricing-signing-grounding.md', psg)

# 9. docs/shared-core/source-mode-divergence.md
smd = get_base_content('docs/shared-core/source-mode-divergence.md')
smd = re.sub(r'\*\*Verified-against:\*\*\s*[a-f0-9]+', '**Verified-against:** 7304330', smd)
smd = re.sub(
    r'file:///c:/Users/[^/]+/Documents/systemer/[^/]+/NCE-Docs/docs/DATA_SOURCE_MODES\.md',
    '../DATA_SOURCE_MODES.md',
    smd,
    flags=re.IGNORECASE,
)
write_staging_file('docs/shared-core/source-mode-divergence.md', smd)

print('All 9 files successfully processed and written to staging!')
