<#
.SYNOPSIS
    Build, test, and install script for cpp-extractor.

.DESCRIPTION
    Automates the full lifecycle of the cpp-extractor component:
      1. Environment auto-detection (LLVM, CMake, MSVC / compiler)
      2. CMake configure + build  (Debug or Release)
      3. Post-build smoke test with an embedded sample
      4. Optional install to a target directory

.PARAMETER Action
    One of: build, clean, rebuild, test, install, check, all.
      build   - configure + compile  (default)
      clean   - wipe the build directory
      rebuild - clean + build
      test    - build + run smoke tests
      install - build + copy artefacts to -InstallDir
      check   - only validate the environment; do not compile
      all     - rebuild + test + install

.PARAMETER Config
    CMake build configuration: Release (default) or Debug.

.PARAMETER Generator
    CMake generator override. Auto-detected if omitted.
    Examples: "Visual Studio 17 2022", "Ninja", "MinGW Makefiles"

.PARAMETER LLVMRoot
    Path to the LLVM installation.  Auto-detected if omitted.

.PARAMETER InstallDir
    Target directory for 'install' action.  Defaults to
    <project-root>/bin (i.e. ../bin relative to this script).

.PARAMETER Jobs
    Parallel build job count.  Defaults to processor count.

.EXAMPLE
    .\build.ps1                      # build Release
    .\build.ps1 -Action test         # build + smoke test
    .\build.ps1 -Action all          # full pipeline
    .\build.ps1 -Action build -Config Debug
    .\build.ps1 -Action install -InstallDir C:\tools\cxxtract
    .\build.ps1 -Action check        # environment check only
#>

[CmdletBinding()]
param(
    [ValidateSet("build", "clean", "rebuild", "test", "install", "check", "all")]
    [string]$Action = "build",

    [ValidateSet("Release", "Debug", "RelWithDebInfo", "MinSizeRel")]
    [string]$Config = "Release",

    [string]$Generator = "",
    [string]$LLVMRoot  = "",
    [string]$InstallDir = "",
    [int]$Jobs = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ====================================================================
# Paths
# ====================================================================
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir          # cxxtract2/
$BuildDir    = Join-Path $ScriptDir "build"
$SrcDir      = Join-Path $ScriptDir "src"

if (-not $InstallDir) {
    $InstallDir = Join-Path $ProjectRoot "bin"
}
if ($Jobs -le 0) {
    $Jobs = [Environment]::ProcessorCount
}

# ====================================================================
# Helpers
# ====================================================================
function Write-Banner  { param([string]$msg) Write-Host "`n========== $msg ==========" -ForegroundColor Cyan }
function Write-Ok      { param([string]$msg) Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-Warn    { param([string]$msg) Write-Host "  [!!]  $msg" -ForegroundColor Yellow }
function Write-Fail    { param([string]$msg) Write-Host "  [XX]  $msg" -ForegroundColor Red }
function Write-Info    { param([string]$msg) Write-Host "  [..]  $msg" -ForegroundColor Gray }

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-SemVer {
    # Extract the first "major.minor.patch" substring from a version string.
    param([string]$raw)
    if ($raw -match '(\d+)\.(\d+)\.?(\d*)') {
        return @{
            Major = [int]$Matches[1]
            Minor = [int]$Matches[2]
            Patch = if ($Matches[3]) { [int]$Matches[3] } else { 0 }
            Text  = $Matches[0]
        }
    }
    return $null
}

# ====================================================================
# 1. Environment checks
# ====================================================================
function Invoke-EnvCheck {
    Write-Banner "Environment Check"

    $script:EnvOK = $true

    # ---- CMake ----
    if (Test-Command "cmake") {
        $cmakeRaw = (cmake --version 2>&1 | Select-Object -First 1) -replace '[^\d.]', '' -replace '^\.' , ''
        $cmakeVer = Get-SemVer $cmakeRaw
        if ($cmakeVer -and $cmakeVer.Major -ge 3 -and $cmakeVer.Minor -ge 20) {
            Write-Ok "CMake $($cmakeVer.Text)"
        } elseif ($cmakeVer) {
            Write-Fail "CMake $($cmakeVer.Text) — need >= 3.20"; $script:EnvOK = $false
        } else {
            Write-Fail "CMake version unreadable"; $script:EnvOK = $false
        }
    } else {
        Write-Fail "CMake not found"; $script:EnvOK = $false
    }

    # ---- Git (nice-to-have for FetchContent) ----
    if (Test-Command "git") {
        Write-Ok "Git available"
    } else {
        Write-Warn "Git not found — CMake FetchContent may fail"
    }

    # ---- LLVM / libclang ----
    $script:LLVMDir = Resolve-LLVMRoot
    if ($script:LLVMDir) {
        # Read LLVM version
        $llvmBin = Join-Path $script:LLVMDir "bin"
        $clangExe = Join-Path $llvmBin "clang.exe"
        if (Test-Path $clangExe) {
            $clangVerRaw = & $clangExe --version 2>&1 | Select-Object -First 1
            $clangVer = Get-SemVer "$clangVerRaw"
            if ($clangVer -and $clangVer.Major -ge 17) {
                Write-Ok "LLVM/Clang $($clangVer.Text) at $($script:LLVMDir)"
            } elseif ($clangVer) {
                Write-Fail "LLVM $($clangVer.Text) — need >= 17"; $script:EnvOK = $false
            }
        } else {
            Write-Warn "clang.exe not found — version check skipped"
        }

        # Verify critical files
        $hdr = Join-Path $script:LLVMDir "include\clang-c\Index.h"
        $lib = Join-Path $script:LLVMDir "lib\libclang.lib"
        $dll = Join-Path $script:LLVMDir "bin\libclang.dll"
        if (Test-Path $hdr) { Write-Ok "clang-c/Index.h found" }
        else { Write-Fail "clang-c/Index.h NOT found at $hdr"; $script:EnvOK = $false }
        if (Test-Path $lib) { Write-Ok "libclang.lib found" }
        else { Write-Fail "libclang.lib NOT found at $lib"; $script:EnvOK = $false }
        if (Test-Path $dll) { Write-Ok "libclang.dll found" }
        else { Write-Warn "libclang.dll not at $dll — runtime may fail" }
    } else {
        Write-Fail "LLVM installation not found (set -LLVMRoot or LLVM_ROOT env var)"
        $script:EnvOK = $false
    }

    # ---- C++ compiler ----
    $script:HasMSVC  = $false
    $script:HasClang = $false
    $script:HasGCC   = $false

    if (Test-Command "cl") {
        $script:HasMSVC = $true
        Write-Ok "MSVC cl.exe available"
    }
    if (Test-Command "clang++") {
        $script:HasClang = $true
        Write-Ok "clang++ available"
    }
    if (Test-Command "g++") {
        $script:HasGCC = $true
        Write-Ok "g++ available"
    }
    if (-not ($script:HasMSVC -or $script:HasClang -or $script:HasGCC)) {
        # Try to find VS via vswhere
        $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
        if (Test-Path $vswhere) {
            $vsPath = & $vswhere -latest -property installationPath 2>$null
            if ($vsPath) {
                Write-Warn "MSVC found via vswhere at $vsPath but cl.exe is not on PATH."
                Write-Warn "  Run from a 'Developer PowerShell for VS' or use the VS generator."
                $script:HasMSVC = $true  # VS generator can find it
            } else {
                Write-Fail "No C++ compiler found"; $script:EnvOK = $false
            }
        } else {
            Write-Fail "No C++ compiler found (cl, clang++, or g++)"; $script:EnvOK = $false
        }
    }

    # ---- Summary ----
    if ($script:EnvOK) {
        Write-Host "`n  Environment OK — ready to build." -ForegroundColor Green
    } else {
        Write-Host "`n  Environment check FAILED — fix the issues above before building." -ForegroundColor Red
    }
    return $script:EnvOK
}


function Resolve-LLVMRoot {
    # Returns the LLVM install root or $null.
    $candidates = @()

    # Explicit parameter
    if ($LLVMRoot -and (Test-Path $LLVMRoot)) { return (Resolve-Path $LLVMRoot).Path }

    # Environment variable
    if ($env:LLVM_ROOT -and (Test-Path $env:LLVM_ROOT)) { $candidates += $env:LLVM_ROOT }

    # Common Windows paths
    $candidates += "C:\Program Files\LLVM"
    $candidates += "$env:ProgramFiles\LLVM"
    $candidates += "C:\LLVM"

    # LLVM installed via scoop / chocolatey
    if ($env:SCOOP)    { $candidates += "$env:SCOOP\apps\llvm\current" }
    if ($env:ChocolateyInstall) { $candidates += "$env:ChocolateyInstall\lib\llvm\tools\LLVM" }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c "include\clang-c\Index.h"))) {
            return (Resolve-Path $c).Path
        }
    }
    return $null
}


# ====================================================================
# 2. CMake Configure
# ====================================================================
function Invoke-Configure {
    Write-Banner "CMake Configure ($Config)"

    if (-not (Test-Path $BuildDir)) {
        New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null
    }

    $cmakeArgs = @(
        "-S", $ScriptDir,
        "-B", $BuildDir
    )

    # Generator selection
    $gen = $Generator
    if (-not $gen) { $gen = Select-Generator }

    $cmakeArgs += "-G", $gen

    # VS generators need -A for architecture
    if ($gen -match "Visual Studio") {
        $cmakeArgs += "-A", "x64"
    }

    # Pass LLVM root hint
    if ($script:LLVMDir) {
        $cmakeArgs += "-DLLVM_ROOT=$($script:LLVMDir)"
    }

    Write-Info "cmake $($cmakeArgs -join ' ')"
    & cmake @cmakeArgs 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "CMake configure failed (exit code $LASTEXITCODE)"
        exit 1
    }
    Write-Ok "Configure complete"
}


function Select-Generator {
    # Auto-pick the best generator available.
    if (Test-Command "ninja") { return "Ninja" }

    # Prefer VS 2022, then 2019
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vsVer = & $vswhere -latest -property catalog_productLineVersion 2>$null
        switch ($vsVer) {
            "2022" { return "Visual Studio 17 2022" }
            "2019" { return "Visual Studio 16 2019" }
        }
    }

    # Fallback heuristics
    if ($script:HasMSVC)  { return "NMake Makefiles" }
    if ($script:HasGCC)   { return "MinGW Makefiles" }
    if ($script:HasClang) { return "Unix Makefiles" }

    return "Visual Studio 17 2022"  # last resort
}


# ====================================================================
# 3. Build
# ====================================================================
function Invoke-Build {
    Write-Banner "Build ($Config, $Jobs jobs)"

    if (-not (Test-Path (Join-Path $BuildDir "CMakeCache.txt"))) {
        Invoke-Configure
    }

    $buildArgs = @(
        "--build", $BuildDir,
        "--config", $Config,
        "--parallel", "$Jobs"
    )

    Write-Info "cmake $($buildArgs -join ' ')"
    & cmake @buildArgs 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Build failed (exit code $LASTEXITCODE)"
        exit 1
    }

    $exe = Find-Executable
    if ($exe) {
        $size = [math]::Round((Get-Item $exe).Length / 1KB, 1)
        Write-Ok "Built: $exe ($size KB)"
    } else {
        Write-Fail "Executable not found after build"
        exit 1
    }
}


function Find-Executable {
    # Returns the path to cpp-extractor.exe in the build tree.
    $candidates = @(
        (Join-Path $BuildDir "$Config\cpp-extractor.exe"),       # VS multi-config
        (Join-Path $BuildDir "Release\cpp-extractor.exe"),       # VS fallback
        (Join-Path $BuildDir "Debug\cpp-extractor.exe"),         # VS debug
        (Join-Path $BuildDir "cpp-extractor.exe")                # single-config (Ninja/Make)
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return (Resolve-Path $c).Path }
    }
    return $null
}


# ====================================================================
# 4. Smoke Test
# ====================================================================
function Invoke-SmokeTest {
    Write-Banner "Smoke Tests"

    $exe = Find-Executable
    if (-not $exe) {
        Write-Fail "cpp-extractor.exe not found — build first"
        exit 1
    }

    $testDir  = Join-Path $BuildDir "_smoke_test"
    $passed   = 0
    $failed   = 0

    try {
        if (Test-Path $testDir) { Remove-Item -Recurse -Force $testDir }
        New-Item -ItemType Directory -Path $testDir -Force | Out-Null

        # Create sample files
        $sampleH = Join-Path $testDir "sample.h"
        $sampleCpp = Join-Path $testDir "sample.cpp"
        $brokenCpp = Join-Path $testDir "broken.cpp"

        Set-Content -Path $sampleH -Value @'
#pragma once
#include <string>
#include <vector>

namespace net {

enum class Proto { TCP, UDP };

class Conn {
public:
    Conn(const std::string& host, int port);
    ~Conn();
    bool Connect();
    void Send(const std::vector<char>& data);
private:
    std::string host_;
    int port_;
    Proto proto_ = Proto::TCP;
};

}  // namespace net
'@

        Set-Content -Path $sampleCpp -Value @'
#include "sample.h"
#include <iostream>

namespace net {

Conn::Conn(const std::string& host, int port)
    : host_(host), port_(port) {}

Conn::~Conn() {}

bool Conn::Connect() { return true; }

void Conn::Send(const std::vector<char>& data) {
    if (!Connect()) return;
}

}  // namespace net

void run_client(const std::string& host) {
    net::Conn conn(host, 80);
    conn.Connect();
    std::vector<char> msg = {'H', 'i'};
    conn.Send(msg);
}

int main() {
    run_client("localhost");
    return 0;
}
'@

        Set-Content -Path $brokenCpp -Value @'
#include <string>
class Bad {
    void foo() { return 42 + ; }
};
UndefinedType x;
'@

        # ---- Test 1: --help exits 0 ----
        $null = & $exe --help 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "T1  --help exits 0"; $passed++
        } else {
            Write-Fail "T1  --help returned $LASTEXITCODE"; $failed++
        }

        # ---- Test 2: Missing args exits non-zero ----
        $null = & $exe 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Ok "T2  missing args -> non-zero exit"; $passed++
        } else {
            Write-Fail "T2  missing args should fail"; $failed++
        }

        # ---- Test 3: extract-all produces valid JSON ----
        $raw = & $exe --action extract-all --file $sampleCpp -- "-I$testDir" "-std=c++17" 2>&1
        $json = $null
        try {
            $json = $raw | ConvertFrom-Json
        } catch {}

        if ($json -and $json.success -eq $true) {
            Write-Ok "T3  extract-all produces valid JSON with success=true"; $passed++
        } else {
            Write-Fail "T3  extract-all JSON parse or success check failed"; $failed++
        }

        # ---- Test 4: Symbols extracted ----
        if ($json -and $json.symbols.Count -gt 0) {
            Write-Ok "T4  symbols: $($json.symbols.Count) found"; $passed++

            # Verify key symbols exist
            $qnames = $json.symbols | ForEach-Object { $_.qualified_name }
            $expectedSymbols = @("net::Conn", "net::Proto", "run_client", "main")
            $allFound = $true
            foreach ($s in $expectedSymbols) {
                if ($s -notin $qnames) {
                    Write-Fail "    Missing expected symbol: $s"
                    $allFound = $false
                }
            }
            if ($allFound) {
                Write-Ok "T4a all expected symbols present"; $passed++
            } else {
                $failed++
            }
        } else {
            Write-Fail "T4  no symbols extracted"; $failed += 2
        }

        # ---- Test 5: References extracted ----
        if ($json -and $json.references.Count -gt 0) {
            Write-Ok "T5  references: $($json.references.Count) found"; $passed++
        } else {
            Write-Fail "T5  no references extracted"; $failed++
        }

        # ---- Test 6: Call edges extracted ----
        if ($json -and $json.call_edges.Count -gt 0) {
            Write-Ok "T6  call_edges: $($json.call_edges.Count) found"; $passed++

            # Verify key edges
            $edges = $json.call_edges | ForEach-Object { "$($_.caller)->$($_.callee)" }
            if ($edges -contains "main->run_client") {
                Write-Ok "T6a main->run_client edge present"; $passed++
            } else {
                Write-Fail "T6a main->run_client edge missing"; $failed++
            }
        } else {
            Write-Fail "T6  no call edges extracted"; $failed += 2
        }

        # ---- Test 7: Include deps extracted ----
        if ($json -and $json.include_deps.Count -gt 0) {
            Write-Ok "T7  include_deps: $($json.include_deps.Count) found"; $passed++
        } else {
            Write-Fail "T7  no include deps extracted"; $failed++
        }

        # ---- Test 8: No duplicate call edges ----
        if ($json -and $json.call_edges) {
            $edgeKeys = $json.call_edges | ForEach-Object { "$($_.caller)|$($_.callee)|$($_.line)" }
            $uniqueEdges = $edgeKeys | Select-Object -Unique
            if ($edgeKeys.Count -eq $uniqueEdges.Count) {
                Write-Ok "T8  no duplicate call edges"; $passed++
            } else {
                Write-Fail "T8  found $($edgeKeys.Count - $uniqueEdges.Count) duplicate call edges"; $failed++
            }
        }

        # ---- Test 9: No duplicate references ----
        if ($json -and $json.references) {
            $refKeys = $json.references | ForEach-Object { "$($_.symbol)|$($_.line)|$($_.col)|$($_.kind)" }
            $uniqueRefs = $refKeys | Select-Object -Unique
            if ($refKeys.Count -eq $uniqueRefs.Count) {
                Write-Ok "T9  no duplicate references"; $passed++
            } else {
                Write-Fail "T9  found $($refKeys.Count - $uniqueRefs.Count) duplicate references"; $failed++
            }
        }

        # ---- Test 10: Destructor naming (no double tilde) ----
        if ($json -and $json.symbols) {
            $dtors    = @($json.symbols | Where-Object { $_.kind -eq "Destructor" })
            $badDtors = @($dtors | Where-Object { $_.qualified_name -match "~~" })
            if ($dtors.Count -gt 0 -and $badDtors.Count -eq 0) {
                Write-Ok "T10 destructor names correct (no ~~)"; $passed++
            } elseif ($dtors.Count -eq 0) {
                Write-Warn "T10 no destructors to check — skipped"
            } else {
                Write-Fail "T10 destructor double-tilde: $($badDtors[0].qualified_name)"; $failed++
            }
        }

        # ---- Test 11: extract-symbols filter ----
        $rawSym = & $exe --action extract-symbols --file $sampleCpp -- "-I$testDir" "-std=c++17" 2>&1
        $jSym = $rawSym | ConvertFrom-Json
        if ($jSym -and $jSym.symbols.Count -gt 0 -and $jSym.references.Count -eq 0 -and $jSym.call_edges.Count -eq 0) {
            Write-Ok "T11 extract-symbols: symbols only, no refs/edges"; $passed++
        } else {
            Write-Fail "T11 extract-symbols filter incorrect"; $failed++
        }

        # ---- Test 12: extract-refs filter ----
        $rawRef = & $exe --action extract-refs --file $sampleCpp -- "-I$testDir" "-std=c++17" 2>&1
        $jRef = $rawRef | ConvertFrom-Json
        if ($jRef -and $jRef.symbols.Count -eq 0 -and $jRef.references.Count -gt 0 -and $jRef.call_edges.Count -gt 0) {
            Write-Ok "T12 extract-refs: refs+edges only, no symbols"; $passed++
        } else {
            Write-Fail "T12 extract-refs filter incorrect"; $failed++
        }

        # ---- Test 13: Header-only extraction ----
        $rawH = & $exe --action extract-all --file $sampleH -- "-std=c++17" 2>&1
        $jH = $rawH | ConvertFrom-Json
        if ($jH -and $jH.success -eq $true -and $jH.symbols.Count -gt 0) {
            Write-Ok "T13 header-only extraction works ($($jH.symbols.Count) symbols)"; $passed++
        } else {
            Write-Fail "T13 header-only extraction failed"; $failed++
        }

        # ---- Test 14: Broken file produces diagnostics but success=true (KeepGoing) ----
        $rawBrk = & $exe --action extract-all --file $brokenCpp -- "-std=c++17" 2>&1
        $jBrk = $rawBrk | ConvertFrom-Json
        if ($jBrk -and $jBrk.success -eq $true -and $jBrk.diagnostics.Count -gt 0) {
            Write-Ok "T14 broken file: success=true + $($jBrk.diagnostics.Count) diagnostics"; $passed++
        } else {
            Write-Fail "T14 broken file handling incorrect"; $failed++
        }

        # ---- Test 15: Nonexistent file returns success=false ----
        $rawMissing = & $exe --action extract-all --file "$testDir\nope.cpp" 2>&1
        $jMissing = $rawMissing | ConvertFrom-Json
        if ($jMissing -and $jMissing.success -eq $false) {
            Write-Ok "T15 nonexistent file: success=false"; $passed++
        } else {
            Write-Fail "T15 nonexistent file should return success=false"; $failed++
        }

    } finally {
        # Cleanup
        if (Test-Path $testDir) {
            Remove-Item -Recurse -Force $testDir 2>$null
        }
    }

    # ---- Summary ----
    Write-Host ""
    $total = $passed + $failed
    if ($failed -eq 0) {
        Write-Host "  All $total tests PASSED" -ForegroundColor Green
    } else {
        Write-Host "  $passed/$total passed, $failed FAILED" -ForegroundColor Red
        exit 1
    }
}


# ====================================================================
# 5. Install
# ====================================================================
function Invoke-Install {
    Write-Banner "Install -> $InstallDir"

    $exe = Find-Executable
    if (-not $exe) {
        Write-Fail "cpp-extractor.exe not found — build first"
        exit 1
    }

    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    # Copy executable
    Copy-Item -Path $exe -Destination $InstallDir -Force
    Write-Ok "Copied cpp-extractor.exe"

    # Copy libclang.dll if present alongside the exe
    $exeDir = Split-Path -Parent $exe
    $dll = Join-Path $exeDir "libclang.dll"
    if (Test-Path $dll) {
        Copy-Item -Path $dll -Destination $InstallDir -Force
        Write-Ok "Copied libclang.dll"
    } else {
        Write-Warn "libclang.dll not found next to exe — you may need to copy it manually"
    }

    # Verify installed binary runs
    $installedExe = Join-Path $InstallDir "cpp-extractor.exe"
    $null = & $installedExe --help 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Installed binary verified"
    } else {
        Write-Fail "Installed binary failed to run (exit code $LASTEXITCODE)"
        exit 1
    }

    Write-Host ""
    Write-Host "  Install complete.  Binary at:" -ForegroundColor Green
    Write-Host "    $installedExe" -ForegroundColor White
    Write-Host ""
    Write-Host "  Update config.yaml:" -ForegroundColor Gray
    Write-Host "    extractor_binary: `"$($installedExe -replace '\\', '/')`"" -ForegroundColor White
}


# ====================================================================
# 6. Clean
# ====================================================================
function Invoke-Clean {
    Write-Banner "Clean"
    if (Test-Path $BuildDir) {
        try {
            Remove-Item -Recurse -Force $BuildDir -ErrorAction Stop
            Write-Ok "Removed $BuildDir"
        } catch {
            Write-Warn "Could not fully remove $BuildDir (files may be locked)"
            Write-Info "Attempting partial clean..."
            # Delete what we can — skip locked files
            Get-ChildItem -Path $BuildDir -Recurse -File -ErrorAction SilentlyContinue |
                ForEach-Object {
                    try { Remove-Item $_.FullName -Force -ErrorAction Stop } catch {}
                }
            Get-ChildItem -Path $BuildDir -Recurse -Directory -ErrorAction SilentlyContinue |
                Sort-Object { $_.FullName.Length } -Descending |
                ForEach-Object {
                    try { Remove-Item $_.FullName -Force -Recurse -ErrorAction Stop } catch {}
                }
            # Force CMake re-configure by removing the cache
            $cache = Join-Path $BuildDir "CMakeCache.txt"
            if (Test-Path $cache) {
                try { Remove-Item $cache -Force -ErrorAction Stop; Write-Ok "Removed CMakeCache.txt" } catch {}
            }
            Write-Warn "Partial clean done — some locked files may remain"
        }
    } else {
        Write-Info "Build directory does not exist — nothing to clean"
    }
}


# ====================================================================
# Main dispatcher
# ====================================================================
Write-Host ""
Write-Host "  cpp-extractor build system" -ForegroundColor White
Write-Host "  Action=$Action  Config=$Config  Jobs=$Jobs" -ForegroundColor DarkGray
Write-Host ""

switch ($Action) {
    "check" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
    }
    "clean" {
        Invoke-Clean
    }
    "build" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
        Invoke-Configure
        Invoke-Build
    }
    "rebuild" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
        Invoke-Clean
        Invoke-Configure
        Invoke-Build
    }
    "test" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
        Invoke-Configure
        Invoke-Build
        Invoke-SmokeTest
    }
    "install" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
        Invoke-Configure
        Invoke-Build
        Invoke-Install
    }
    "all" {
        $ok = Invoke-EnvCheck
        if (-not $ok) { exit 1 }
        Invoke-Clean
        Invoke-Configure
        Invoke-Build
        Invoke-SmokeTest
        Invoke-Install
    }
}

Write-Host ""
