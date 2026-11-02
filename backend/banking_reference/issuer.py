"""Offline issuer hints, never account ownership/payment authorization."""
from pathlib import Path
from functools import lru_cache
import json
@lru_cache(maxsize=1)
def reference():return json.loads(Path(__file__).with_name('card_bins.json').read_text('utf8'))
def identify(number):
    cfg=reference()
    matches=[r for r in cfg['entries'] if len(number)==r['length'] and number.startswith(r['prefix'])]
    if not matches:return dict(bank_name=None,source='bin_reference',identity_verified=False,coverage='unknown')
    longest=max(len(r['prefix']) for r in matches);matches=[r for r in matches if len(r['prefix'])==longest]
    names={r['bank_name'] for r in matches}
    return dict(bank_name=next(iter(names)) if len(names)==1 else None,source='bin_reference',identity_verified=False,
                coverage='matched' if len(names)==1 else 'ambiguous',reference_source=cfg['source'],reference_version=cfg['bin_blob'])
