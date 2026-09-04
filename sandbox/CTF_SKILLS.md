# External CTF skill catalog

The sandbox image installs `ljagiello/ctf-skills` read-only at
`/challenge/skills` from commit
`36c72e53a96a035791821caff7440882ea0f5c57`.

Source: <https://github.com/ljagiello/ctf-skills>

The solver prompt routes each challenge to one category `SKILL.md`. Agents may
read relevant linked reference documents, but must not automatically run the
repository's installer or bundled scripts. The project sandbox, scope, budget,
and flag-evidence policies take precedence over external guidance.

To update the catalog, review the upstream diff and security-sensitive scripts,
then change `CTF_SKILLS_REF` in `Dockerfile.sandbox` and rebuild `ctf-sandbox`.
