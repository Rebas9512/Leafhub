# leafhub-sdk

Lightweight credential resolution SDK for [LeafHub](https://github.com/Rebas9512/Leafhub) consumer projects.

**Zero external dependencies.** Works with Python 3.10+.

## Install

```bash
pip install leafhub-sdk
```

## Quick Start

### 1. Create `leafhub.toml` in your project root

```toml
[project]
name = "my-project"

[[bindings]]
alias = "llm"
required = true

[env_fallbacks]
llm = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
```

### 2. Resolve credentials in your code

```python
from leafhub_sdk import resolve

# Get a credential object (api_key, model, base_url, api_format)
cred = resolve("llm")
print(cred.api_key, cred.model)

# Or get a pre-built API client
cred = resolve("llm", as_client=True)
client = cred.client  # openai.OpenAI or anthropic.Anthropic

# Or inject as environment variables
env = resolve("rewrite", as_env=True)
os.environ.update(env)
```

## Resolution Priority

1. **LeafHub vault** -- `.leafhub` token + encrypted storage
2. **Manifest fallbacks** -- env vars declared in `leafhub.toml`
3. **Common provider env vars** -- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.
4. **CredentialError** -- actionable error message

## leafhub.toml Schema

```toml
[project]
name = "my-project"          # required
python = ">=3.11"             # optional

[[bindings]]
alias = "llm"                 # required: runtime alias
required = true               # optional: fail if unresolvable
env_prefix = "LLM"            # optional: prefix for as_env=True output
capabilities = ["chat"]       # optional: declare needed capabilities

[setup]
extra_deps = [...]            # optional: extra install steps
post_register = [...]         # optional: post-registration hooks
doctor_cmd = "python check.py"  # optional: health check command

[env_fallbacks]
llm = ["MY_KEY", "OPENAI_API_KEY"]  # optional: env var fallback chain
```

## License

MIT
