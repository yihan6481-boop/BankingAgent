"""Process-load fingerprint: no credentials, database rows or environment values."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]

def current_build():
    files = [p for p in (ROOT/'backend').rglob('*.py') if '__pycache__' not in p.parts and not p.name.startswith('test_')]
    files += [p for p in (ROOT/'frontend/dist').rglob('*') if p.is_file()]
    files += [ROOT/'manual/bill_vision_review.html', ROOT/'tools/run_integrated.py']
    values = [(p.relative_to(ROOT).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(files)]
    return {'project_id':hashlib.sha256(str(ROOT).encode()).hexdigest(),
            'build':hashlib.sha256(repr(values).encode()).hexdigest(),
            'ui_sha256':hashlib.sha256((ROOT/'frontend/dist/index.html').read_bytes()).hexdigest()}

LOADED_BUILD = current_build()
