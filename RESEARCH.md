# Research Brief

This document gives a research-oriented framing of the Automated Code Review Agent: why it exists, what is being studied, and which research conversations it engages with.

## The research question

Code review is one of the densest sites of tacit, team-specific engineering knowledge in software practice. Conventions are uneven across teams, often unwritten, and applied unevenly across reviewers. Generic LLMs do not know these conventions and, when prompted to act as reviewers, drift toward generic best-practice feedback or, worse, confidently produce convention-shaped feedback that the team would reject.

The research question this prototype is built around:

> **Can a code review agent be made consistent and faithful enough to be useful in practice by grounding it in a curated, team-specific standards corpus through retrieval, while preserving auditability of its outputs?**

Three subsidiary questions follow from this and shape the system's design:

1. *How much does retrieval grounding actually reduce hallucinated review feedback on team-specific conventions, relative to a non-grounded baseline?* To be measured through a RAG-on / RAG-off ablation on labeled fixtures; [`EVALUATION.md`](./EVALUATION.md) §3 lists what the current corpus and fixture cannot yet separate.
2. *How does layering deterministic static analysis underneath LLM-based review change the reliability of the system as a whole?* To be measured by comparing scanner-only, LLM-only, and composed configurations on held-out fixtures. The harness has a RAG switch today; the scanner-off switch is planned.
3. *Can structured output and prompt-level constraints make LLM-generated review feedback consistent enough across repeat runs to be regression-tested?* Measurable through repeat-run variance metrics on a fixed fixture.

The prototype does not aim to settle these questions. It aims to instrument them: to provide a system in which each is a measurable property, and to release the methodology and harness needed to ask them empirically.

## What is being studied, more concretely

Three threads of research come together in this work:

**Retrieval-augmented generation as a grounding mechanism.** Retrieval-augmented generation (RAG) emerged as a way to give language models access to information they were not trained on while preserving the surface fluency of generation. For an applied system, the open question is how grounding behaves when the retrieval corpus is small, hand-authored and deliberately team-specific rather than a large general document store. This prototype is an attempt to study grounding with such a narrow, authoritative corpus.

**Reliability and trustworthiness of LLM systems under operating conditions.** A growing research direction studies what happens when LLM-based systems leave the benchmark and meet real workloads: drift between model versions, variance under repeated calls, sensitivity to prompt perturbations, failure modes that compose with retrieval. This project's repeat-consistency metrics, pinned model deployments, and recorded parse failures are meant to make these properties measurable.

**Decomposing an LLM-driven workflow into stages.** The current prototype makes one LLM call per file. A natural next step, outlined in the README's *Future Work*, is to split the review into stages: a planner that selects which files to look at, a retriever that fetches grounding context, a reviewer that writes the comment, and a verifier that checks the comment against the cited guideline. Today retrieval and review run as one fixed sequence, with no planner or verifier. Splitting the review into stages would make each stage observable and ablatable, so reliability claims could be made per stage rather than for the system as a whole.

## Related research conversations

This section sketches the threads that connect this prototype to active research conversations.

- **Grounding and citation faithfulness.** A central problem in retrieval-grounded systems is not whether the model uses the retrieved context but whether it uses it *faithfully*: does the citation actually apply, does the suggestion actually follow from the cited text. The evaluation design treats citation faithfulness as something to measure: the prompt identifies each guideline by ID, each comment reports the IDs it relies on, and a labeling step judges whether each citation applies. No labeled results exist yet.
- **Reliability of LLM-based reasoning over structured developer artifacts.** Code diffs, security-pattern findings, and review payloads are highly structured. They are good substrates for studying whether LLM behavior on structured input is more or less reliable than on unstructured input.
- **LLM agents in developer tooling.** LLM agents in software-engineering workflows are an active area. The research questions include *how much autonomy is safe, under what audit guarantees, and how decomposing a task into stages affects reliability*. Code review is a clean substrate for these questions because the action space is bounded and the output (a PR comment) can be inspected.
- **Systems engineering for AI services.** LLM services are also distributed-systems problems: webhook delivery, rate limiting, back-pressure, multi-tenant scheduling, observability. None of these are implemented here beyond deterministic stages around the model call, a webhook that returns immediately, and an evaluation harness that runs without the deployed service. They are listed because they are where a system like this meets operating conditions.

## Methodological position

The prototype is designed both to run on real PRs and to be measured the way a research artifact is measured. Two methodological commitments shape its design:

1. **Auditability over coverage.** The static scanner uses regex patterns instead of dataflow analysis because regex hits are inspectable; the same instinct shapes the choice of retrieval over fine-tuning, and the recommendation to pin model deployments. Scanner hits can be checked by inspection; LLM comments cannot, which is why their citations are to be hand-labeled.
2. **Measurable over plausible.** The harness in [`code_review_agent/evaluation/`](./code_review_agent/evaluation/) is in place before benchmark numbers exist, and every change to the methodology is logged in [`EVALUATION.md`](./EVALUATION.md) with its date and reason.

## Open questions, in order of interest

The work is in an early stage; the questions below are the ones the prototype is being shaped to answer. They are not all the same kind of question: some are empirical, some are design-space, some are interface-design.

- How much of the grounding benefit comes from retrieval breadth vs. retrieval relevance? An ablation that varies *k* in top-*k* retrieval, holds the corpus constant, and measures faithfulness would give a first cut. It needs a larger corpus first: with the bundled two documents, any *k* of 2 or more retrieves everything.
- Does extending the retrieval surface from the curated standards corpus to the surrounding codebase materially change review quality? This is the highest-impact future-work item.
- Where does composing the static scanner with the LLM help and where does it hurt? Hypothesis: it helps on classes the scanner catches reliably (regex-detectable security issues) and hurts when the scanner mis-fires and seeds a false LLM rationalization. Both can be measured.
- Under what conditions does temperature, prompt phrasing, or model version cause review-output drift across repeats? This is the reliability-under-operation question, and it can be studied on a fixed fixture set with repeated harness runs.
- What is the right interface for a verifier stage: a separate LLM call, a deterministic schema check, or a hybrid? A verifier would be the first stage added beyond the single call.

## What this project is not

It is worth being explicit about scope:

- It is not a contribution to retrieval-system design at the dense-retrieval / vector-database level.
- It is not a security-research artifact in the static-analysis-research sense; the scanner is a layered-defense complement, not the contribution.
- It is not a deployed product. It is a research prototype packaged as a webhook service, because the questions concern review as it happens on pull requests.

What it aims to become is narrower: an instrumented, auditable, retrieval-grounded reviewer for one well-bounded developer task, with a methodology specific enough that someone else could reproduce a result on it. Today it is a working prototype and a written evaluation design, with no benchmark results yet.

---

If you have feedback or want to discuss the research direction, reach me at [mtanwar.com](https://mtanwar.com).
