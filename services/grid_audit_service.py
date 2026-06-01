import json
import time
from pathlib import Path
from typing import Any, Dict


class GridAuditService:
    def __init__(self, root: str = "data/usdc_ai_grid_audit"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_record(self, action: str, payload: Dict[str, Any]) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = self.root / f"{day}.jsonl"
        record = {"timestamp": time.time(), "action": action, "payload": payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return path

