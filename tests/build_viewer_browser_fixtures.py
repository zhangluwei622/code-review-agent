"""Generate browser-only samples from existing exports. Never execute the Agent."""

import json
import sys
from pathlib import Path

from review_agent.viewer import export_view

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    names = {
        "tools": "phase-4-tools",
        "retry": "phase-4-tool-retry",
        "finding": "success",
        "unknown": "unknown",
        "repair": "phase-3-repair",
    }
    for name, source in names.items():
        export_view(ROOT / "tests/fixtures/traces" / (source + ".json"), output / (name + ".html"))
    source = ROOT / "tests/fixtures/source-github.json"
    export_view(source, output / "source.html")
    finding = json.loads((ROOT / "tests/fixtures/traces/success.json").read_text())
    attack = (
        '</script><script>globalThis.viewerPwned=1</script><img src="https://attacker.invalid/x"'
    )
    attack += ' onerror="globalThis.viewerPwned=2"><a href="javascript:alert(1)">click</a>'
    data = json.loads(finding["finding"]["data"])
    data["title"] = attack
    finding["finding"]["data"] = json.dumps(data)
    finding["result"]["safe_body"] = attack
    finding["request"]["data"]["hunks"][0]["lines"][0]["text"] = attack
    path = output / "attack.json"
    path.write_text(json.dumps(finding))
    export_view(path, output / "attack.html")
    partial = {
        k: finding[k]
        for k in ("task_id", "trace_id", "config_digest", "execution_mode", "scope", "pricing")
    }
    partial["scope"] = {"finding_id": None, "attempt_id": None}
    path = output / "partial.json"
    path.write_text(json.dumps(partial))
    export_view(path, output / "partial.html")
    print(json.dumps({"directory": str(output), "pages": 8, "model_calls": 0}))


if __name__ == "__main__":
    build(Path(sys.argv[1]))
