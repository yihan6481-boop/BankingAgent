"""Native runtime model gateway, using the existing external provider configuration."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
STATE=Path.home()/'.banking-agent/r4_trial_01'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',choices=['runtime'],default='runtime')
    parser.parse_args()
    for line in (STATE/'gateway.env').read_text('utf8').splitlines():
        if '=' in line:
            k,v=line.split('=',1);os.environ[k]=v
    overall=dict(profile='runtime',batch_id='R4_MEMORY_RUNTIME_01',ledger=str(STATE/'memory_runtime.json'),max_requests=None,max_tokens=None)
    config=STATE/'overall_host_runtime.json'
    config.write_text(json.dumps(overall,indent=2),'utf8')
    os.environ['BA_LLM_BUDGET_CONFIG']=str(config)
    os.environ.pop('R4_PHASE_CONFIG',None)
    sys.path.insert(0,str(ROOT/'deploy/r4'))
    import uvicorn
    uvicorn.run('model_gateway:app',host='127.0.0.1',port=18761,access_log=False,log_level='warning')

if __name__=='__main__':main()
