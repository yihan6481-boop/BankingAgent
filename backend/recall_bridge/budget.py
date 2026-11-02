"""Acceptance-only phase reservation over the unchanged R3 total ledger."""
import json
from pathlib import Path
from dialogue.budget import Budget,BudgetError


class PhaseBudget(Budget):
    def __init__(self,phase_config):
        super().__init__()
        if self.profile!='acceptance':raise BudgetError('explicit_acceptance_budget_required')
        phase=json.loads(Path(phase_config).read_text('utf8'))
        if phase['profile']!='acceptance' or phase['max_requests']>120 or phase['max_tokens']>1_500_000:
            raise BudgetError('invalid_phase_budget')
        self.phase=Budget()
        self.phase.path=Path(phase['ledger']).resolve();self.phase.batch_id=phase['batch_id']
        self.phase.max_requests=phase['max_requests'];self.phase.max_tokens=phase['max_tokens']
        self.references={}

    def reserve(self,payload):
        phase_ref=self.phase.reserve(payload)
        total_ref=super().reserve(payload)
        self.references[total_ref]=phase_ref
        return total_ref

    def settle(self,rid,response=None,elapsed=0,error=None):
        super().settle(rid,response,elapsed,error)
        self.phase.settle(self.references[rid],response,elapsed,error)
