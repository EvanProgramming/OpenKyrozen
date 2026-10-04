"""Persistent, conservative CNY ceiling for one audit process and its threads.

Default rates bound DeepSeek Pro cache-miss / peak pricing. The caller can
use verified offpeak rates inside a guarded pricing window:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
No prompts, credentials, or responses are retained. Do not share one ledger
between separate simultaneous audit processes.
"""
import json
from pathlib import Path
import threading
import time
import uuid

class AuditBudget:
    def __init__(self,path,limit=4.8,rates=(9,27),wait_seconds=0):
        self.path=Path(path)
        self.limit=limit
        self.rates=rates
        self.lock=threading.Lock()
        self.condition=threading.Condition(self.lock)
        self.active=set()
        self.wait_seconds=wait_seconds
        self.entries=json.loads(self.path.read_text())['entries'] if self.path.exists() else {}

    @property
    def total(self):
        return sum(item['cny_upper_bound'] for item in self.entries.values())

    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.next')
        temporary.write_text(json.dumps({'limit_cny':self.limit,'total_cny_upper_bound':self.total,
            'rates_cny_per_million':{'input':self.rates[0],'output':self.rates[1]},'entries':self.entries},indent=2))
        temporary.replace(self.path)

    def reserve(self,input_bound,output_bound):
        cost=(input_bound*self.rates[0]+output_bound*self.rates[1])/1_000_000
        deadline=time.monotonic()+self.wait_seconds
        with self.condition:
            while self.total+cost>self.limit:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not self.active:
                    raise RuntimeError('Live audit RMB cost budget exhausted')
                self.condition.wait(remaining)
            ticket=uuid.uuid4().hex
            self.entries[ticket]={'cny_upper_bound':cost,'input_bound':input_bound,
                'output_bound':output_bound,'usage_verified':False}
            self.active.add(ticket)
            self._save()
            return ticket

    def settle(self,ticket,input_tokens,output_tokens):
        with self.condition:
            self.active.discard(ticket)
            self.condition.notify_all()
            if input_tokens is None or output_tokens is None:
                return
            item=self.entries[ticket]
            if not (0<=input_tokens<=item['input_bound'] and 0<=output_tokens<=item['output_bound']):
                raise RuntimeError('Provider usage exceeded the reserved token bounds')
            item.update(cny_upper_bound=(input_tokens*self.rates[0]+output_tokens*self.rates[1])/1_000_000,
                        input_tokens=input_tokens,output_tokens=output_tokens,usage_verified=True)
            self._save()
