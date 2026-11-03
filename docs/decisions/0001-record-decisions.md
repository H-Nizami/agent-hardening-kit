# 0001. Record decisions

## Status

Accepted (2026-10-22)

## Context

This repo is a portfolio piece and a template for client work. Readers (reviewers, clients, future me) need to know why things are the way they are, not only what they are. Choices such as pinning, tool versions and security boundaries are easy to undo by accident if the reason is not written down.

## Decision

We record non-obvious choices as short ADRs in `docs/decisions/`, numbered `NNNN-title.md`, with the sections Status / Context / Decision / Consequences.

## Consequences

- Each ADR is a few paragraphs; a change of mind is a new ADR that supersedes the old one.
- Pinned action SHAs and tool versions are listed in [`docs/pins.md`](../pins.md).
