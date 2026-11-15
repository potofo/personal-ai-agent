# Default skills

Skills in this folder are copied into `/data/skills` the first time the agent starts
(see `SkillStore.seed_defaults`). A skill that was seeded once is never re-added, so a
user who disables, edits or deletes it keeps that choice.

| Skill | Source | License |
| --- | --- | --- |
| `skill-creator` | [anthropics/skills](https://github.com/anthropics/skills/tree/1780c5e5a552fd4d1c9d6a7b0cab26b0c2bef064/skills/skill-creator) at commit `1780c5e5a552fd4d1c9d6a7b0cab26b0c2bef064`, unmodified | Apache-2.0 (`skill-creator/LICENSE.txt`) |

This environment cannot run the skill's scripts, sub-agents or eval loop. The agent's
instructions tell it to skip those steps and save skills with the `save_skill` and
`write_skill_file` tools instead. The skill files themselves are not changed.

To update: replace `skill-creator/` with a newer copy from the repository and update the
commit above. Existing workspaces keep their copy; users can re-import it from an export.
