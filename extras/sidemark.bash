# bash completion for sidemark
#
# Installed automatically by install.sh / the PKGBUILD to the standard
# bash-completion directory. To enable manually:  source extras/sidemark.bash
# (works in zsh too after `autoload -U +X bashcompinit && bashcompinit`).
_sidemark() {
    local cur prev opts
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    opts="-h --help -v --verbose --page --presentation --deck --list-recent"

    # --page takes a numeric argument we can't usefully complete
    if [[ "$prev" == "--page" ]]; then
        return 0
    fi

    if [[ "$cur" == -* ]]; then
        mapfile -t COMPREPLY < <(compgen -W "$opts" -- "$cur")
        return 0
    fi

    # otherwise complete a document to open (plus directories to descend into).
    # Prefer bash-completion's _filedir when present (handles dirs + case); fall
    # back to compgen with extglob enabled so it also works when sourced alone.
    if declare -F _filedir >/dev/null 2>&1; then
        _filedir '@(pdf|pptx|md|markdown|txt|smdeck|PDF|PPTX|MD|MARKDOWN|TXT|SMDECK)'
    else
        local files dirs had_extglob=0
        shopt -q extglob && had_extglob=1 || shopt -s extglob
        mapfile -t files < <(compgen -f -X '!*.@(pdf|PDF|pptx|PPTX|md|markdown|txt|smdeck)' -- "$cur")
        mapfile -t dirs  < <(compgen -d -- "$cur")
        ((had_extglob)) || shopt -u extglob
        COMPREPLY=("${files[@]}" "${dirs[@]}")
    fi
}
complete -o filenames -F _sidemark sidemark
