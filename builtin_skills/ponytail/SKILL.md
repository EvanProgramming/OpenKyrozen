# Ponytail — minimal code that works

## Trigger

Use this skill for every coding task when Ponytail is enabled.

## Steps

1. Check whether the requested code needs to exist.
2. Reuse a shared implementation already in the repository.
3. Prefer the standard library, native platform, or an installed dependency.
4. Add only the minimum code that meets the explicit requirement.
5. Fix root causes at the shared boundary instead of patching callers.
6. Do not simplify away validation, security, accessibility, or data-loss guards.

## Verify

Leave the smallest runnable regression check for non-trivial behavior and verify
the real user-facing path. Do not claim success from source inspection alone.
