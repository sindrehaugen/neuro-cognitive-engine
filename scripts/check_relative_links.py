import os
import re
import subprocess

repo_path = r'C:\Users\SindreLøvlieHaugen\Documents\systemer\Neuro-Cognitive Engine\NCE-Main'
staging_path = r'C:\Claude\NCE_DOCS_STAGING\docs'

# Get all files in git at 7304330
cmd = ['git', '-C', repo_path, 'ls-tree', '-r', '--name-only', '7304330']
res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
all_repo_files = set(res.stdout.strip().splitlines())

git_docs = [f for f in all_repo_files if f.startswith('docs/')]

staging_docs = []
for root, dirs, files in os.walk(staging_path):
    for f in files:
        rel = os.path.relpath(os.path.join(root, f), staging_path)
        staging_docs.append('docs/' + rel.replace('\\', '/'))

all_docs = sorted(set(git_docs + staging_docs))


def get_content(p):
    stg_p = os.path.join(r'C:\Claude\NCE_DOCS_STAGING', p.replace('/', os.sep))
    if os.path.exists(stg_p):
        with open(stg_p, encoding='utf-8', errors='ignore') as f:
            return f.read(), 'staging'
    cmd = ['git', '-C', repo_path, 'show', f'7304330:{p}']
    res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    if res.returncode == 0:
        return res.stdout, 'git'
    return '', 'none'


link_pat = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')

print(f'Total docs: {len(all_docs)}')
for p in all_docs:
    if not p.endswith('.md'):
        continue
    content, src = get_content(p)
    if not content:
        continue

    lines = content.splitlines()
    for idx, line in enumerate(lines, 1):
        for m in link_pat.finditer(line):
            text, url = m.group(1), m.group(2).strip()
            # print all relative links pointing to other files
            if (
                not url.startswith('http')
                and not url.startswith('mailto:')
                and not url.startswith('#')
                and not url.startswith('file:')
            ):
                url_clean = url.split('#')[0].split('?')[0].strip()
                if not url_clean:
                    continue
                doc_dir = os.path.dirname(p)
                norm_target = os.path.normpath(os.path.join(doc_dir, url_clean)).replace('\\', '/')
                if norm_target in all_docs:
                    pass  # valid docs link
                elif norm_target in all_repo_files:
                    print(f'DOCS_TO_CODE: {p}:{idx} -> [{text}]({url}) -> {norm_target}')
                else:
                    is_dir = any(x.startswith(norm_target + '/') for x in all_docs) or any(
                        x.startswith(norm_target + '/') for x in all_repo_files
                    )
                    print(
                        f'NOT_FOUND: {p}:{idx} -> [{text}]({url}) -> {norm_target} (is_dir={is_dir})'
                    )
