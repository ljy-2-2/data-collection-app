"""Manual variant of the same fixed privileged worker; no second owner ledger."""
import json
import time
from .kuavo5_manual_control import PROTOCOL, validate_command
from .kuavo5_session_worker import SessionWorker


class ManualWorker(SessionWorker):
    def send_input(self, packet, *, absolute_deadline=None):
        if self.process is None or self._input_closed or self.poll() is not None:
            raise RuntimeError('manual worker input unavailable')
        if set(packet) != {'event','challenge_id','command'} or packet['event'] != 'manual_input':
            raise ValueError('invalid manual input')
        validate_command(packet['command'])
        if not isinstance(packet['challenge_id'],str) or len(packet['challenge_id']) != 32:
            raise ValueError('invalid manual challenge')
        deadline = self.clock()+.05 if absolute_deadline is None else absolute_deadline
        if deadline <= self.clock(): raise TimeoutError('manual input expired')
        if self._pending:
            self.request_close()
            raise BufferError('manual input backpressure; no retry')
        data = (json.dumps(packet,allow_nan=False)+'\n').encode()
        if len(data)>4096: raise ValueError('manual input too large')
        self._pending.append([data,0,deadline]); self._flush_input()

    @property
    def cleanup_confirmed(self):
        p = self.closed_report or {}
        if p.get('transport',{}).get('worker_start_refused') is True:
            return super().cleanup_confirmed
        return (self.process is not None and self.output_drained and self.cleanup_error is None
                and self.poll() == 0 and p.get('event') == 'closed' and p.get('exit_code') == 0
                and p.get('manual_protocol') == PROTOCOL and p.get('cleanup_confirmed') is True)

    def cleanup_diagnostics(self):
        return dict(worker_exit_code=self.poll(),output_drained=self.output_drained,
            closed_report_present=self.closed_report is not None,
            failed_checks=[] if self.cleanup_confirmed else ['manual_cleanup_unconfirmed'],
            stderr_summary=(self.closed_report or {}).get('error',''))
