# Woltspace 0.5.14

Python **0.5.14** is a small patch on the 0.5 line. The separately distributed
**@woltspace/tui remains at 0.5.2**; no npm release is required.

## A fresh lodge meets the newest Onboardie

- **The starter lodge is no longer pinned to a release.** A brand-new lodge
  installs Onboardie from the latest `main` of
  [woltspace-starter-lodge](https://github.com/jerpint/woltspace-starter-lodge),
  so she improves between releases. Today that means a confident pitch for
  people who want to build, plain answers on cost and safety, one install line
  and nothing more, and a little more beaver.
- **Existing lodges are unaffected.** The starter is installed once, on a
  lodge's first start. The revision a lodge got is recorded on the wolt.
- Set `WOLTSPACE_STARTER_SEED=none` before the first start to skip it, as
  before.

## Releases

- **Patch releases can ship from a maintenance branch.** 0.5 fixes now come from
  `release/0.5` while `main` moves on to 0.6. Nothing changes for installs:
  `uv tool install woltspace` and `woltspace update` keep picking the newest
  stable release.
