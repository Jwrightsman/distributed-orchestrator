"""Bounded public generation requirements shared by production strategies.

Evaluator expectations, reference answers and fixtures are never inputs here.
The canonical output contract is public to every participating generator.
"""

import json

from execution.contracts import OutputContractV1


MAX_GENERATION_BRIEF_BYTES = 96 * 1024


def generation_brief(task: str, contract: OutputContractV1 | None) -> str:
    if contract is None:
        return task
    # Explicit public projection: do not serialize an entire request or an
    # evaluator item when extending this function. Preserve schema constraints
    # intact; truncating them would silently change the generation requirements.
    public = {
        "kind": contract.kind,
        "artifact_count": contract.artifact_count,
        "format": contract.format,
        "required_files": contract.required_files,
        "json_schema": contract.json_schema,
        "validators": [item.model_dump(mode="json") for item in contract.validators],
    }
    brief = task + "\n\n## Output contract\n" + json.dumps(
        public, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    if len(brief.encode("utf-8")) > MAX_GENERATION_BRIEF_BYTES:
        raise ValueError("public generation brief exceeds its byte budget")
    return brief
