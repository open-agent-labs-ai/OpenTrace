# Example OpenTrace traces

Ready-to-open `.ot.jsonl` files. Load any of them in [`../viewer/index.html`](../viewer/)
or inspect with `opentrace tree <file>` / `opentrace stats <file>`.

| File | What it shows |
|------|---------------|
| `minimal.ot.jsonl` | A single LLM call, non-agentic, **batch** profile (spec §B.1). |
| `research-agent.ot.jsonl` | The full demo: multi-turn, planning, router, RAG, tool loop with an error+retry, sub-agent handoff, guardrail, evaluator, human approval — **streaming** profile. Produced by [`../demo/research_agent_demo.py`](../demo/research_agent_demo.py). |

Regenerate the research-agent trace with:

```bash
python3 ../demo/research_agent_demo.py
```
