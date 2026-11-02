"""Print only ASR configuration status. Does not connect to DB, microphone or provider.
Run inside bank container: python /app/deploy/server/voice_check.py --config /run/private/server.json
This checks the effective configuration for a fresh startup, not a live ASR request.
"""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'backend')]
from deploy.server.settings import read_json,environment_file,install
from voice_transcription import capabilities

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    try:
        config=read_json(args.config);values=environment_file(config['files']['model_environment'])
        names=['BA_ASR_ENABLED','BA_ASR_ENDPOINT','BA_ASR_MODEL','BA_ASR_API_KEY']
        presence={k:bool(values.get(k,'')) for k in names}
        install(config)
        print(json.dumps({'check':'effective_configuration_only','private_config_fields_present':presence,'capabilities':capabilities(),'provider_request_performed':False},ensure_ascii=False))
    except Exception:
        print(json.dumps({'check':'effective_configuration_only','error':'voice_config_check_failed','provider_request_performed':False}));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
