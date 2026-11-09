#!/bin/sh
# Lance le binaire Claude Code fourni par l'extension VS Code (Remote-SSH), dans sa version la plus récente.
# Son chemin contient le numéro de version et change à chaque mise à jour : CLAUDE_BIN pointe sur ce script.
bin=$(ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -n 1)
[ -n "$bin" ] || { echo "Claude Code introuvable dans ~/.vscode-server/extensions" >&2; exit 127; }
exec "$bin" "$@"
