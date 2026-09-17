# LeanGUI

---

A gui wrapper for leandvb. Mostly vibe-coded, but I'll try to unvibe code it in the future.

## Dependencies

---

### Python Libraries

1. numpy
  
2. PySide6
  
3. pyqtgraph
  

### Binaries

1. leandvb
  
2. ldpc_tool
  
3. mpv
  

leandvb and ldpc_tool are expected to be in the same directory as gui.py. For convenience, prebuilt binaries are currently shipped at the project root, but they are **not** git-tracked (see `.gitignore`) - if they're ever missing (fresh clone without them, or after a `git clean`), rebuild them.

This GUI relies on `--fd-gse`, `--fd-bbf`, and `--drift` flags in leandvb. Those flags are **not** present in upstream's tagged releases (the latest tag, `1.2.0`, predates them) - they only exist in unreleased work on the `work` branch of [pabr/leansdr](https://github.com/pabr/leansdr). `ldpc_tool` isn't part of that repo at all; it comes from a separate fork, [pabr/xdsopl-LDPC-pabr](https://github.com/pabr/xdsopl-LDPC-pabr) (branch `ldpc_tool`).

Run `./build.sh` from the project root to reproducibly clone and build both binaries from source into `./build/` (it does not touch the prebuilt binaries at the project root - copy `build/leandvb` and `build/ldpc_tool` over them, or point the GUI at `./build/`, once you've verified they work). See the comments at the top of `build.sh` for details.

This GUI is currently validated against:
- leandvb: `pabr/leansdr` commit `84c59e1c7a1a79338d5722d63f28640cc9d350f3` (`work` branch tip; `leandvb --version` reports `leansdr-1.2.0-110-g84c59e1`)
- ldpc_tool: `pabr/xdsopl-LDPC-pabr` commit `6ada4aac6d853835eeaefb7e12a0136481647b01` (`ldpc_tool` branch tip; this fork carries no version string)

## Usage

---

```bash
$ python3 gui.py
```
