# Faire tourner Jarvis sur une VM

Jarvis tourne en service systemd utilisateur sur la VM (les agents travaillent sans ton poste) ; tu l'ouvres depuis ton poste par un transfert de port.

## Installation

1. **Cloner**, sur la VM, dans le dossier de ton choix :
   ```bash
   gh repo clone <organisation>/Jarvis && cd Jarvis && uv sync
   ```
2. **Copier ta configuration et tes données**, depuis ton poste (l'historique des agents reste propre à chaque machine) :
   ```bash
   rsync -a --exclude agents/ config data <vm>:<dossier de Jarvis sur la VM>/
   ```
3. **Installer le service**, sur la VM, depuis le dossier de Jarvis ; remplace les `<…>` (`JARVIS_AGENT_ADD_DIRS` est facultatif) :
   ```bash
   mkdir -p ~/.config/systemd/user/jarvis.service.d && cp deploy/jarvis.service ~/.config/systemd/user/
   printf '[Service]\nWorkingDirectory=%s\nEnvironment=JARVIS_MODE=live\nEnvironment=ORCHESTRATOR_DIR=<dossier de integration-orchestrator>\nEnvironment=CLAUDE_BIN=%s/deploy/claude-vscode.sh\nEnvironment=JARVIS_AGENT_ADD_DIRS=<dossier de notes>\nEnvironment=AIRFLOW_BASE_URL=http://localhost:8888\n' "$PWD" "$PWD" > ~/.config/systemd/user/jarvis.service.d/vm.conf
   systemctl --user daemon-reload && systemctl --user enable --now jarvis
   loginctl enable-linger $USER   # le service continue après la déconnexion SSH
   ```
4. **Secrets** : `AIRFLOW_USER`, `AIRFLOW_PASSWORD` et, si besoin, `NOTION_TOKEN` dans le `.env` du dossier de Jarvis, puis `systemctl --user restart jarvis`.
5. **Ouvrir** : transfert du port 8765 (onglet Ports de VS Code ou `ssh -L 8765:localhost:8765 <vm>`), puis http://localhost:8765.

Suivi : `systemctl --user status jarvis`, `journalctl --user -u jarvis -f`. Mise à jour : `git pull && uv sync && systemctl --user restart jarvis`.

## Bon à savoir

- **Claude** : sans Claude Code installé sur la VM, `deploy/claude-vscode.sh` prend le binaire le plus récent de l'extension VS Code Remote-SSH. Claude doit être connecté sur la VM. L'agent charge le `CLAUDE.md` et les hooks de la VM, hook de fin de session compris.
- **Synchro Notion** : la routine Claude Code de ton poste écrit dans le `data/` de ton poste. Sur la VM, utilise « Rafraîchir depuis Notion » (jeton) ou recopie `data/` (étape 2).

## Shell de la VM dans la console (`JARVIS_SHELL=1`)

Onglet **Shell VM** de la console : un vrai terminal (xterm.js, chargé depuis `cdn.jsdelivr.net`) sur le shell de connexion de l'utilisateur du service.

- **Désactivé par défaut.** Sur la VM seulement : `Environment=JARVIS_SHELL=1` dans `vm.conf`, puis `systemctl --user restart jarvis`.
- **Garde-fous** ([`src/deploy_hub/shell.py`](../src/deploy_hub/shell.py)) : connexions locales seulement, page venant de Jarvis (origine vérifiée), jeton renouvelé à chaque démarrage, secrets de Jarvis absents de l'environnement.
- La session dure tant que la page est ouverte : pour un travail long, `tmux`.
- **Oh My Zsh** (comme sur le Mac, thème `robbyrussell`, plugin `git`) :
  ```bash
  sudo apt install -y zsh git
  # installe Oh My Zsh (https://ohmyz.sh), puis dans ~/.zshrc : ZSH_THEME="robbyrussell" et plugins=(git)
  chsh -s "$(command -v zsh)" && systemctl --user restart jarvis
  ```
  Pour un thème à glyphes (agnoster, powerlevel10k), installe une police Nerd Font sur ton poste (`MesloLGS NF`, `JetBrainsMono Nerd Font`, `Hack Nerd Font`).
