# Automated Code Review Agent

> **AI-Powered Code Review Automation using Azure AI, OpenAI API, and GitHub Webhooks**

An intelligent AI agent that automates the software development lifecycle by providing context-aware code reviews, security scanning, and style optimization through GitHub Pull Request integration.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Azure AI](https://img.shields.io/badge/Azure-AI%20Services-0078D4.svg)](https://azure.microsoft.com/en-us/products/ai-services)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Advanced Features](#advanced-features)
- [Development](#development)
- [Deployment](#deployment)
- [Contributing](#contributing)

---

## Features

### Core Capabilities

- **Real-Time GitHub Integration**: Intercepts Pull Request events via webhooks
- **AI-Powered Analysis**: Leverages Azure OpenAI (GPT-4) for intelligent code review
- **RAG System**: Retrieval-Augmented Generation with vector embeddings for context-aware guidelines
- **Security Scanning**: Pattern-based vulnerability detection (SQL injection, XSS, hardcoded secrets, etc.)
- **Multi-Language Support**: Python, JavaScript/TypeScript, Java, Go, Rust, Ruby, PHP, Swift, Kotlin
- **Structured Output**: JSON-formatted reviews with severity levels and actionable suggestions
- **Prompt Engineering**: Advanced prompts to reduce LLM hallucination and ensure guideline adherence

### Key Differentiators

1. **Hybrid Approach**: Combines static analysis (SecurityScanner) with LLM-based reviews
2. **Context-Aware**: RAG system retrieves relevant coding standards based on file type and content
3. **Production-Ready**: Includes Docker containerization, Azure Pipelines CI/CD, and health checks
4. **Scalable**: Async/await architecture for handling multiple PRs concurrently

---

## Architecture

```
┌─────────────┐         ┌──────────────────┐         ┌─────────────┐
│   GitHub    │────────>│   FastAPI App    │<───────>│  Azure AI   │
│  Webhooks   │ webhook │  (main.py)       │  API    │  OpenAI     │
└─────────────┘         └──────────────────┘         └─────────────┘
                               │
                    ┌──────────┼──────────┐
                    │          │          │
              ┌─────▼────┐ ┌───▼────┐ ┌──▼────────┐
              │ GitHub   │ │ Review │ │    RAG    │
              │ Client   │ │ Engine │ │  System   │
              └──────────┘ └────────┘ └───────────┘
                                │
                        ┌───────┴────────┐
                        │                │
                   ┌────▼─────┐   ┌─────▼──────┐
                   │ Security │   │ Guidelines │
                   │ Scanner  │   │ Embeddings │
                   └──────────┘   └────────────┘
```

### Component Overview

| Component | Purpose | Technology |
|-----------|---------|------------|
| **FastAPI App** | Webhook receiver & orchestrator | FastAPI, Uvicorn |
| **ReviewEngine** | LLM-based code analysis | Azure OpenAI GPT-4 |
| **RAGSystem** | Context retrieval from guidelines | Azure Embeddings, NumPy |
| **SecurityScanner** | Static security analysis | Regex pattern matching |
| **GitHubClient** | GitHub API interactions | httpx async client |

---

## Project Structure

```
automated-code-review/
├── code-review-agent/
│   └── code_review_agent/         # Main package
│       ├── __init__.py
│       ├── main.py                # FastAPI app & webhook handler
│       ├── review_engine.py       # AI review + SecurityScanner
│       ├── github_client.py       # GitHub API client
│       └── rag_system.py          # RAG with embeddings
├── tests/
│   └── test_review_engine.py     # Unit tests
├── guidelines/                    # Coding standards (markdown)
├── pyproject.toml                 # Dependencies & metadata
├── dockerfile                     # Container definition
├── azure-pipelines.yml            # CI/CD pipeline
├── .env                           # Environment variables
└── README.md                      # This file
```

---

## Installation

### Prerequisites

- Python 3.10+
- Azure OpenAI Service access
- GitHub account with repository admin access
- Docker (for containerized deployment)

### Local Development Setup

```bash
# Clone the repository
git clone <repository-url>
cd automated-code-review

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment template
cp .env.example .env
```

---

## Configuration

### Environment Variables

Create a `.env` file in the project root:

```env
# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_KEY=your-api-key-here
AZURE_OPENAI_DEPLOYMENT=gpt-4
AZURE_EMBEDDING_DEPLOYMENT=text-embedding-ada-002

# GitHub Configuration
GITHUB_TOKEN=ghp_your-github-token
GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

### Azure OpenAI Setup

1. Create an Azure OpenAI resource in Azure Portal
2. Deploy a GPT-4 model (for code review)
3. Deploy text-embedding-ada-002 (for RAG embeddings)
4. Copy endpoint and API key to `.env`

### GitHub Webhook Setup

1. Go to your repository → Settings → Webhooks → Add webhook
2. Set Payload URL: `https://your-domain.com/webhook/github`
3. Content type: `application/json`
4. Secret: (same as `GITHUB_WEBHOOK_SECRET` in `.env`)
5. Events: Select "Pull requests"
6. Activate webhook

---

## Usage

### Running Locally

```bash
# Start the server
uvicorn code_review_agent.main:app --reload --port 8000

# In another terminal, test health endpoint
curl http://localhost:8000/health
```

### Testing with ngrok (for local webhook testing)

```bash
# Install ngrok
npm install -g ngrok

# Expose local server
ngrok http 8000

# Use the ngrok URL in GitHub webhook settings
```

### Creating a Pull Request

Once configured, the agent will automatically:

1. Receive webhook notification when PR is opened/updated
2. Fetch PR files and diffs from GitHub
3. Run security scanner on changed files
4. Retrieve relevant coding guidelines via RAG
5. Generate AI-powered review with GPT-4
6. Post structured comments on the PR

---

## How It Works

### 1. Webhook Reception

```python
@app.post("/webhook/github")
async def github_webhook(request: Request):
    # Verify signature for security
    # Parse PR event data
    # Queue review in background task
```

### 2. Security Scanning

The `SecurityScanner` detects:

- **Hardcoded secrets** (passwords, API keys, tokens)
- **SQL injection** vulnerabilities
- **Command injection** risks (eval, exec, shell=True)
- **XSS vulnerabilities** (innerHTML, dangerouslySetInnerHTML)
- **Path traversal** issues

### 3. RAG-Based Context Retrieval

```python
# Compute embeddings for query
query_embedding = await get_embedding(code_snippet)

# Find similar guidelines using cosine similarity
relevant_guidelines = top_k_similar(query_embedding, guideline_embeddings)

# Include in LLM prompt
```

### 4. LLM Review Generation

**Prompt Engineering Strategy**:

- **System Prompt**: Defines role, guidelines, output format
- **User Prompt**: Includes file context, security issues, code diff
- **Structured Output**: Forces JSON response with schema validation
- **Temperature 0.1**: Reduces hallucination, increases consistency

### 5. GitHub Comment Posting

```json
{
  "body": "## 🤖 AI Code Review Summary\n\n### 🚨 Security Alerts\n- **2 CRITICAL** issues found\n\n### 📝 Review Notes\n- File looks good overall\n- Consider adding type hints",
  "event": "COMMENT",
  "comments": [
    {
      "path": "src/main.py",
      "line": 42,
      "body": "**[CRITICAL] SECURITY**\n\n**Issue:** SQL injection via f-string\n\n**Suggestion:** Use parameterized queries"
    }
  ]
}
```

---

## Advanced Features

### Custom Guidelines

Add custom coding standards in the `guidelines/` directory:

```markdown
# guidelines/security/api_security.md

## API Security Best Practices

- Always validate JWT tokens on protected endpoints
- Implement rate limiting (max 100 req/min per user)
- Use HTTPS for all API communication
- Log all authentication failures
```

The RAG system will automatically:
1. Load and embed the guideline
2. Retrieve it when reviewing API-related code
3. Include it in the LLM prompt

### Repository-Specific Guidelines

```python
from code_review_agent.rag_system import GuidelineManager

manager = GuidelineManager()
manager.save_repo_guidelines("owner/repo", [
    {
        "title": "Our TypeScript Style",
        "content": "Always use `const` over `let`...",
        "language": "typescript"
    }
])
```

### Extending Security Scanner

```python
# In review_engine.py
SecurityScanner.PATTERNS["nosql_injection"] = [
    (r'\$where.*user.*input', "Potential NoSQL injection"),
    (r'db\.\w+\.find\({.*\+.*}\)', "Unsafe MongoDB query")
]
```

---

## Development

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=code_review_agent --cov-report=html

# Run specific test
pytest tests/test_review_engine.py::TestSecurityScanner::test_detects_sql_injection
```

### Code Quality

```bash
# Format code
black code-review-agent/code_review_agent/

# Lint
ruff check code-review-agent/code_review_agent/

# Type checking
mypy code-review-agent/code_review_agent/
```

### Pre-commit Hooks

```bash
# Install pre-commit
pip install pre-commit

# Setup hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

---

## Deployment

### Docker Deployment

```bash
# Build image
docker build -t code-review-agent .

# Run container
docker run -d \
  --name code-review-agent \
  -p 8000:8000 \
  --env-file .env \
  code-review-agent
```

### Azure Container Instances

```bash
az container create \
  --resource-group myResourceGroup \
  --name code-review-agent \
  --image your-acr.azurecr.io/code-review-agent:latest \
  --dns-name-label code-review-agent \
  --ports 8000 \
  --environment-variables \
    AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT \
    AZURE_OPENAI_KEY=$AZURE_OPENAI_KEY
```

### Azure App Service

The included `azure-pipelines.yml` automates:

1. **Lint Stage**: Code quality checks (black, ruff)
2. **Test Stage**: Unit tests with coverage reporting
3. **Security Scan**: Bandit (SAST) + Safety (dependency vulnerabilities)
4. **Build Stage**: Docker image build and push to ACR
5. **Deploy Stage**: Deploy to Azure App Service (production environment)

---

## Performance & Scalability

### Current Capacity

- **Throughput**: ~10 PRs/minute (with 2 uvicorn workers)
- **Latency**: 3-8 seconds per file review (depends on file size)
- **Token Usage**: ~2000-4000 tokens per file

### Optimization Strategies

1. **Caching**: Cache embeddings for guidelines (implemented)
2. **Batching**: Review multiple files in parallel (async)
3. **Throttling**: Rate limit GitHub API calls to avoid 429s
4. **Pruning**: Truncate code snippets to 4000 chars in prompts

---

## Troubleshooting

### Common Issues

**Issue**: Webhook returns 401 Unauthorized
- **Solution**: Verify `GITHUB_WEBHOOK_SECRET` matches GitHub settings

**Issue**: Reviews not posting to GitHub
- **Solution**: Check `GITHUB_TOKEN` has `repo` and `write:discussion` scopes

**Issue**: LLM returns malformed JSON
- **Solution**: Ensure `response_format={"type": "json_object"}` is set

**Issue**: RAG retrieves irrelevant guidelines
- **Solution**: Add language-specific boosts in `retrieve_guidelines()`

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Add tests for new functionality
4. Ensure all tests pass (`pytest tests/`)
5. Format code (`black .`)
6. Commit changes (`git commit -m 'Add amazing feature'`)
7. Push to branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.

---

## Acknowledgments

- **Azure AI Services**: For providing state-of-the-art LLM capabilities
- **FastAPI**: For the high-performance async web framework
- **OpenAI**: For the powerful GPT models and embedding APIs
- **GitHub**: For the comprehensive webhooks and API

---

## Contact & Support

- **Author**: Munish Tanwar
- **Issues**: Please open an issue on GitHub
- **Documentation**: See this README and code comments

---

**Built with Agentforce orchestration principles | Powered by Azure AI**
