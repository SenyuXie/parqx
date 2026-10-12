# Domain Docs

## Layout

This repo uses a single-context layout:

- `GLOSSARY.md` at the repo root contains shared domain vocabulary.
- `docs/adr/` contains architecture decision records, named `NNNN-short-title.md`.

## Before exploring

Read the root `GLOSSARY.md` and ADRs in `docs/adr/` that touch the area you are about to work in.

If these files do not exist, proceed silently. The `domain-modeling` skill creates them lazily when terms or decisions are resolved; setup does not create empty placeholders.

## Use the glossary's vocabulary

Use glossary terms when naming domain concepts in issues, proposals, hypotheses, tests, and code. Follow any preferred and avoided terms recorded there.

If a needed concept is missing, check whether an existing term fits. If there is a real gap, note it for `domain-modeling`.

## Flag ADR conflicts

When a proposal contradicts an existing ADR, cite that ADR and explain why the decision should be reconsidered before proceeding with a conflicting change.
