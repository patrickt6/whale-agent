# whale-agent-cli

Run [Whale Agent](https://github.com/patrickt6/whale-agent) from npm. Whale Agent builds a
ranked daily digest of large public disclosures (SEC Form 4, 13D/G and more) and emails
it to you.

```bash
npx whale-agent-cli                 # opens the app
npx whale-agent-cli digest --demo   # offline sample digest
```

Whale Agent is a Python program, so this package is only a launcher. It looks for a
Python tool runner and passes your arguments to the real `whale` command:

1. `uvx --from whale-agent whale ...` if you have [uv](https://docs.astral.sh/uv/)
2. `pipx run --spec whale-agent whale ...` if you have [pipx](https://pipx.pypa.io)
3. Otherwise it prints the one-line installer:

   ```bash
   curl -fsSL https://whale-agent.com/install | sh
   ```

The launcher itself downloads nothing. uv or pipx fetch the `whale-agent` package from
PyPI and cache it.

Set `WHALE_INSTALL_SOURCE` to run another build, for example a local wheel or
`git+https://github.com/patrickt6/whale-agent.git`.

## Why the name is not `whale-agent`

An unrelated package already uses `whale-agent` on npm, so this one is
`whale-agent-cli`. The Python package on PyPI is still `whale-agent`.

MIT licence.
