# AI Paper Digest

A daily, automatically curated digest of new AI/ML research from arXiv. Every morning a GitHub Actions workflow fetches the latest papers in `cs.AI`, `cs.LG`, `cs.CL` and `cs.CV`, ranks them by relevance to applied AI engineering (agents, RAG, LLM inference, fine-tuning, multimodal), writes a short plain-language summary for each of the top picks, and commits the result to this repository.

The archive grows into a searchable record of what the field is publishing, plus a CSV dataset you can analyze for trends.

## Latest digest

<!-- LATEST_DIGEST_START -->
_The first digest will appear here after the workflow runs._
<!-- LATEST_DIGEST_END -->

## How it works

```
arXiv API ──► dedupe (data/seen.json) ──► keyword ranking ──► LLM summary ──► digests/ + data/papers.csv + README
```

1. **Fetch.** Pulls the most recent submissions from the arXiv API for the configured categories.
2. **Deduplicate.** Skips any paper already processed on a previous day.
3. **Rank.** Scores each paper against weighted keywords in `config.json` (title matches count double).
4. **Summarize.** Sends the top papers to any OpenAI-compatible chat endpoint for a two-sentence TL;DR and a one-line "why it matters". Without an API key it falls back to the first sentences of the abstract, so the pipeline never breaks.
5. **Publish.** Writes `digests/YYYY/MM/YYYY-MM-DD.md`, appends to `data/papers.csv`, and refreshes the section above.

If there are no new papers (arXiv doesn't publish on weekends), nothing changes and no commit is made. Every commit in this repo contains real content.

## Repository layout

| Path | Contents |
|------|----------|
| `main.py` | The whole pipeline, standard library only |
| `config.json` | Categories, digest size, keyword weights, timezone, summary language |
| `digests/` | One Markdown digest per day |
| `data/papers.csv` | Every paper that made a digest (date, id, title, authors, categories, score, url) |
| `data/seen.json` | Processed arXiv IDs used for deduplication |
| `.github/workflows/daily-digest.yml` | Scheduled workflow (08:17 WIB daily) |

## Setup

1. Fork or clone this repository and push it to your GitHub account.
2. Get a free API key from [Groq](https://console.groq.com/keys) (or use any OpenAI-compatible provider).
3. In the repo, go to **Settings → Secrets and variables → Actions**:
   - **Secret** `LLM_API_KEY`: your API key.
   - **Variables** (optional, only to change provider or model):
     - `LLM_BASE_URL`, default `https://api.groq.com/openai/v1`
     - `LLM_MODEL`, default `openai/gpt-oss-20b`
     - `LLM_REASONING_EFFORT`, default `low` (leave empty for providers that don't support it)
4. Go to **Actions → Daily AI Paper Digest → Run workflow** to trigger the first run manually.

After that it runs on its own every day.

### Using another provider

Any endpoint that implements `/chat/completions` works. Examples:

| Provider | `LLM_BASE_URL` |
|----------|----------------|
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| Local Ollama | `http://localhost:11434/v1` |

Model IDs change over time, so check your provider's model list if summaries start falling back to abstracts.

## Run locally

```bash
export LLM_API_KEY=your_key   # optional
python main.py
```

Requires Python 3.9+. No `pip install` needed.

## Customize

Edit `config.json`:

- `categories`: any [arXiv category](https://arxiv.org/category_taxonomy), e.g. add `cs.RO` for robotics or `cs.CR` for security.
- `keywords`: terms and weights that decide what rises to the top.
- `digest_size`: papers per digest.
- `language`: summary language, e.g. `"Indonesian"`.

## License

MIT. Paper metadata and abstracts belong to their authors and are provided by arXiv. Thank you to arXiv for use of its open access interoperability.
