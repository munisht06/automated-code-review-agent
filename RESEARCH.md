# Research Brief

This document gives a research-oriented framing of the Automated Code Review Agent. It is intended for readers who care less about how to run the system and more about why it exists, what is being studied, and which research conversations it engages with.

## The research question

Code review is one of the densest sites of tacit, team-specific engineering knowledge in software practice. Conventions are uneven across teams, often unwritten, and applied unevenly across reviewers. Generic LLMs do not know these conventions and, when prompted to act as reviewers, drift toward generic best-practice feedback or — worse — confidently produce convention-shaped feedback that the team would reject.

The research question this prototype is built around:

> **Can a code review agent be made consistent and faithful enough to be useful in practice by grounding it in a curated, team-specific standards corpus through retrieval, while preserving auditability of its outputs?**

Three subsidiary questions follow from this and shape the system's design:

1. *How much does retrieval grounding actually reduce hallucinated review feedback on team-specific conventions, relative to a non-grounded baseline?* — measurable through RAG-on / RAG-off ablation on labeled fixtures.
2. *How does layering deterministic static analysis underneath LLM-based review change the reliability of the system as a whole?* — measurable by comparing scanner-only, LLM-only, and composed configurations against held-out fixture sets.
3. *Can structured output and prompt-level constraints make LLM-generated review feedback consistent enough across repeat runs to be regression-tested?* — measurable through repeat-run variance metrics on a fixed fixture.

The prototype does not aim to settle these questions. It aims to instrument them: to provide a system in which each is a measurable property, and to publish the methodology and harness needed to ask them empirically.

## What is being studied, more concretely

Three threads of research come together in this work:

**Retrieval-augmented generation as a grounding mechanism.** Retrieval-augmented generation (RAG) emerged as a way to give language models access to information they were not trained on while preserving the surface fluency of generation. The interesting open questions for an applied system are not whether RAG helps in the average case — there is now a substantial body of evidence that it does — but how grounding behavior changes when the retrieval corpus is small, hand-authored, and deliberately team-specific rather than scraped from a general document store. The current literature on retrieval grounding largely tests against open-domain QA; this prototype is one attempt to study grounding under a much narrower, much more authoritative corpus.

**Reliability and trustworthiness of LLM systems under operating conditions.** A growing research direction studies what happens when LLM-based systems leave the benchmark and meet real workloads — drift between model versions, variance under repeated calls, sensitivity to prompt perturbations, failure modes that compose with retrieval. This project's commitment to repeat-consistency metrics, version-pinning of model deployments, and explicit fall-through behavior on schema-validation failure is a deliberate engagement with that direction. The system is built to be measurable as a reliability artifact, not just an engineering one.

**Agentic decomposition of an LLM-driven workflow.** The current prototype is a single-LLM-call pipeline. The natural next step — outlined in the README's *Future Work* — is to decompose the review task across a small directed agent: a planner that selects which files to look at; a retriever that fetches grounding context; a reviewer that generates the comment; a verifier that checks the comment against the cited guideline. This decomposition is interesting precisely because each stage becomes observable and ablatable, which lets reliability claims be made at the stage level rather than the system level. The prototype is structured to make that decomposition a refactor rather than a rewrite.

## Why this is interesting to applied AI / NLP research

A faculty reader will recognize the research-direction adjacencies without me having to name them; this section sketches the threads that connect this prototype to active research conversations.

- **Grounding and citation faithfulness.** The hardest problem in retrieval-grounded systems is not whether the model uses the retrieved context but whether it uses it *faithfully* — does the citation actually apply, does the suggestion actually follow from the cited text. The evaluation framework in this repo treats citation faithfulness as a first-class measurable property, not as an emergent hope.
- **Reliability of LLM-based reasoning over structured developer artifacts.** Code diffs, security-pattern findings, and review payloads are highly structured. They are good substrates for studying whether LLM behavior on structured input is more or less reliable than on unstructured input — a question that comes up across applied NLP and applied AI safety literatures.
- **Agentic developer tooling.** The broader space of LLM agents in software-engineering workflows is increasingly active. The interesting research questions are about *how much autonomy is safe, under what audit guarantees, and how the agentic decomposition affects reliability*. A code review agent is a clean substrate for studying this because the action space is bounded and the output (a PR comment) is auditable by design.
- **Distributed-systems engineering for AI services.** Agentic LLM systems are not just NLP problems; they are also distributed-systems problems — webhook delivery, rate limiting, back-pressure, multi-tenant scheduling, observability. The system is built with that explicitly in mind: deterministic stages where possible, structured event semantics, an evaluation harness that does not depend on the deployed runtime. This connects the project to research on operationalizing AI systems at scale.

## Methodological position

This is an *applied study*, in the sense that the prototype is designed both to run on real PRs and to be measured in the way a research artifact is measured. Two methodological commitments shape every design decision:

1. **Auditability over coverage.** The static scanner uses regex patterns instead of dataflow analysis because regex hits are inspectable; the same instinct shapes the choice of retrieval over fine-tuning, and the choice to pin model deployments rather than auto-upgrade. Every claim the system makes about a piece of code should be defensible by inspection.
2. **Measurable over plausible.** The harness in [`code_review_agent/evaluation/`](./code_review_agent/evaluation/) is in place before benchmark numbers exist — because methodology defends results that do not yet exist far better than results defend methodology that did not.

## Open questions, in order of interest

The work is in an early stage; the questions below are the ones the prototype is being shaped to answer. They are not all the same kind of question — some are empirical, some are design-space, some are interface-design.

- How much of the grounding benefit comes from retrieval breadth vs. retrieval relevance? An ablation that varies *k* in top-*k* retrieval, holds the corpus constant, and measures faithfulness will give a first cut.
- Does extending the retrieval surface from the curated standards corpus to the surrounding codebase materially change review quality? This is the highest-impact future-work item.
- Where does composing the static scanner with the LLM help and where does it hurt? Hypothesis: it helps on classes the scanner catches reliably (regex-detectable security issues) and hurts when the scanner mis-fires and seeds a false LLM rationalization. Both can be measured.
- Under what conditions does temperature, prompt phrasing, or model version cause review-output drift across repeats? This is the reliability-under-operation question, and it is studyable on a fixed fixture set with a daily harness run.
- What is the right interface for a verifier stage in the agentic decomposition — a separate LLM call, a deterministic schema check, a hybrid? This is the question the next refactor will be designed to investigate.

## What this project is not

It is worth being explicit about scope:

- It is not a general-purpose LLM safety paper.
- It is not a contribution to retrieval-system design at the dense-retrieval / vector-database level.
- It is not a security-research artifact in the static-analysis-research sense — the scanner is a layered-defense complement, not the contribution.
- It is not (yet) a deployed product. It is a research prototype that happens to be engineered like a production service because the research questions require it.

The contribution it is reaching for is narrower and more specific: an instrumented, auditable, retrieval-grounded agent for a single, well-bounded developer task, with a methodology defensible enough that someone else could reproduce a result on it.

---

If you have feedback or want to discuss the research direction, reach me at [mtanwar.com](https://mtanwar.com).
