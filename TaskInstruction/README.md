# Task Instructions

MainController loads task-specific language instructions from this directory.
Pass the filename stem through the required `--task-name` option:

```bash
ros2 run main_controller main_controller -- \
  --task-name 16mm-peg-in-hole
```

The corresponding file must be:

```text
TaskInstruction/16mm-peg-in-hole.json
```

Task names may contain ASCII letters, digits, `.`, `_`, and `-`. They must
start with a letter or digit and must not contain `..`.

Each JSON file has this schema:

```json
{
  "instructions": [
    {
      "text": "Insert the peg into the hole.",
      "weight": 0.6
    },
    {
      "text": "Fit the peg into the matching opening.",
      "weight": -1
    }
  ]
}
```

Every instruction must define exactly `text` and `weight`. Text must be a
non-empty string. A weight is either a finite number satisfying
`0 < weight < 1`, or `-1` for automatic allocation.

- If automatic entries exist, they equally share the probability remaining
  after all explicit weights.
- If no automatic entries exist, explicit weights must sum to `1`.
- If every entry uses `-1`, all instructions are equally weighted.

The file is loaded and validated once when MainController starts. Each new demo
samples one instruction and stores both `task_name` and
`language_instruction` in its manifest.

## Backfilling legacy manifests

Use the standalone tool to add task metadata to legacy demo manifests. It runs
in dry-run mode by default:

```bash
python3 tools/backfill_demo_task_metadata.py
```

Apply the validated changes explicitly:

```bash
python3 tools/backfill_demo_task_metadata.py --apply
```

The default rule assigns demos through `demo_20260618_160911` (inclusive) to
`16mm-peg-in-hole` and later demos to `gear-insert-big2small`. The tool samples
from the corresponding instruction file using the configured weights. It
automatically skips manifests that already contain valid `task_name` and
`language_instruction` values, and reports demo directories with no
`manifest.json`.
