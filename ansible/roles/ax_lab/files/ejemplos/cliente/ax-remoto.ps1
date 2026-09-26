#Requires -Version 7.4
# ax-remoto.ps1: usa la CLI de AX del servidor desde Windows, por SSH.
#
#   .\ax-remoto.ps1 <orden de ax> [argumentos...]
#   .\ax-remoto.ps1 get tasks -a '*'
#   .\ax-remoto.ps1 apply -f mi-tarea.yaml
#   .\ax-remoto.ps1 ssh <tarea> ls -la /workspace
#
# Igual que el ax-remoto de Linux y macOS: cada llamada es
# `ssh <servidor> sudo -n ax ...` y no abre ningún puerto ni túnel.
# Diferencias propias de PowerShell:
#   - PowerShell se come un `--` sin comillas. En `ssh <tarea> orden...` no
#     hace falta: lo que sigue a la tarea (y a -a <atespace>) es la orden.
#     Si la orden empieza por -a, -n, --server o --context, escribe '--'
#     entre comillas delante.
#   - `apply` solo acepta `-f <fichero>` (no `-f -`): el fichero se manda
#     byte a byte por la entrada estándar de ssh.
#   - Pon entre comillas los argumentos con comas, `$` o `@`, y escribe
#     -a <atespace>, no -a:<atespace>: PowerShell los transforma y el
#     ayudante los rechaza.
#   - ax-tarea no va por aquí: entra con `ssh -t` (ver LEEME.md).
# Requiere PowerShell 7.4 o posterior y el cliente OpenSSH de Windows.
#
# Variables: AX_REMOTO_HOST (apptolast, un Host de ~/.ssh/config),
# AX_REMOTO_SSH (ssh; un solo ejecutable), AX_REMOTO_TTY (auto, si o no).
# Sale con el código de la orden remota, 255 si falla ssh, 127 si no
# encuentra ssh y 64 o 66 si el error es local.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Paso de argumentos a programas nativos sin reinterpretar comillas
# (about_Parsing); no depende de la configuración de quien lo ejecuta.
$PSNativeCommandArgumentPassing = 'Standard'
$PSNativeCommandUseErrorActionPreference = $false

$Servidor = if ($env:AX_REMOTO_HOST) { $env:AX_REMOTO_HOST } else { 'apptolast' }
$Ssh = if ($env:AX_REMOTO_SSH) { $env:AX_REMOTO_SSH } else { 'ssh' }
$Tty = if ($env:AX_REMOTO_TTY) { $env:AX_REMOTO_TTY } else { 'auto' }

function Exit-ConFallo([string]$Mensaje, [int]$Codigo = 64) {
    [Console]::Error.WriteLine("ax-remoto: $Mensaje")
    exit $Codigo
}

# La ayuda es el comentario de cabecera (de la línea 2 a la primera que no
# es un comentario).
function Ayuda {
    foreach ($Linea in (Get-Content -LiteralPath $PSCommandPath | Select-Object -Skip 1)) {
        if (-not $Linea.StartsWith('#')) { break }
        $Linea -replace '^# ?', ''
    }
}

# Cita un argumento para el shell remoto con comillas simples (POSIX).
function Citar([string]$Texto) { "'" + $Texto.Replace("'", "'\''") + "'" }

# PowerShell convierte `a,b` en un array, `$x` sin definir y `@x` en $null, y
# parte `-f:x` en '-f:' y 'x'. Nada de eso es lo que se escribió: se rechaza.
$Argumentos = [System.Collections.Generic.List[string]]::new()
foreach ($a in $args) {
    if ($null -eq $a) { Exit-ConFallo 'un argumento llegó vacío ($null): ponlo entre comillas' }
    if ($a -is [System.Array]) { Exit-ConFallo "un argumento con comas llegó partido ($($a -join ',')): ponlo entre comillas" }
    $Texto = [string]$a
    if ($Texto -cmatch '^--?[^\s:=]+:$') { Exit-ConFallo "PowerShell parte «$Texto<valor>» en dos: escribe la opción y el valor separados por un espacio" }
    $Argumentos.Add($Texto)
}

if ($Argumentos.Count -lt 1) { Ayuda | ForEach-Object { [Console]::Error.WriteLine($_) }; exit 64 }
if ($Argumentos[0] -cin @('-h', '--help', 'ayuda')) { Ayuda; exit 0 }

$ConValor = @('-a', '--atespace', '--server', '--context', '-n', '--namespace')
$Remoto = [System.Collections.Generic.List[string]]::new()
$Remoto.Add('sudo -n ax')
$Fichero = $null

# Localiza la orden igual que cmd/ax/main.go: el primer argumento que no es
# una opción global ni el valor de una.
$Orden = $null
$Saltar = $false
foreach ($a in $Argumentos) {
    if ($Saltar) { $Saltar = $false; continue }
    if ($a -cin $ConValor) { $Saltar = $true; continue }
    if (-not $a.StartsWith('-')) { $Orden = $a; break }
}
if (-not $Orden) { Ayuda | ForEach-Object { [Console]::Error.WriteLine($_) }; exit 64 }

switch -CaseSensitive ($Orden) {
    { $_ -cin @('tarea', 'ax-tarea') } {
        Exit-ConFallo 'ax-tarea no se lanza con ax-remoto: entra con ssh -t y ejecútala allí (ver LEEME.md)'
    }
    'apply' {
        $n = 0
        for ($i = 0; $i -lt $Argumentos.Count; $i++) {
            $a = $Argumentos[$i]
            $Remoto.Add((Citar $a))
            if ($a -cin @('-f', '--file')) {
                $n++
                if ($i + 1 -ge $Argumentos.Count) { Exit-ConFallo 'apply necesita -f <fichero>' }
                $Ruta = $Argumentos[$i + 1]
                if ($Ruta -ceq '-') { Exit-ConFallo 'en PowerShell apply necesita un fichero, no -f -' }
                if (-not (Test-Path -LiteralPath $Ruta -PathType Leaf)) { Exit-ConFallo "no puedo leer el fichero local: $Ruta" 66 }
                $Fichero = (Resolve-Path -LiteralPath $Ruta).ProviderPath
                $Remoto.Add("'-'"); $i++
            }
        }
        # La CLI solo lee el primer -f (manifestFromArgs, cmd/ax/main.go).
        if ($n -ne 1) { Exit-ConFallo 'apply necesita exactamente un -f <fichero>' }
    }
    'ssh' {
        # fase 0: hasta `ssh`; 1: hasta la tarea; 2: opciones de ax tras la
        # tarea; 3: la orden (con o sin '--' delante), entera a `sh -c`.
        $Fase = 0
        $Comando = [System.Collections.Generic.List[string]]::new()
        for ($i = 0; $i -lt $Argumentos.Count; $i++) {
            $a = $Argumentos[$i]
            if ($Fase -eq 3) { $Comando.Add((Citar $a)); continue }
            if ($a -cin $ConValor -and $i + 1 -lt $Argumentos.Count) {
                $Remoto.Add((Citar $a)); $Remoto.Add((Citar $Argumentos[$i + 1])); $i++; continue
            }
            if ($a -clike '--atespace=*' -or $a -clike '--server=*' -or $a -clike '--context=*' -or $a -clike '--namespace=*') {
                $Remoto.Add((Citar $a)); continue
            }
            if ($Fase -eq 2) {
                $Fase = 3
                if ($a -cne '--') { $Comando.Add((Citar $a)) }
                continue
            }
            $Remoto.Add((Citar $a))
            if ($Fase -eq 0 -and $a -ceq 'ssh') { $Fase = 1 }
            elseif ($Fase -eq 1 -and -not $a.StartsWith('-')) { $Fase = 2 }
        }
        if ($Comando.Count -eq 0) { Exit-ConFallo 'ax ssh sin orden no abre ninguna shell (google/ax#374); usa: ssh <tarea> <orden>' }
        $Remoto.Add("'--'"); $Remoto.Add("'sh'"); $Remoto.Add("'-c'"); $Remoto.Add((Citar ($Comando -join ' ')))
    }
    default { foreach ($a in $Argumentos) { $Remoto.Add((Citar $a)) } }
}

# Terminal solo para las órdenes interactivas (watch, ssh) y si tú tienes
# una: así Ctrl-C y el corte de la conexión llegan al servidor como señales.
switch -CaseSensitive ($Tty) {
    'si' { $Modo = '-tt' }
    'no' { $Modo = '-T' }
    'auto' {
        $Modo = '-T'
        if ($Orden -cin @('watch', 'ssh') -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $Modo = '-t' }
    }
    default { Exit-ConFallo 'AX_REMOTO_TTY tiene que ser auto, si o no' }
}
if ($Fichero -and $Modo -cne '-T') { Exit-ConFallo 'apply no admite terminal (AX_REMOTO_TTY=si)' }

$Opciones = [System.Collections.Generic.List[string]]::new()
$Opciones.AddRange([string[]]@('-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=4', $Modo))
if (-not $Fichero -and $Modo -ceq '-T') { $Opciones.Add('-n') }
$Opciones.AddRange([string[]]@('--', $Servidor, ($Remoto -join ' ')))

if (-not (Get-Command -Name $Ssh -CommandType Application -ErrorAction SilentlyContinue)) {
    Exit-ConFallo "no encuentro el programa ssh: $Ssh" 127
}

if (-not $Fichero) {
    & $Ssh @Opciones
    exit $LASTEXITCODE
}

# apply: el fichero va byte a byte por la entrada estándar de ssh.
$Inicio = [System.Diagnostics.ProcessStartInfo]::new($Ssh)
foreach ($o in $Opciones) { $Inicio.ArgumentList.Add($o) }
$Inicio.UseShellExecute = $false
$Inicio.RedirectStandardInput = $true
$Proceso = [System.Diagnostics.Process]::Start($Inicio)
$Flujo = [System.IO.File]::OpenRead($Fichero)
try { $Flujo.CopyTo($Proceso.StandardInput.BaseStream) } finally { $Flujo.Dispose(); $Proceso.StandardInput.Close() }
$Proceso.WaitForExit()
exit $Proceso.ExitCode
