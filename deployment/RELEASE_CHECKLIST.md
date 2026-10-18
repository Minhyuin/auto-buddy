# Release checklist

Use this checklist for every production release. The updater and CI enforce
many of these mechanically; the remaining items require a human.

## Before tagging

- [ ] CI on `main` is green, including the PostgreSQL-backed Django tests,
      deploy-config checks, upstream submodule tests, image smoke test and
      security scan.
- [ ] Conventional commit history produces the intended bump:
      `python scripts/next_version.py --format text`.
- [ ] If a specific version is required, confirm it is greater than the latest
      tag and pass it as the workflow `version` input rather than rewriting
      tags.
- [ ] Migration policy is reviewed with
      `python scripts/release_manifest.py policy --format text`. Any `review`
      or `unsafe` operation has an explicit rollout/rollback plan.
- [ ] The upstream gitlink is a formal upstream release, not an unreleased
      branch HEAD. Confirm with
      `git ls-tree HEAD workbuddy-auto-signin` and
      `git -C workbuddy-auto-signin describe --tags --always`.
- [ ] The upstream stable version is recorded; note any known upstream defects
      (for example growth partial-result masking before the next formal
      release) in the release notes. Do not patch the submodule for production.
- [ ] The root repository has no uncommitted changes. Do not ship a dirty tree.
- [ ] Runtime secrets, `.env` files, session JSON, backups and generated
      credentials are not staged.

## Release workflow

- [ ] Dispatch `.github/workflows/release.yml` on `main` (or call it from a
      trusted workflow). Do not build a production image from a developer
      workstation.
- [ ] The reusable CI job passed for the exact commit being released.
- [ ] The image was built for `linux/amd64`, pushed by commit SHA, and its
      registry digest recorded.
- [ ] Cosign keyless signing succeeded and the workflow's own
      `cosign verify` passed with the strict identity and issuer.
- [ ] `release-manifest.json` was generated from the actual gitlink and
      validated against `deployment/release-manifest.schema.json`.
- [ ] The manifest asset digest (or `Release-Manifest-SHA256:` line in the
      release body) is present and the updater can verify it.
- [ ] Version, `stable` and `latest` image tags were created only after all of
      the above succeeded and resolve to the released image. `imagetools create`
      may wrap the manifest in an OCI index, so a tag digest can differ from the
      image digest recorded in `release-manifest.json`; the updater always pulls
      the recorded digest, never these tags.
- [ ] The GitHub release is non-draft, non-prerelease, and the version is
      `vMAJOR.MINOR.PATCH`.

## Deployment

- [ ] `--check` passes on the host:
      `python3 /usr/local/lib/auto-buddy/auto_buddy_updater.py --check`.
- [ ] `--dry-run` selects the expected version and verifies the manifest,
      digest, signature and labels without touching services.
- [ ] The migration policy is backward compatible, or a maintenance window
      and tested restore plan are approved.
- [ ] The previous release's backup exists and a restore rehearsal has been
      done on an isolated host for any risky migration.
- [ ] The worker is drainable; no Run is expected to exceed the 650 s grace.
- [ ] The update runs outside a traffic peak; Caddy continues serving during
      the short web stop, or maintenance is announced.
- [ ] After the update: `/health/` is 200, login/credential flows work, the
      worker processes a synthetic job, and logs show no secret leakage.
- [ ] `--status` shows the new `current.json`, the previous release remains in
      `previous.json`, and `failed.json`/`manual-required.json` are absent.
- [ ] The timer is enabled and the next scheduled run is a no-op.

## Rollback / incident

- [ ] A failed deploy before migrations auto-restores the old services; verify
      with `--status` and logs.
- [ ] A failed deploy after backward-compatible migrations auto-rolls back the
      image; confirm health and worker resume.
- [ ] If `manual-required.json` exists, stop and follow
      `deployment/README.md`; do not restore the database automatically and do
      not delete state until the incident is understood.
- [ ] Record the failed tag and root cause. Use `--retry-failed` only after a
      fix, or `--clear-failed` to allow a newer release.
- [ ] Rotate credentials if logs, backups or session material were exposed.
