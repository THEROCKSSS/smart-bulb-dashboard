# bash completion for bulbctl
# Install: source this file from ~/.bashrc, e.g.
#   echo 'source /path/to/cli/completions/bulbctl-completion.bash' >> ~/.bashrc
_bulbctl_completions() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands="list on off toggle color brightness scene status scenes presets groups login logout auth-status completion"

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
        return 0
    fi

    case "${prev}" in
        completion)
            COMPREPLY=( $(compgen -W "bash zsh powershell" -- "${cur}") )
            return 0
            ;;
    esac
}
complete -F _bulbctl_completions bulbctl
complete -F _bulbctl_completions bulbctl.py
