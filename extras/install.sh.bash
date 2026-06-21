# bash completion for Sidemark's install.sh
#
# install.sh is run from the source tree, so this isn't auto-loaded; enable it
# from the repo root with:  source extras/install.sh.bash
# (works in zsh too after `autoload -U +X bashcompinit && bashcompinit`).
_sidemark_install() {
    local cur opts
    cur="${COMP_WORDS[COMP_CWORD]}"
    opts="-h --help -y --yes --with-ocr --walker-menu --register-pptx --uninstall"
    mapfile -t COMPREPLY < <(compgen -W "$opts" -- "$cur")
}
complete -F _sidemark_install install.sh ./install.sh
