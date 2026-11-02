"""Rebuild only the two observed public CPU model caches at observed revisions."""
from pathlib import Path
import argparse
MODELS={
    'embeddings':('intfloat/multilingual-e5-small','614241f622f53c4eeff9890bdc4f31cfecc418b3'),
    'reranker':('cross-encoder/mmarco-mMiniLMv2-L12-H384-v1','1427fd652930e4ba29e8149678df786c240d8825')}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    from huggingface_hub import snapshot_download
    for name,(repo,revision) in MODELS.items():
        snapshot_download(repo_id=repo,revision=revision,local_dir=str(Path(a.output)/name))
        print(name+': pinned public model cache prepared')
