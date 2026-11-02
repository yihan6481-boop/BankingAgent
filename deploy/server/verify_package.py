"""Offline extracted-package integrity check; manifest itself is excluded by design."""
from pathlib import Path
import json,hashlib,sys
root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
manifest=json.loads((root/'MANIFEST.json').read_text('utf8'))
for row in manifest['files']:
    p=(root/row['path']).resolve()
    if not p.is_relative_to(root) or not p.is_file():raise SystemExit('missing_or_invalid_package_path')
    if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256'] or p.stat().st_size!=row['bytes']:raise SystemExit('package_file_mismatch')
print('package_files_verified='+str(len(manifest['files'])))
