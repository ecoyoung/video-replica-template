#!/usr/bin/env bash
# Install this skill for the agent(s) you already have. Usage: ./install.sh [target-parent-dir]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="$(basename "$here")"

if [[ $# -gt 0 ]]; then
  targets=("$1")
else
  targets=()
  for dir in "$HOME/.claude/skills" "$HOME/.dsh/skills" "$HOME/.codex/skills" "$HOME/.agents/skills"; do
    [[ -d "$dir" ]] && targets+=("$dir")
  done
  if [[ ${#targets[@]} -eq 0 ]]; then
    echo "No agent skill directory found. Pass one explicitly, e.g.:"
    echo "  ./install.sh ~/.claude/skills"
    exit 1
  fi
fi

for parent in "${targets[@]}"; do
  mkdir -p "$parent"
  dest="$parent/$name"
  if [[ "$dest" == "$here" ]]; then
    echo "already at $dest"
    continue
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  # copy everything the agent needs, leave development artefacts behind
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '__pycache__' --exclude '.git' --exclude '*.zip' --exclude '.venv*' "$here/" "$dest/"
  else
    tar -C "$here" -cf - --exclude '__pycache__' --exclude '.git' --exclude '*.zip' . | tar -C "$dest" -xf -
  fi
  rm -rf "$dest/__pycache__" "$dest/scripts/__pycache__" "$dest/scripts/desklib/__pycache__"
  echo "installed -> $dest"
done

echo
echo "check it:"
echo "  python3 \"$parent/$name/scripts/desk.py\" doctor"
