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

output_lines = []
output_lines.append('=== DETAILED LINK ANALYSIS ===')

for p in all_docs:
    if not (p.endswith('.md') or p.endswith('.html')):
        continue
    content, src = get_content(p)
    if not content:
        continue

    lines = content.splitlines()
    for idx, line in enumerate(lines, 1):
        for m in link_pat.finditer(line):
            text, url = m.group(1), m.group(2).strip()
            url_clean = url.split('#')[0].split('?')[0].strip()
            if not url_clean:
                continue
            if url.startswith('http://') or url.startswith('https://') or url.startswith('mailto:'):
                continue

            doc_dir = os.path.dirname(p)

            if url.startswith('file:'):
                output_lines.append(
                    f'[FILE_URL] {p}:{idx} (src={src}) -> text: "{text}", url: "{url}"'
                )
            else:
                norm_target = os.path.normpath(os.path.join(doc_dir, url_clean)).replace('\\', '/')
                if norm_target.startswith('docs/') or norm_target == 'docs':
                    # Internal to docs
                    if norm_target not in all_docs:
                        # Check if directory
                        is_dir = any(x.startswith(norm_target + '/') for x in all_docs)
                        output_lines.append(
                            f'[INTERNAL_UNRESOLVED] {p}:{idx} (src={src}) -> text: "{text}", url: "{url}", resolved: "{norm_target}" (is_dir={is_dir})'
                        )
                else:
                    # Escapes docs/
                    # Check if exists in full repo
                    in_repo = norm_target in all_repo_files or any(
                        x.startswith(norm_target + '/') for x in all_repo_files
                    )
                    output_lines.append(
                        f'[ESCAPES_DOCS] {p}:{idx} (src={src}) -> text: "{text}", url: "{url}", resolved: "{norm_target}" (in_repo={in_repo})'
                    )

out_file = r'C:\Claude\NCE_DOCS_STAGING\scratch_link_analysis_full.txt'
with open(out_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(output_lines))

print(f'Done. Wrote {len(output_lines)} lines to {out_file}')
