# Shown on every login shell. Lives in /etc/profile.d rather than ~/.bashrc so
# it survives a user rewriting their own dotfiles in the persistent home.
if [ -n "${PS1-}" ]; then
    printf '\n AI Factory Terminal — Rocky Linux 10\n'
    printf ' Home persists across restarts; /home/devuser/projects is a good place to work.\n'
    printf ' Session runs inside tmux, so a dropped connection keeps your work alive.\n'
    printf ' No sudo: install language tooling under $HOME (pip install --user, npm -g with a user prefix).\n\n'
fi
