# PowerShell completion for bulbctl.
# Install: add to your PowerShell profile ($PROFILE), e.g.
#   Add-Content $PROFILE '. "C:\path\to\cli\completions\bulbctl-completion.ps1"'
$bulbctlCommands = @(
    'list', 'on', 'off', 'toggle', 'color', 'brightness', 'scene', 'status',
    'scenes', 'presets', 'groups', 'login', 'logout', 'auth-status', 'completion'
)

Register-ArgumentCompleter -Native -CommandName bulbctl, bulbctl.py -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $tokens = $commandAst.CommandElements | ForEach-Object { $_.ToString() }

    if ($tokens.Count -le 2) {
        $bulbctlCommands | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }

    if ($tokens[1] -eq 'completion') {
        @('bash', 'zsh', 'powershell') | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
    }
}
