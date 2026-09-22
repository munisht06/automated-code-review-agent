# Architecture

This document describes the system design of the Automated Code Review Agent: how the components are decomposed, how data flows between them, and where the boundary between deterministic and stochastic computation is drawn. It is intended to be read after the README's *Approach* section and before reading the source.

## Design goals

The system is shaped by four design goals, in priority order:

1. **Auditability.** Every comment should be traceable to the inputs the model saw: the diff, the retrieved guidelines, and the static-scanner findings placed in the prompt. The evaluation harness records, per file, the retrieved guideline IDs, any input that was truncated to fit the prompt, parse errors, and the raw model response. The guideline IDs a comment cites are reported by the model itself and are not verified.
2. **Composable evaluation.** Stages should be switchable without architectural change. RAG on/off is a constructor flag today (`ReviewEngine(rag_enabled=False)`, `--no-rag` in the harness); a scanner-off switch and model comparisons are planned.
3. **Determinism where possible, stochasticity where necessary.** The LLM call is the only stochastic stage. Signature verification, diff-line mapping, retrieval (given the embeddings), prompt construction, response parsing and comment filtering are deterministic and covered by offline unit tests.
4. **Operational realism.** The system is a single FastAPI service behind a GitHub webhook and builds as a container.

## Runtime data flow

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant FA as FastAPI App<br/>(main.py)
    participant GC as GitHubClient
    participant SS as SecurityScanner
    participant RAG as RAGSystem
    participant RE as ReviewEngine
    participant AOAI as Azure OpenAI

    GH->>FA: POST /webhook/github (PR opened)
    FA->>FA: HMAC signature verification
    FA->>FA: Parse PR event payload
    FA-->>GH: 200 {"status": "processing"}
    Note over FA: Background task (FastAPI BackgroundTasks)

    FA->>GC: get_pr_files(repo, pr_number)
    GC->>GH: GET /repos/{}/pulls/{}/files (paginated)
    GH-->>GC: file list + patches
    GC-->>FA: per-file patches

    Note over FA: Sequential per-file loop
    loop for each reviewable file
        FA->>GC: get_file_content(repo, path, sha)
        GC-->>FA: file content
        FA->>RE: review_file(filename, patch, file_content)
        RE->>SS: scan(file_content)
        SS-->>RE: security findings (regex hits)
        RE->>RAG: retrieve_guidelines(filename, file_content)
        RAG->>AOAI: embeddings (query)
        AOAI-->>RAG: query embedding
        RAG->>RAG: cosine similarity over guideline embeddings
        RAG-->>RE: top-k guidelines (k=3)
        RE->>RE: construct system + user prompts
        RE->>AOAI: chat completion (JSON mode, T=0.1)
        AOAI-->>RE: JSON review payload
        RE->>RE: parse and normalize the JSON
        RE-->>FA: FileReviewResult
        FA->>FA: keep comments on diff lines; others go to the summary
    end

    FA->>GC: create_pr_review(pr_number, comments, summary)
    GC->>GH: POST /repos/{}/pulls/{}/reviews
    GH-->>GC: 200
    GC-->>FA: ack
```

## Component-by-component design

### 1. FastAPI orchestrator (`main.py`)

The orchestrator owns four responsibilities:

- HMAC verification of the inbound webhook against `GITHUB_WEBHOOK_SECRET`. Failures return `401` before any computation runs. Verification fails closed: with no secret configured, every request is rejected unless `ALLOW_UNSIGNED_WEBHOOKS` is set for local development.
- Parsing the PR event payload. Malformed JSON, or a payload missing the repository or pull-request fields, returns `400`. Events other than `pull_request.opened` and `pull_request.synchronize` are acknowledged and ignored.
- Dispatching the review as a FastAPI `BackgroundTasks` callable, so the webhook responds at once with `200` and `{"status": "processing"}`, well within GitHub's delivery timeout.
- Driving the per-file review loop. Removed files are skipped. Each file is reviewed inside its own error handler, so one failing file does not abort the others. Comments on lines that appear in the diff are posted as line comments; comments on other lines go into the review summary, because GitHub rejects the entire review if any comment targets a line outside the diff.

Retrieval, inference and GitHub calls are delegated to the components below. The webhook handler is tested with FastAPI's `TestClient` and a stubbed review task, and the review loop in `process_pull_request` with stubbed GitHub and engine objects.

### 2. `GitHubClient`

Wraps GitHub's REST endpoints behind an async `httpx` client. It is the only module that calls the GitHub API; `process_pull_request` constructs it. There are no retries or rate-limit handling: a non-2xx response raises.

The client exposes a small surface:

- `get_pull_request(repo, pr_number)`: full PR metadata
- `get_pr_files(repo, pr_number)`: every changed file with its patch, following pagination (100 files per page)
- `get_file_content(repo, file_path, commit_sha)`: file content at a given commit, base64-decoded from GitHub's contents endpoint
- `create_pr_comment(repo, pr_number, comment)`: issue-style PR comment, used for orchestration messages such as a failure or no files to review
- `create_pr_review(repo, pr_number, commit_sha, comments, summary)`: a review with line-level comments, each addressed by `line` and `side`

A module-level helper, `commentable_lines(patch)`, returns the new-file line numbers that appear in a patch (added and context lines, bounded by each hunk header's line count). The orchestrator uses it to decide which comments can be posted on a line. GitHub omits the patch for very large or binary diffs; every comment on such a file goes into the summary.

### 3. `SecurityScanner`

A pattern-based static scanner: a catalog of regular expressions, each with a vulnerability class and a short description, matched line by line and case-insensitively. It finds hits, returns structured findings, and does no semantic analysis. It accepts a language argument but does not use it yet: every pattern runs on every file.

Categories currently covered:

- Hardcoded secrets (string literals assigned to passwords, API keys, tokens and secrets; AWS secret keys in assignments or dict entries)
- SQL injection (queries built with f-strings, `.format()` or `+` concatenation)
- Command injection (bare `eval()` and `exec()` calls, `os.system` with concatenation, `subprocess` with `shell=True`)
- XSS sinks (`innerHTML` with concatenation, `dangerouslySetInnerHTML`, `document.write`)
- Path traversal (`open()` with a concatenated `..` path; a `File(...)` constructor taking a user-named variable)

The scanner complements the LLM rather than replacing it. It runs first, and its five highest-severity findings are placed in the prompt. This serves three purposes:

- It catches a known class of issues cheaply, and every hit names the pattern that fired.
- It gives the LLM concrete evidence to work from. Whether that improves the review is not measured yet; the planned scanner-off ablation would test it.
- Its findings are reproducible, since regex hits are deterministic.

Its precision has not been measured. The tests pin known false-positive cases (`model.eval()`, `ast.literal_eval()`, `read_file(username)`, an AWS key read from the environment) as non-findings.

Severity is assigned by class: SQL injection, command injection and path traversal are `CRITICAL`; hardcoded secrets and XSS are `HIGH`.

### 4. `RAGSystem`

The retrieval layer. It loads every markdown file under `guidelines/` (resolved relative to the package, not the working directory), embeds each file as a single chunk with the deployment named in `AZURE_EMBEDDING_DEPLOYMENT`, and caches the embeddings in memory. At review time it embeds a query built from the file's detected language, its name and its first 1,000 characters, ranks guidelines by cosine similarity, and returns the top *k* (default 3). A guideline whose language matches the file's language gets a 1.3× similarity boost; a guideline's language comes from its file-name prefix (`python_…`, `typescript_…`). Embeddings are computed on first retrieval.

If no guideline files are found, the system falls back to five built-in default guidelines, logs a warning, and records the corpus source, which the evaluation harness writes into every report of a run with RAG on.

Three design notes:

- **The bundled corpus is small.** It is two documents (Python; TypeScript and React), one chunk each. With k=3, every file retrieves the whole corpus, and similarity and the language boost only set the order. The RAG on/off ablation therefore currently compares all guidelines against none, not selective retrieval against none.
- **Why guidelines, not codebase?** The retrieval surface is a standards corpus, which is bounded and auditable. The bundled documents are generic best-practice notes rather than a team's own conventions; the grounding hypothesis needs a team-authored corpus, and extending retrieval to the surrounding codebase is future work (see the README).
- **Why cosine similarity, not a vector database?** At this corpus size an in-memory NumPy computation is simpler and easier to reason about than a vector database. If the corpus grows to the point where this matters, swapping in a vector store is a localized change.

A companion `GuidelineManager` class can persist repository-specific guidelines to disk, but it is not wired in: nothing in the runtime path loads them.

### 5. `ReviewEngine` and the LLM boundary

The `ReviewEngine` holds the system's only stochastic stage. For each file it builds the prompt, calls the LLM once, parses the response, and returns a `FileReviewResult` with its provenance (retrieved guideline IDs, truncated inputs, parse error, the model name the API reported, raw response). The Azure client is created on first use, so the engine can be constructed and tested without credentials, and a client can be injected.

The prompt is a system + user pair:

- **System message** opens with the role ("You are an expert code reviewer specializing in security, performance, and code quality."), then lists each retrieved guideline as `## {title} (id: {id})` followed by its text, then a five-item focus list (security, style, performance, bugs, maintainability) and the JSON structure to return, including a `cited_guideline_ids` list per comment. Each guideline has a 4,000-character budget; both bundled guidelines fit whole. With RAG off, the guideline section reads "(No guidelines are provided for this review.)"
- **User message** carries the file name, up to five scanner findings sorted by severity (CRITICAL → HIGH → MEDIUM → LOW), the diff (2,000-character budget) and the file content, line-numbered (4,000-character budget) so the model can report line numbers without counting.

Any input cut to fit its budget is cut at a line break where possible, and visibly: the prompt carries a marker (`[... truncated: showing N of M characters]`, placed after the numbered lines in the file content, unnumbered) and the result records the cut in `truncated_inputs`.

The LLM is invoked with:

- `response_format={"type": "json_object"}`. JSON mode returns syntactically valid JSON unless the response is cut off at the token limit, and it says nothing about the field structure; the structure is requested in the prompt and checked during parsing.
- `temperature=0.1`, a bias toward consistency. How consistent the outputs actually are at this setting is what the consistency measurement in the [evaluation framework](./EVALUATION.md) is for.
- A pinned model deployment name. Evaluation runs are not portable across model versions; pin the deployment in `.env` and change the pin only when re-running the full evaluation suite.

The response is parsed with `json.loads` and mapped onto `LineComment` dataclasses. Line numbers given as digit strings or whole floats are accepted and values below 1 are dropped, severity and category are normalized, and comments without a usable line number are dropped and counted in `parse_error`. If the response is not a JSON object, the result has no LLM comments, keeps the scanner findings, and records the error and the raw response, so a parse failure is never read as "no findings". There is no Pydantic schema validation.

### 6. Evaluation harness (`code_review_agent/evaluation/`)

The evaluation package is independent of the webhook path. It loads PR fixtures from disk, drives the same `ReviewEngine`, `RAGSystem` and `SecurityScanner` used at runtime, and scores the structured output. Each report records the run's configuration (git commit and whether tracked files had uncommitted changes, deployment names, API version, temperature, RAG on/off, repeats, prompt budgets, corpus source) and per-file provenance, including the model name the API reported. A review call that fails is recorded, its run is excluded from scoring, and the harness exits with status 3.

`--mock-llm` replaces Azure with an offline stand-in: hashed bag-of-words embeddings and a chat stub that echoes the scanner findings in the prompt. It exercises the harness end to end without credentials; its numbers are not model results. No live benchmark results are reported yet. Methodology and metric definitions are in [`EVALUATION.md`](./EVALUATION.md).

## Configuration boundaries

External dependencies are configured through environment variables, loaded from `.env` by python-dotenv:

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_KEY` | API key |
| `AZURE_OPENAI_API_VERSION` | Azure OpenAI API version (default `2024-02-15-preview`) |
| `AZURE_OPENAI_DEPLOYMENT` | Chat-model deployment name (pin for reproducibility) |
| `AZURE_EMBEDDING_DEPLOYMENT` | Embedding-model deployment name (pin for reproducibility) |
| `GITHUB_TOKEN` | GitHub token that can read the repository and write pull-request reviews (classic PAT: `repo`) |
| `GITHUB_WEBHOOK_SECRET` | Shared secret for HMAC verification |
| `ALLOW_UNSIGNED_WEBHOOKS` | Local development only: accept unsigned webhooks when no secret is set |

`.env.example` is checked in; `.env` is git-ignored.

## Concurrency model and current limits

The webhook returns immediately and runs the review as a FastAPI `BackgroundTasks` callable in the same process, after the response is sent. Reviews for different PRs run concurrently on the event loop, interleaving at network calls. Each review builds its own `ReviewEngine`, so the guideline embeddings are recomputed once per PR.

Within a single PR, files are reviewed in a sequential loop. There are no semaphores on outbound LLM or GitHub API calls; on a high-volume repository this is a load-bearing limitation. Bounded per-file concurrency with an explicit semaphore, and a graceful fallback when Azure OpenAI returns a 429, are on the roadmap.

`BackgroundTasks` is also not durable. If the process restarts mid-review, in-flight reviews are lost. A real task system (Celery, dramatiq, or an Azure-native equivalent) is required for production and is deliberately out of scope for the prototype.

## Failure modes and observability

The system distinguishes three classes of failure:

1. **Inbound failure.** A bad signature returns `401`; malformed JSON or missing pull-request fields return `400`; unsupported events and actions return an `ignored` JSON response. No background work is dispatched.
2. **Per-file failure.** Any exception while fetching or reviewing one file (a GitHub API error, non-UTF-8 content, an embedding or LLM failure) is logged with its traceback, and the file is listed under "Files not reviewed" in the summary; the rest of the PR is still reviewed. A model response that fails to parse is not an exception: the result carries `parse_error` and keeps the scanner findings.
3. **Outer failure.** Any other exception in `process_pull_request` (listing files, constructing the engine, posting the review) is logged with its traceback, and a generic comment is posted on the PR: "⚠️ Code review encountered an error and could not complete. See the service logs for details." Exception details stay in the log rather than in a public comment.

Not implemented: structured logging with per-PR correlation IDs, retries for transient network failures, and run-cost tracking.

## Deployment

The Dockerfile builds a container for the service, suitable for Azure Container Instances or Azure App Service; it has not been deployed to either. `azure-pipelines.yml` is a CI/CD template with lint, test, security-scan, build and deploy stages; its service connections are placeholders and it has not been run. For local development, the README describes exposing the service through ngrok.

The deployment surface is intentionally narrow. Multi-tenant operation, regional failover and high availability are out of scope for the prototype.

## Boundaries that are deliberately not crossed

- **No database.** The agent is stateless between PR events. Guidelines are loaded from disk and embedded when a review starts; review output is written to GitHub, not persisted locally.
- **No user-facing UI.** The only interface is GitHub PR comments. A dashboard for review history, run cost or fixture management is deliberately excluded; the agent's surface is the artifact engineers already look at.
- **No fine-tuning.** The grounding strategy is retrieval, not parameter updates. This is a research choice: retrieved context can be inspected (the evaluation harness records which guidelines each file retrieved), and fine-tuned behavior cannot be inspected the same way.
