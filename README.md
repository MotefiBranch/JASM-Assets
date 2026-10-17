# JASM-Assets

Automated character discovery and 256x256 icon asset pipeline for Just Another Skin Manager (JASM).

## Overview

This repository hosts:
1. `manifest.json`: Public metadata catalog linking GameBanana character category IDs to high-resolution 256x256 character icons with SHA256 checksums.
2. `Images/{Game}/{internalName}.png`: 256x256 RGBA square cropped character icons.
3. Automated GitHub Actions worker (`.github/workflows/crop_characters.yml`) that polls GameBanana categories daily, fetches official transparent avatars or datamined renders, applies smart face-crop geometry, and commits updates automatically.

## Supported Games

- Arknights: Endfield (Game ID: 21842, Root Category: 42770)
- Neverness to Everness (Game ID: 23012, Root Category: 37906)
- Zenless Zone Zero (Game ID: 19482, Root Category: 30305)
- Genshin Impact (Game ID: 8552, Root Category: 18140)

## Quality Tiers

- Tier 3 (`official_avatar`): Datamined in-game headshot icons.
- Tier 2 (`auto_crop`): 1:1 anime face crop generated from high-resolution transparent splash art.
- Tier 1 (`thumbnail`): GameBanana category icon placeholder.

## Automated Execution

The pipeline executes via GitHub Actions:
- Scheduled daily at 04:00 UTC.
- Can be triggered manually via `workflow_dispatch`.
- Fast gatekeeper probe (<1s) checks GameBanana category diffs and exits immediately if no additions exist, minimizing GitHub Actions runner minutes.
